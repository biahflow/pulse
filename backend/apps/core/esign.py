"""Assinatura eletrônica atrás de flag (`esign`), agnóstica de fornecedor (ADR 0007).

Dois lados, no mesmo molde do `tasksync.py`:

- **SAÍDA** (Biahflow → fornecedor): `send_for_signature()` cria a solicitação no provedor e
  devolve as referências que ligam a `SignatureRequest` ao documento/signatário de lá.
- **ENTRADA** (fornecedor → Biahflow): o webhook (`/api/v1/esign/webhook/`) valida o HMAC do
  corpo cru, normaliza o evento (`parse_event`) e aplica o status (`apply_event`) — é a
  transição `pending → signed/declined` de verdade. O `mark-signed` do `DocumentViewSet`
  segue como fallback manual para quando não há provedor configurado.

Uma solicitação tem **N signatários numa rodada só** (issue #115, ADR 0065): a casa, a parte
contratante e as testemunhas assinam o mesmo documento, então o fornecedor é chamado uma vez com a
lista inteira. A rodada é o `document_ref` que ele devolve, e é ela — não "todas as solicitações do
documento" — que responde se o instrumento está assinado. Onde cada assinatura aparece na página é
propriedade da solicitação (`positions`), não do arquivo: a Autentique não lê âncora de texto.

O fornecedor em uso é o **Autentique**; o Clicksign fica como segundo adaptador.
`ESIGN_PROVIDER` escolhe qual vale e, sem um reconhecido, cai no `NullProvider` (só registra
a intenção). Cada fornecedor traz seu próprio esquema de assinatura do webhook — header e
formato do HMAC diferem —, por isso `verify()` pertence ao adaptador e não à view. As
chamadas HTTP reais ficam fora da cobertura (`# pragma: no cover`), como em `tasksync.py`.
"""

from __future__ import annotations

import base64
import hmac
import io
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

import pypdf
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from . import drive, flags, notifications
from .document import conteudo_e_pdf
from .exceptions import DriveUnavailable
from .portal import sign

if TYPE_CHECKING:
    from .models import Document, SignatureRequest

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return flags.is_enabled("esign")


@dataclass(frozen=True)
class Event:
    """Evento do fornecedor já normalizado para o vocabulário do Biahflow."""

    status: str  # SignatureRequest.Status
    provider_ref: str = ""
    document_ref: str = ""
    signer_email: str = ""


class EsignProviderError(Exception):
    """O fornecedor de assinatura não devolveu uma solicitação utilizável.

    `_http_raw` engole a falha de rede e devolve `None` de propósito — o portal não pode cair
    porque o fornecedor caiu. O problema era o degrau seguinte: `None` virava um `SignatureRef`
    vazio, **indistinguível de sucesso**, e a solicitação era gravada assim mesmo. Este tipo existe
    para separar as duas coisas.
    """


@dataclass(frozen=True)
class SignatureRef:
    """O que o fornecedor devolve ao criar a solicitação."""

    provider_ref: str = ""  # identifica o signatário; chave de busca do webhook
    document_ref: str = ""  # identifica o documento; fallback junto com o e-mail
    sign_url: str = ""  # link para o signatário assinar (vai no lembrete)


@dataclass(frozen=True)
class Signer:
    """Um signatário da rodada: quem assina e **em que papel** (issue #115).

    O papel é `SignatureRequest.SignerRole` — o vocabulário mora lá, num lugar só, e aqui só se
    carrega o valor. Ele decide três coisas distintas, e nenhuma delas é derivável do e-mail: onde
    a assinatura aparece na página (`POSICAO_POR_PAPEL`), qual `action` vai para o fornecedor
    (testemunha assina como testemunha) e, na volta, para quem sai o convite do Discovery.
    """

    email: str
    role: str


class Provider(Protocol):
    """Contrato do adaptador de fornecedor (Autentique e Clicksign hoje)."""

    def send(self, document: Document, signers: Sequence[Signer]) -> list[SignatureRef]:
        """Cria **uma** solicitação com todos os signatários e devolve uma referência por signatário.

        A lista é o contrato, e não um laço do chamador: os três signatários de um contrato assinam
        **o mesmo** documento. Chamar o fornecedor uma vez por pessoa criaria três documentos
        separados lá dentro, cada um com uma assinatura — o defeito exato que a issue #115 existe
        para não ter, e que não faz barulho nenhum ao acontecer.
        """

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """A entrega veio mesmo do fornecedor?"""

    def parse_event(self, payload: dict) -> Event | None:
        """Normaliza a entrega; `None` quando o evento não interessa ao portal."""


class NullProvider:
    """Sem fornecedor homologado: registra a intenção e não promete nada."""

    def send(self, document: Document, signers: Sequence[Signer]) -> list[SignatureRef]:
        for signer in signers:
            logger.info(
                "Solicitação de assinatura sem provedor homologado doc=%s signer=%s papel=%s",
                document.pk, signer.email, signer.role,
            )
        return [SignatureRef() for _ in signers]

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return False

    def parse_event(self, payload: dict) -> Event | None:
        return None


# De-para explícito de eventos do Autentique (o ADR exige não perder informação). Só estes
# dois movem a assinatura; `signature.viewed`, os biométricos e os de documento
# (`document.finished` etc.) não mudam o estado de nenhum signatário.
_AUTENTIQUE_EVENTS: dict[str, str] = {
    "signature.accepted": "signed",
    "signature.rejected": "declined",
}

# `sandbox` é argumento do `createDocument` (não campo do `DocumentInput` — confirmado por
# introspecção do schema; a documentação sugere o contrário).
_AUTENTIQUE_CREATE = """
mutation CreateDocument(
  $document: DocumentInput!, $signers: [SignerInput!]!, $file: Upload!, $sandbox: Boolean!
) {
  createDocument(document: $document, signers: $signers, file: $file, sandbox: $sandbox) {
    id
    signatures { public_id email link { short_link } }
  }
}
"""


def delivers_by_link() -> bool:
    """O portal entrega o link de assinatura (em vez de o fornecedor mandar o convite)?"""
    return settings.ESIGN_DELIVERY == "link"


# Frase escrita num lugar só (issue #112): nomeia o que foi **observado** — a combinação não
# entregou o convite numa rodada de homologação —, nunca a causa. Três hipóteses sobrevivem
# (sandbox não dispara convite; o signatário era o dono da conta; atraso maior que a janela do
# teste) e nenhuma está provada; escrever "o sandbox não manda e-mail" aqui seria inventar causa.
_AVISO_SANDBOX_COM_ENTREGA_POR_EMAIL = (
    "sandbox com entrega por e-mail: combinação não observada entregando o convite (issue #112)"
)


def aviso_de_entrega() -> str:
    """Vazio quando não há o que avisar; a frase quando a combinação é a não observada."""
    if settings.ESIGN_SANDBOX and not delivers_by_link():
        return _AVISO_SANDBOX_COM_ENTREGA_POR_EMAIL
    return ""


# --- Onde a assinatura aparece na página (issue #115, ADR 0065) ---------------------------------
#
# A Autentique **não tem** detecção de âncora por texto: a linha "Assinatura: ____" do documento
# não é lida por ninguém. Onde a assinatura aparece é propriedade da *solicitação*, e se declara em
# `positions` dentro de cada signatário do `createDocument`. Sem esse campo, o painel do fornecedor
# diz o que disse na primeira assinatura real (03/09/2026): *"esse signatário não possui campos de
# assinatura visíveis, a assinatura dele aparecerá somente na última página ao baixar o arquivo"*.
#
# Cada posição é `{x, y, z, element}`: `x`/`y` em **percentual 0–100** da largura/altura da página,
# com origem no **topo**, e `z` é o **número da página** — não existe "última página", nem `z: -1`,
# nem repetição em todas. É por isso que o documento precisa ser lido: a página do bloco de
# assinatura não é dedutível de nenhum outro dado que o portal já tenha.

# **A posição é lida do próprio documento, e não cravada por papel.** A primeira versão desta ADR
# tinha um mapa fixo `papel → (x, y)`, declarado como estimativa. A medição dos dois templates reais
# em PDF (03/09/2026) mostrou que ele não podia funcionar: as mesmas quatro linhas ficam a **dez
# pontos percentuais** de distância entre um e outro, porque a última página do contrato carrega
# mais texto que a do NDA.
#
#     linha              Contrato   NDA
#     casa               37,24%     26,74%
#     parte contratante  48,58%     38,08%
#     testemunha 1       66,89%     56,39%
#     testemunha 2       77,06%     66,55%
#
# E o problema não é ter dois templates: é que a razão social, o endereço e o objeto do cliente
# entram no texto e empurram o bloco dentro do **mesmo** template. Um número cravado acerta o
# documento em que foi medido e erra o próximo, sem nada ficar vermelho.
#
# A Autentique não tem detecção de âncora por texto — mas nós temos o PDF em mãos, e `pypdf` já é
# dependência por causa da contagem de páginas. Então a âncora é lida aqui: acham-se as linhas de
# assinatura (corridas de sublinhado) da última página, e o rótulo "Testemunhas:" separa as de parte
# das de testemunha. Validado contra os dois documentos reais, que produzem exatamente duas de cada.

# Corrida de sublinhado que conta como linha de assinatura. **Vinte é o limiar e ele é medido**: as
# linhas de assinatura dos templates têm 43–47 caracteres, e a linha de data logo acima
# ("__________________, ______ de __________________ de 20____.") tem no máximo 19 por corrida.
# Sem o limiar, a primeira "linha de assinatura" encontrada seria a data.
_LINHA_DE_ASSINATURA = re.compile(r"_{20,}")

# O rótulo que separa as duas metades do bloco. Acima dele estão as linhas das partes (a casa
# primeiro, depois a contraparte — é a ordem em que o template as desenha); abaixo, as das
# testemunhas.
_ROTULO_DAS_TESTEMUNHAS = "Testemunhas"

# A assinatura fica **acima** da linha, como uma assinatura à mão. `y` cresce para baixo (origem no
# topo), então subir é diminuir. 2,2% de uma A4 são ~18pt, a altura de uma assinatura.
#
# ⚠️ **Este deslocamento é o único número não medido que sobrou**, e é assim porque a documentação
# do fornecedor não diz se `x`/`y` são o canto superior esquerdo ou o centro do elemento. O primeiro
# envio real resolve: se a assinatura sair sobre o texto do rótulo, aumente; se sair solta acima,
# diminua. Antes desta medição eram oito números no escuro; agora é um.
_ACIMA_DA_LINHA = 2.2
# E um pouco à direita de onde a linha começa, para a assinatura não vazar pela esquerda.
_APOS_O_INICIO_DA_LINHA = 3.0


@dataclass(frozen=True)
class ItemDeTexto:
    """Um pedaço de texto da página, já em percentual — a entrada da leitura de âncoras.

    Percentual e não ponto: é a unidade em que a Autentique recebe a posição, e converter na
    fronteira (onde se conhece a dimensão da página) evita carregar `mediabox` mundo adentro.
    `y` tem origem no **topo**, como o `positions` do fornecedor — e ao contrário do PDF, cuja
    origem é embaixo. A conversão acontece uma vez, em `_itens_da_ultima_pagina`.
    """

    y: float
    x: float
    texto: str


@dataclass(frozen=True)
class Ancoras:
    """As linhas de assinatura encontradas na última página, já na ordem do documento."""

    pagina: int
    partes: tuple[tuple[float, float], ...]  # (x, y) da casa e da contraparte, nessa ordem
    testemunhas: tuple[tuple[float, float], ...]


def ancoras_de_assinatura(pagina: int, itens: Sequence[ItemDeTexto]) -> Ancoras | None:
    """As linhas de assinatura da página, separadas pelo rótulo "Testemunhas:".

    Função **pura**, e é o ponto inteiro: a leitura do PDF é I/O e fica fora da cobertura, mas a
    regra que decide o que é linha de assinatura e a quem ela pertence é testada com a geometria
    real dos dois templates. Mesmo desenho de `classify` neste módulo.

    Devolve `None` quando não encontra **as duas** linhas de parte. Achar uma só, ou nenhuma,
    significa que este documento não tem o bloco que a casa desenha — e posicionar assinatura a
    partir de um palpite é pior do que mandá-la para a página anexa, que é o que acontece sem
    `positions`.
    """
    linhas = sorted(
        {(item.y, item.x) for item in itens if _LINHA_DE_ASSINATURA.search(item.texto)}
    )
    rotulo = min(
        (item.y for item in itens if _ROTULO_DAS_TESTEMUNHAS in item.texto), default=None
    )
    if rotulo is None:
        partes, testemunhas = linhas, []
    else:
        partes = [linha for linha in linhas if linha[0] < rotulo]
        testemunhas = [linha for linha in linhas if linha[0] > rotulo]
    if len(partes) < 2:
        return None
    return Ancoras(
        pagina=pagina,
        # As duas **últimas** acima do rótulo, e não as duas primeiras: se um dia entrar uma linha
        # de sublinhado longa mais acima na página, é o bloco de assinatura que fica colado ao
        # rótulo, não ela.
        partes=tuple((x, y) for y, x in partes[-2:]),
        testemunhas=tuple((x, y) for y, x in testemunhas),
    )


def _itens_da_ultima_pagina(  # pragma: no cover - I/O de PDF, como `_request` é I/O de rede
    conteudo: bytes,
) -> tuple[int, list[ItemDeTexto]] | None:
    """Lê a última página do PDF e devolve seus textos posicionados em percentual.

    **Nunca levanta**: posicionar é auxílio, não a operação. PDF corrompido, ilegível ou arquivo
    que não é PDF devolvem `None`, e a solicitação sai sem posição — que é o comportamento de
    antes da issue #115, ruim mas funcional. Recusar seria pior que o defeito.

    A posição de um texto no PDF é a matriz de texto composta com a de transformação corrente
    (`cm ∘ tm`) — usar só a `tm` devolve a posição relativa dentro do bloco e coloca o documento
    inteiro na mesma altura, que foi o primeiro resultado ao medir os templates reais.
    """
    if not conteudo_e_pdf(conteudo):
        return None
    try:
        leitor = pypdf.PdfReader(io.BytesIO(conteudo))
        pagina = leitor.pages[-1]
        largura, altura = float(pagina.mediabox.width), float(pagina.mediabox.height)
        if largura <= 0 or altura <= 0:
            return None
        itens: list[ItemDeTexto] = []

        def visitar(texto, cm, tm, fonte, tamanho) -> None:  # type: ignore[no-untyped-def]
            limpo = (texto or "").strip()
            if not limpo:
                return
            x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
            y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
            itens.append(
                ItemDeTexto(y=(altura - y) / altura * 100, x=x / largura * 100, texto=limpo)
            )

        pagina.extract_text(visitor_text=visitar)
        return len(leitor.pages), itens
    except Exception as exc:  # noqa: BLE001 - ler o PDF não derruba o envio
        logger.info("PDF ilegível ao procurar as linhas de assinatura (%s)", exc)
        return None

# Os `Document.Kind` que têm bloco de assinatura desenhado. A Proposta não tem, e o `kind` vazio
# não diz nada — nos dois casos a solicitação sai **sem** posição, com o motivo no log, em vez de
# carimbar uma assinatura no meio de um texto corrido. Molde de `DOCUMENT_KINDS_QUE_ABREM_ENGAGEMENT`
# (`models.py`): a decisão mora numa constante, não numa condição espalhada.
DOCUMENT_KINDS_COM_BLOCO_DE_ASSINATURA = frozenset(
    {"design_partner_agreement", "nda", "commercial_contract"}
)


def _acao_do_papel(role: str) -> str:
    """`SIGN`, ou `SIGN_AS_A_WITNESS` para quem testemunha (valores do `SignerInput`)."""
    return "SIGN_AS_A_WITNESS" if role == "witness" else "SIGN"


def posicoes_da_rodada(
    document: Document, signers: Sequence[Signer], conteudo: bytes
) -> list[dict[str, str] | None]:
    """Uma posição por signatário, alinhada com `signers` — `None` para quem vai sem posição.

    Sem posição **manda assim mesmo** e registra o motivo. Não recusa: o fluxo real de hoje usa
    `.docx`, e recusar quebraria o que funciona hoje para ganhar um posicionamento que aquele
    formato não permite calcular.
    """
    sem_posicao: list[dict[str, str] | None] = [None for _ in signers]
    if document.kind not in DOCUMENT_KINDS_COM_BLOCO_DE_ASSINATURA:
        logger.info(
            "documento %s (kind=%r) não tem bloco de assinatura; assinaturas sem posição",
            document.pk, document.kind,
        )
        return sem_posicao
    lido = _itens_da_ultima_pagina(conteudo)
    if lido is None:
        logger.info("documento %s não é um PDF legível; assinaturas sem posição", document.pk)
        return sem_posicao
    ancoras = ancoras_de_assinatura(*lido)
    if ancoras is None:
        logger.info(
            "documento %s não tem o bloco de assinatura da casa na última página; "
            "assinaturas sem posição",
            document.pk,
        )
        return sem_posicao

    # A casa assina na primeira linha de parte e a contraparte na segunda — é a ordem em que o
    # template as desenha, e não uma convenção deste módulo.
    casa, contraparte = ancoras.partes
    pagina = str(ancoras.pagina)
    testemunhas = 0
    posicoes: list[dict[str, str] | None] = []
    for signer in signers:
        if signer.role == "witness":
            if testemunhas >= len(ancoras.testemunhas):
                # Empilhar duas assinaturas no mesmo ponto produz um documento ilegível, que é pior
                # que uma assinatura na página anexa.
                logger.info(
                    "documento %s tem %d linha(s) de testemunha; %s assina sem posição",
                    document.pk, len(ancoras.testemunhas), signer.email,
                )
                posicoes.append(None)
                continue
            ponto = ancoras.testemunhas[testemunhas]
            testemunhas += 1
        elif signer.role == "house":
            ponto = casa
        elif signer.role == "counterparty":
            ponto = contraparte
        else:  # pragma: no cover - a view recusa papel desconhecido com 400
            logger.info("papel %r sem linha no documento; %s assina sem posição",
                        signer.role, signer.email)
            posicoes.append(None)
            continue
        posicoes.append(_posicao(ponto, pagina))
    return posicoes


def _posicao(ponto: tuple[float, float], pagina: str) -> dict[str, str]:
    """A posição no formato do fornecedor, já deslocada para cima da linha.

    Os três valores vão como **string**, que é a forma dos exemplos de `positions` da documentação
    da Autentique. Uma casa decimal basta: a página tem ~600pt de largura, então 0,1% é meio ponto.
    """
    x, y = ponto
    return {
        "x": f"{max(0.0, min(100.0, x + _APOS_O_INICIO_DA_LINHA)):.1f}",
        "y": f"{max(0.0, min(100.0, y - _ACIMA_DA_LINHA)):.1f}",
        "z": pagina,
        "element": "SIGNATURE",
    }


def lacuna_de_posicionamento(document: Document) -> str | None:
    """A lacuna de posicionamento que já dá para saber **sem** ler os bytes reais do arquivo.

    `None` é "nenhuma lacuna conhecida", e não promessa de posição: a âncora pode faltar na hora
    do envio, e quem decide de verdade continua sendo `posicoes_da_rodada`, lendo o documento.
    Serve à tela, que precisa avisar antes do clique (DAP `dap-assinatura-com-papeis-r1`, E1).

    Na mesma ordem de `posicoes_da_rodada`: primeiro a finalidade sem bloco de assinatura, depois
    o conteúdo sabidamente não-PDF. "Sabidamente" exclui de propósito o documento que mora no
    Drive e ainda não tem carimbo — farejar ali seria um download por linha da listagem.
    """
    if document.kind not in DOCUMENT_KINDS_COM_BLOCO_DE_ASSINATURA:
        return "kind_without_block"
    carimbo = document.content_is_pdf
    if carimbo is None and not document.drive_file_id and document.file:
        try:
            with document.file.open("rb") as arquivo:
                carimbo = conteudo_e_pdf(arquivo.read(5))
        except Exception as exc:  # noqa: BLE001 - posicionar é auxílio, não a operação
            logger.info("arquivo do documento %s ilegível ao farejar o tipo (%s)", document.pk, exc)
            return None
    if carimbo is False:
        return "not_pdf"
    return None


def _autentique_signer(
    signer: Signer, position: dict[str, str] | None = None
) -> dict[str, object]:
    """Signatário no formato do Autentique, conforme quem avisa (ver `ESIGN_DELIVERY`).

    Na entrega por link o fornecedor não manda convite — e exige `name`, que derivamos do
    e-mail — mas devolve o `short_link`, que o portal usa no convite e no lembrete.

    Sem posição o formato é **o mesmo de antes da issue #115**, chave por chave: é o que mantém o
    modo `link` e o contrato da `/api/v1/` idênticos para o documento que não é PDF.
    """
    if delivers_by_link():
        dados: dict[str, object] = {
            "email": signer.email,
            "name": signer.email.split("@")[0],
            "action": _acao_do_papel(signer.role),
            "delivery_method": "DELIVERY_METHOD_LINK",
        }
    else:
        dados = {"email": signer.email, "action": _acao_do_papel(signer.role)}
    if position is not None:
        dados["positions"] = [position]
    return dados


class AutentiqueProvider:
    """Adaptador Autentique: GraphQL (multipart) na saída, `x-autentique-signature` na entrada."""

    DEFAULT_BASE = "https://api.autentique.com.br/v2/graphql"

    def send(self, document: Document, signers: Sequence[Signer]) -> list[SignatureRef]:
        if not settings.ESIGN_API_TOKEN:
            logger.info("Autentique sem ESIGN_API_TOKEN; solicitação só registrada localmente")
            return [SignatureRef() for _ in signers]
        content = _document_bytes(document)
        if not content:
            logger.warning("Documento %s sem conteúdo; nada enviado ao Autentique", document.pk)
            return [SignatureRef() for _ in signers]
        posicoes = posicoes_da_rodada(document, signers, content)
        operations = json.dumps(
            {
                "query": _AUTENTIQUE_CREATE,
                "variables": {
                    "document": {"name": document.original_name},
                    # Um `createDocument` com N signatários, nunca N chamadas: é a mesma folha de
                    # papel que os três assinam.
                    "signers": [
                        _autentique_signer(signer, posicao)
                        for signer, posicao in zip(signers, posicoes, strict=True)
                    ],
                    "file": None,
                    "sandbox": bool(settings.ESIGN_SANDBOX),
                },
            }
        )
        body, content_type = _multipart(
            {"operations": operations, "map": '{"file": ["variables.file"]}'},
            document.original_name,
            content,
        )
        data = self._post(body, content_type)
        return self._parse_created(data, signers)

    @staticmethod
    def _parse_ping(data: dict | None) -> tuple[bool, str]:
        """Lê a resposta do `me` (separada do I/O para ficar testável, como `_parse_created`)."""
        me = ((data or {}).get("data") or {}).get("me") or {}
        email = str(me.get("email", ""))
        if not email:
            return False, "o Autentique não reconheceu o token"
        return True, f"conta {email} acessível"

    def ping(self) -> tuple[bool, str]:  # pragma: no cover - I/O com o fornecedor
        """Pergunta de quem é o token, sem criar documento nem cobrar nada.

        O gancho existe em `integrations._probe_esign` desde a FDD 024, mas nenhum adaptador o
        implementava — e o e-sign era a única integração configurada que dizia "sem sonda
        disponível". A rodada 4 confirmou que a query `me` serve.
        """
        # `_http_raw` e não `_http`: este último é o helper do **Clicksign**, que leva o token na
        # URL e por isso não manda header de autorização — usá-lo aqui rende um 401 com um token
        # perfeitamente válido. Foi o que aconteceu na primeira versão desta sonda, e só apareceu
        # porque ela foi rodada contra o fornecedor de verdade.
        return self._parse_ping(
            _http_raw(
                settings.ESIGN_API_BASE or self.DEFAULT_BASE,
                json.dumps({"query": "{ me { email } }"}).encode(),
                {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.ESIGN_API_TOKEN}",
                },
            )
        )

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        secret = settings.ESIGN_WEBHOOK_SECRET
        provided = headers.get("x-autentique-signature", "")
        if not secret or not provided:
            return False
        return hmac.compare_digest(provided, sign(secret, body))  # hex puro, sem prefixo

    def parse_event(self, payload: dict) -> Event | None:
        event = payload.get("event") or {}
        status = _AUTENTIQUE_EVENTS.get(str(event.get("type", "")).strip().lower())
        if status is None:
            return None
        data = event.get("data") or {}
        return Event(
            status=status,
            provider_ref=str(data.get("public_id", "")),
            document_ref=str(data.get("document", "")),
            signer_email=str((data.get("user") or {}).get("email", "")),
        )

    @staticmethod
    def _parse_created(
        data: dict | None, signers: Sequence[Signer]
    ) -> list[SignatureRef]:
        """Lê a resposta do `createDocument` (separada do I/O para ficar testável).

        Uma referência por signatário **pedido**, casada por e-mail, e nenhuma inventada. A versão
        anterior escolhia *um* signatário e caía num fallback `signatures[0]` quando o e-mail não
        casava: com um signatário só ele acertava por sorte, mas numa lista de três pegaria calado
        a referência de outra pessoa — e o webhook passaria a fechar a assinatura errada.

        O `document_ref` é o mesmo para todos (é o `id` do documento criado) e **é a rodada**: as
        solicitações que o compartilham são as que precisam estar todas assinadas para o documento
        contar como assinado (`Document.is_signed`).
        """
        created = ((data or {}).get("data") or {}).get("createDocument") or {}
        document_ref = str(created.get("id", ""))
        por_email = {
            str(assinatura.get("email", "")).lower(): assinatura
            for assinatura in created.get("signatures") or []
        }
        refs: list[SignatureRef] = []
        for signer in signers:
            assinatura = por_email.get(signer.email.lower())
            if assinatura is None:
                logger.warning(
                    "Autentique não devolveu assinatura para %s no documento %s",
                    signer.email, document_ref or "(sem referência)",
                )
                # Sem `provider_ref` — e é `send_for_signature` que transforma isso em falha alta,
                # em vez de gravar uma solicitação que o webhook nunca poderá fechar.
                refs.append(SignatureRef(document_ref=document_ref))
                continue
            refs.append(
                SignatureRef(
                    provider_ref=str(assinatura.get("public_id", "")),
                    document_ref=document_ref,
                    sign_url=str((assinatura.get("link") or {}).get("short_link", "")),
                )
            )
        return refs

    def _post(self, body: bytes, content_type: str) -> dict | None:  # pragma: no cover - I/O
        return _http_raw(
            settings.ESIGN_API_BASE or self.DEFAULT_BASE,
            body,
            {
                "Content-Type": content_type,
                "Authorization": f"Bearer {settings.ESIGN_API_TOKEN}",
            },
        )


# De-para explícito de eventos do Clicksign (o ADR exige não perder informação).
# `deadline` e os eventos de upload/visualização não movem a assinatura: ficam pendentes.
_CLICKSIGN_EVENTS: dict[str, str] = {
    "sign": "signed",
    "auto_close": "signed",
    "document_closed": "signed",
    "refusal": "declined",
    "cancel": "declined",
}


class ClicksignProvider:
    """Adaptador Clicksign: API REST para a saída, webhook `Content-Hmac` para a entrada."""

    DEFAULT_BASE = "https://sandbox.clicksign.com"

    def send(self, document: Document, signers: Sequence[Signer]) -> list[SignatureRef]:
        if len(signers) > 1:
            # **Recusa, e não laço.** `_create_signature_request` faz um upload por chamada, então
            # repeti-la produziria N documentos separados no Clicksign, cada um com uma assinatura —
            # e nenhum deles seria o contrato que as três pessoas pensam ter assinado. Falhar alto
            # aqui é a diferença entre um adaptador incompleto e um adaptador que mente.
            raise EsignProviderError(
                "o adaptador Clicksign não implementa múltiplos signatários numa solicitação"
            )
        if not settings.ESIGN_API_TOKEN:
            logger.info("Clicksign sem ESIGN_API_TOKEN; solicitação só registrada localmente")
            return [SignatureRef() for _ in signers]
        content = _document_bytes(document)
        if not content:
            logger.warning("Documento %s sem conteúdo; nada enviado ao Clicksign", document.pk)
            return [SignatureRef() for _ in signers]
        return [self._create_signature_request(document, signers[0].email, content)]

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        secret = settings.ESIGN_WEBHOOK_SECRET
        provided = headers.get("Content-Hmac", "")
        if not secret or not provided:
            return False
        return hmac.compare_digest(provided, f"sha256={sign(secret, body)}")

    def parse_event(self, payload: dict) -> Event | None:
        event = payload.get("event") or {}
        status = _CLICKSIGN_EVENTS.get(str(event.get("name", "")).strip().lower())
        if status is None:
            return None
        document = payload.get("document") or {}
        signer = (event.get("data") or {}).get("user") or {}
        # A chave da lista (`request_signature_key`) é o que casa 1:1 com a SignatureRequest;
        # quando ela não vem, o par documento + e-mail do signatário resolve.
        signature_key = ""
        for entry in document.get("signers") or []:
            if str(entry.get("email", "")).lower() == str(signer.get("email", "")).lower():
                signature_key = str(entry.get("request_signature_key", ""))
                break
        return Event(
            status=status,
            provider_ref=signature_key,
            document_ref=str(document.get("key", "")),
            signer_email=str(signer.get("email", "")),
        )

    def _create_signature_request(  # pragma: no cover - I/O com o fornecedor
        self, document: Document, signer_email: str, content: bytes
    ) -> SignatureRef:
        base = (settings.ESIGN_API_BASE or self.DEFAULT_BASE).rstrip("/")
        token = settings.ESIGN_API_TOKEN
        mime = "application/pdf" if document.original_name.lower().endswith(".pdf") else "text/plain"
        encoded = base64.b64encode(content).decode()
        created = _http(
            f"{base}/api/v1/documents?access_token={token}",
            {
                "document": {
                    "path": f"/{document.original_name}",
                    "content_base64": f"data:{mime};base64,{encoded}",
                }
            },
            method="POST",
        )
        document_ref = str(((created or {}).get("document") or {}).get("key", ""))
        if not document_ref:
            return SignatureRef()
        signer = _http(
            f"{base}/api/v1/signers?access_token={token}",
            {"signer": {"email": signer_email, "auths": ["email"]}},
            method="POST",
        )
        signer_key = str(((signer or {}).get("signer") or {}).get("key", ""))
        if not signer_key:
            return SignatureRef(document_ref=document_ref)
        linked = _http(
            f"{base}/api/v1/lists?access_token={token}",
            {"list": {"document_key": document_ref, "signer_key": signer_key, "sign_as": "sign"}},
            method="POST",
        )
        listed = (linked or {}).get("list") or {}
        return SignatureRef(
            provider_ref=str(listed.get("request_signature_key", "")),
            document_ref=document_ref,
            sign_url=str(listed.get("url", "")),
        )


_PROVIDERS: dict[str, type] = {
    "autentique": AutentiqueProvider,
    "clicksign": ClicksignProvider,
}


def get_provider() -> Provider:
    """Adaptador escolhido por `ESIGN_PROVIDER`; sem um reconhecido, o `NullProvider`."""
    provider = _PROVIDERS.get(settings.ESIGN_PROVIDER.strip().lower())
    return provider() if provider else NullProvider()


# --- Saída (Biahflow → fornecedor) ------------------------------------------


def has_provider() -> bool:
    """Há fornecedor homologado, ou estamos no registro local do `NullProvider`?"""
    return not isinstance(get_provider(), NullProvider)


def send_for_signature(document: Document, signers: Sequence[Signer]) -> list[SignatureRef]:
    """Envia o documento para assinatura e devolve uma referência **por signatário**.

    Levanta `EsignProviderError` quando **havia um fornecedor** e ele não devolveu referência: sem
    `provider_ref` a solicitação não existe do lado dele, e gravá-la aqui produziria uma assinatura
    que ninguém assina, que o webhook nunca fecha (não há o que casar) e que o lembrete ainda
    cobraria de uma pessoa de verdade — sem link, porque `sign_url` também vem vazio.

    A guarda vale **por signatário** desde a issue #115: numa rodada de três, dois voltarem e um não
    é exatamente o caso em que gravar o que voltou deixa o documento pendente para sempre — a rodada
    nunca fecha, porque a solicitação que falta não tem como ser assinada.

    Sem fornecedor, referência vazia é **correta**: o `NullProvider` registra a intenção e o
    `mark-signed` manual é o caminho previsto. A distinção é toda esta função.

    **A rodada é um fato nosso, e por isso ela é cunhada aqui quando o fornecedor não a dá.** Ela
    nasce no instante em que a casa pede as assinaturas, e só *coincide* com o `id` do
    `createDocument` quando existe um fornecedor para emiti-lo. Deixar `document_ref` vazio jogaria
    todas as solicitações do documento na mesma rodada (`Document.rodada_assinada`), e um documento
    recusado, reenviado e assinado à mão nunca mais contaria como assinado — no modo que o
    `mark-signed` manual existe para servir.
    """
    refs = list(get_provider().send(document, signers))
    if has_provider():
        # `strict=True`: adaptador que devolve lista de tamanho diferente do pedido é defeito de
        # programação, e o par signatário↔referência abaixo já estaria trocado.
        for signer, ref in zip(signers, refs, strict=True):
            if not ref.provider_ref:
                raise EsignProviderError(
                    f"{settings.ESIGN_PROVIDER} não devolveu referência para {document.pk} "
                    f"({signer.role})"
                )
    if refs and not any(ref.document_ref for ref in refs):
        # O prefixo é o que torna a referência auto-explicativa no banco e impede colisão com um
        # id de fornecedor. `not any`, e não `not all`: os signatários de uma chamada vêm do mesmo
        # `createDocument` e ou todos têm a referência, ou nenhum tem.
        rodada = f"local:{uuid.uuid4().hex}"
        refs = [replace(ref, document_ref=rodada) for ref in refs]
    return refs


def _document_bytes(document: Document) -> bytes:
    """Conteúdo do arquivo, venha ele do Drive ou do storage local (mesma regra do download)."""
    if document.drive_file_id:
        # Mesmo download da action de documento, mesmo tratamento: falha do Drive é do fornecedor.
        try:
            return drive.download_document(document).read()
        except drive.DriveProviderError as exc:
            raise DriveUnavailable() from exc
    if not document.file:
        return b""
    with document.file.open("rb") as handle:
        return handle.read()


def _mail_signer(document: Document, signature: SignatureRequest, subject: str, lead: str) -> None:
    """E-mail do portal ao signatário, com o link quando somos nós que entregamos."""
    link = f"\nAssine aqui: {signature.sign_url}" if signature.sign_url else ""
    send_mail(
        f"{subject} — {document.original_name}",
        f"{lead}{link}",
        None,
        [signature.signer_email],
        fail_silently=True,
    )


def invite_signer(document: Document, signature: SignatureRequest) -> bool:
    """Convida o signatário quando a entrega é nossa (`ESIGN_DELIVERY=link`).

    Na entrega por e-mail quem convida é o fornecedor — o portal não duplica o aviso.
    """
    if not delivers_by_link() or not signature.sign_url:
        return False
    _mail_signer(
        document,
        signature,
        "Documento para assinatura",
        f"Você tem um documento para assinar: '{document.original_name}'.",
    )
    return True


def remind_pending(document: Document) -> int:
    """Envia lembrete a cada signatário ainda pendente do documento e retorna quantos.

    Com provedor homologado o status chega por webhook; o lembrete continua sendo a
    ferramenta de quem acompanha a assinatura (e o único laço quando não há provedor).
    """
    from .models import SignatureRequest

    pending = document.signature_requests.filter(status=SignatureRequest.Status.PENDING)
    reminded = 0
    for signature in pending:
        _mail_signer(
            document,
            signature,
            "Lembrete de assinatura",
            f"Consta pendente a sua assinatura do documento '{document.original_name}'.\n"
            f"Por favor, conclua a assinatura eletrônica.",
        )
        signature.reminded_at = timezone.now()
        signature.save(update_fields=["reminded_at"])
        reminded += 1
    return reminded


# --- Entrada (fornecedor → Biahflow) ----------------------------------------


def find_signature(event: Event) -> SignatureRequest | None:
    """Casa o evento com a solicitação: pela referência do fornecedor, ou documento + e-mail."""
    from .models import SignatureRequest

    if event.provider_ref:
        found = SignatureRequest.objects.filter(provider_ref=event.provider_ref).first()
        if found is not None:
            return found
    if event.document_ref and event.signer_email:
        return SignatureRequest.objects.filter(
            document_ref=event.document_ref, signer_email__iexact=event.signer_email
        ).first()
    return None


def email_da_contraparte(document: Document, document_ref: str | None) -> str:
    """O e-mail da **parte contratante** de uma rodada — nunca o de quem assinou por último.

    Enquanto o único signatário era o cliente, "quem assinou por último" e "o cliente" eram a mesma
    pessoa. Com a casa e uma testemunha na mesma rodada deixaram de ser, e o que dependia daquele
    atalho passava a apontar para quem não vai marcar Discovery nenhum. São **dois** os sítios que
    dependiam: o convite por e-mail (`apply_decision`, abaixo) e o convidado do evento no Google
    (`views._discovery_attendee`) — daí esta função ser pública e não haver duas consultas parecidas.

    Sem `counterparty` na rodada — o que não deveria acontecer, porque a view sempre cria ao menos
    um — devolve vazio e diz por quê no log: quem chama trata a ausência, e nenhum dos dois pode
    levantar por causa disso.
    """
    from .models import SignatureRequest

    if document_ref is None:
        return ""
    contraparte = (
        document.signature_requests.filter(
            document_ref=document_ref,
            signer_role=SignatureRequest.SignerRole.COUNTERPARTY,
        )
        .order_by("id")
        .first()
    )
    if contraparte is None:
        logger.warning(
            "rodada %s do documento %s não tem signatário «counterparty»; "
            "quem depende do endereço da contraparte fica sem ele",
            document_ref or "(sem referência)", document.pk,
        )
        return ""
    return contraparte.signer_email


def _close_contract_artifacts(document, signature_status: str) -> None:  # type: ignore[no-untyped-def]
    """Fecha o artefato de contrato ligado ao documento com a decisão do signatário (FDD 016).

    **A assimetria é deliberada: aceitar exige todos, recusar exige um.** Uma assinatura basta para
    o contrato estar recusado — não há o que esperar depois de alguém dizer não. Aceitar, não: com
    a casa, o cliente e a testemunha na mesma rodada, marcar `ACCEPTED` na primeira assinatura faria
    o contrato ser aceito pela assinatura da **própria casa**, antes de o cliente abrir o link.
    """
    from .models import Artifact, SignatureRequest

    if signature_status == SignatureRequest.Status.SIGNED:
        if not document.is_signed:
            return
        decision = Artifact.Status.ACCEPTED
    elif signature_status == SignatureRequest.Status.DECLINED:
        decision = Artifact.Status.REJECTED
    else:
        return
    contracts = document.artifacts.filter(
        kind=Artifact.Kind.CONTRACT, archived_at__isnull=True
    ).exclude(status=decision)
    for contract in contracts:
        contract.status = decision
        contract.save(update_fields=["status", "decided_at", "updated_at"])


def apply_decision(signature_pk: int, new_status: str) -> SignatureRequest:
    """Aplica a decisão do signatário — o único lugar onde uma assinatura se conclui.

    Recebe **pk** e não a instância já lida, de propósito: a trava é parte da operação, e uma
    API que aceitasse o objeto já carregado permitiria chamá-la sem travar nada. Mesmo par
    `atomic` + `select_for_update` de `convert_to_project` (`views.py`) — o lock na linha é o
    que substitui a unicidade que o banco dava de graça, e é o que serializa duas entregas
    simultâneas do mesmo evento em vez de deixar as duas passarem pela guarda de idempotência
    ao mesmo tempo.

    Idempotente: reentrega do webhook, ou um segundo clique em "marcar como assinado", não muda
    nada e não repete efeito nenhum.
    """
    from . import design_partner, discovery_booking
    from .models import SignatureRequest

    engagement = None
    with transaction.atomic():
        signature = SignatureRequest.objects.select_for_update().get(pk=signature_pk)
        if signature.status == new_status:
            return signature
        signature.status = new_status
        if new_status == SignatureRequest.Status.SIGNED:
            signature.signed_at = timezone.now()
        signature.save(update_fields=["status", "signed_at"])
        document = signature.document
        # O contrato do documento acompanha a decisão do signatário (FDD 016), ainda dentro da
        # transação: é escrita no banco, não efeito externo — mesmo lugar de `seed_work_items`
        # em `convert_to_project`, pelo mesmo motivo.
        _close_contract_artifacts(document, new_status)
        # O mandato de Design Partner nasce do mesmo jeito, e pelo mesmo motivo: assinatura de
        # recusa não abre engagement nenhum.
        if new_status == SignatureRequest.Status.SIGNED:
            engagement = design_partner.abrir_engagement_do_acordo(document)

    # Fora da transação: `notifications.notify` espelha por e-mail quando a flag `email` está
    # ligada, o mesmo padrão de `kickoff.finalize` (chamado fora do `atomic` em `views.py`).
    label = "assinou" if new_status == SignatureRequest.Status.SIGNED else "recusou assinar"
    notifications.notify(
        [document.uploaded_by],
        "esign",
        f"{signature.signer_email} {label} o documento {document.original_name}.",
        "/documentos",
    )
    if engagement is not None:
        notifications.notify(
            [document.uploaded_by],
            "engagement",
            f"O acordo assinado por {engagement.account.name} abriu um mandato de Design Partner.",
            f"/contas/{document.account_id}",
        )
        # E o cliente recebe o link para marcar o Discovery — aqui, fora da transação e junto do
        # aviso interno, porque é o mesmo degrau: o mandato nasceu. Best-effort de propósito
        # (`discovery_booking.enviar_convite` nunca levanta): a assinatura já está aplicada e o
        # webhook do fornecedor reentregaria em laço um evento que já teve efeito.
        discovery_booking.enviar_convite(
            engagement, email_da_contraparte(document, signature.document_ref)
        )
    return signature


def apply_event(event: Event) -> SignatureRequest | None:
    """Aplica o status do fornecedor à solicitação. Idempotente: reentrega não muda nada.

    Retorna a `SignatureRequest` afetada (mesmo quando já estava no status alvo) ou `None`
    quando o evento não corresponde a nenhuma solicitação conhecida.
    """
    signature = find_signature(event)
    if signature is None:
        return None
    return apply_decision(signature.pk, event.status)


def _http(  # pragma: no cover - I/O com o fornecedor
    url: str, payload: dict, method: str
) -> dict | None:
    return _http_raw(
        url, json.dumps(payload).encode(), {"Content-Type": "application/json"}, method=method
    )


def _multipart(fields: Mapping[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    """Corpo `multipart/form-data` (upload GraphQL) e o Content-Type com o boundary.

    Formato do graphql-multipart-request-spec: os campos `operations` e `map` descrevem a
    mutation e onde o arquivo entra nas variáveis.
    """
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode()
            + b"\r\n"
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _http_raw(  # pragma: no cover - I/O com o fornecedor
    url: str, body: bytes, headers: Mapping[str, str], method: str = "POST"
) -> dict | None:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            raw = response.read()
        return json.loads(raw) if raw else None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("Falha ao falar com o fornecedor de assinatura (%s): %s", url, exc)
        return None
