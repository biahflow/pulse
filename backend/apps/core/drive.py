"""Integração com o Google Drive (conta de serviço + Shared Drive).

Organiza os documentos por conta usando a **finalidade** (``Document.kind``):
``[raiz]/{Conta}/{Contratos|Propostas|NDAs|Acordos de Design Partner|Outros}/arquivo``, mais
``{Conta}/Projetos/{Projeto}`` para a pasta que o kickoff garante (issue #113). Até aqui o
critério era o **vínculo** do documento (conta/oportunidade/projeto, o método PARA) — mas o
vínculo nunca disse para que o documento serve, só onde ele nasceu; um Contrato Comercial preso a
uma oportunidade e outro já solto na conta (`account`, pós-conversão) caíam em pastas diferentes
apesar de serem o mesmo tipo de papel. `Document.kind` passou a existir e responde à pergunta
certa. Documento já enviado ao Drive **não é movido**: a estrutura antiga continua valendo para
quem já está lá (é operação sobre dado real de cliente, fora deste escopo — issue #113).
Quando ``GOOGLE_DRIVE_ENABLED`` está desligado, o app usa o storage local e nada
aqui é chamado. As funções que falam com a API do Google são finas e ficam fora da
cobertura de teste; a lógica de negócio (a pasta e a conta-dona) é testada.
"""

from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING

from django.conf import settings

from . import flags

if TYPE_CHECKING:
    from .models import Account, Document, Project

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveProviderError(Exception):
    """O Drive recusou: credencial, escopo, pasta inexistente, cota ou rede.

    Tipo **estreito** de propósito, como o `ai.AiProviderError` da rodada 2: quem chama envolve só
    a chamada de rede, e um erro de banco logo depois continua sendo 500 em vez de virar "o Google
    caiu" — que é exatamente o tipo de mentira que a FDD 024 existe para evitar.
    """

# O método PARA saiu (issue #113): as duas constantes de subpasta por vínculo (conta e
# oportunidade) classificavam pelo lugar de origem, critério que a finalidade substitui. A
# subpasta "4-Arquivo" saiu por um motivo à parte — nunca teve chamador: a função de bucket
# anterior jamais a retornava, e a issue #113 a descrevia como "destino de documento arquivado"
# por engano. Mesma dívida da issue #110 (classe sem consumidor), em escala menor.
PASTA_DE_PROJETOS = "Projetos"

# Nome de pasta em plural, num mapa próprio — e não o rótulo cru do `Document.Kind`, que é
# singular e serve a outra superfície (o `<select>` de finalidade). É uma segunda definição do
# rótulo, e o custo disso é pago pelo teste de exaustividade em `tests/test_drive.py`: ele reprova
# se um `Kind` novo não tiver entrada aqui, o que impede a divergência silenciosa.
#
# Chaveado por **string literal** (e não por `Document.Kind.…`) de propósito: `drive.py` só
# importa `Document` sob `TYPE_CHECKING` (linhas 20-21) para não criar ciclo — `models.py` não
# importa `drive`, mas um `dict` de módulo com chave `Document.Kind.…` exigiria o import de
# `models` em tempo de execução no topo do arquivo, e a alternativa (montar o mapa dentro de uma
# função com import local, como `_service()` faz) pagaria esse custo a cada chamada por nada: as
# chaves já são exatamente os `value` de `Document.Kind`, e a correspondência entre os dois é
# provada pelo teste de exaustividade, não pelo tipo do Python.
PASTA_POR_FINALIDADE: dict[str, str] = {
    "commercial_contract": "Contratos",
    "proposal": "Propostas",
    "nda": "NDAs",
    "design_partner_agreement": "Acordos de Design Partner",
}
PASTA_SEM_FINALIDADE = "Outros"


# O id de uma pasta/Shared Drive só aparece **dentro** da URL, que é de onde a pessoa o copia —
# então colar a URL inteira é o erro natural, não descuido. Observado na rodada 3 da homologação
# (FDD 024): sem isto, o valor colado vira um 404 do Drive que se parece com falta de permissão, e
# manda quem opera depurar a coisa errada.
_URL_DE_PASTA = re.compile(r"/folders/([A-Za-z0-9_-]+)")


def is_enabled() -> bool:
    return flags.is_enabled("drive")


def parse_root_folder_id(valor: str) -> str:
    """Aceita o id da pasta **ou** a URL de onde ele foi copiado.

    O que não casa com o padrão volta como veio: não inventamos id — quem reclama de um valor
    inválido é a sonda do `check_integrations`, que pergunta ao Drive.
    """
    valor = (valor or "").strip()
    achado = _URL_DE_PASTA.search(valor)
    return achado.group(1) if achado else valor


def root_folder_id() -> str:
    return parse_root_folder_id(settings.GOOGLE_DRIVE_ROOT_FOLDER_ID)


def pasta_do_documento(document: Document) -> str:
    """Subpasta de acordo com a finalidade do documento (`Document.kind`), não o vínculo."""
    return PASTA_POR_FINALIDADE.get(document.kind, PASTA_SEM_FINALIDADE)


def account_of(document: Document) -> Account | None:
    """Conta-dona do documento, seguindo o vínculo (conta, oportunidade ou projeto)."""
    if document.account_id:
        return document.account
    opportunity = (
        document.commercial_opportunity if document.commercial_opportunity_id else None
    )
    if opportunity:
        return opportunity.account
    project = document.project if document.project_id else None
    if project:
        return project.engagement.account
    return None


def _service():  # pragma: no cover - I/O com a API do Google
    from googleapiclient.discovery import build

    from . import google_auth

    return build(
        "drive", "v3", credentials=google_auth.credentials([DRIVE_SCOPE]), cache_discovery=False
    )


def _find_folder(service, name: str, parent: str) -> str | None:  # pragma: no cover - I/O
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and '{parent}' in parents "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        )
        .execute()
    )
    folders = response.get("files", [])
    return folders[0]["id"] if folders else None


def _create_folder(service, name: str, parent: str) -> str:  # pragma: no cover - I/O
    metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
    folder = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return folder["id"]


def _ensure_subfolder(service, name: str, parent: str) -> str:  # pragma: no cover - I/O
    return _find_folder(service, name, parent) or _create_folder(service, name, parent)


def _ensure_account_folder(service, account: Account) -> str:  # pragma: no cover - I/O
    if account.drive_folder_id:
        return account.drive_folder_id
    folder_id = _ensure_subfolder(service, account.name, root_folder_id())
    account.drive_folder_id = folder_id
    account.save(update_fields=["drive_folder_id", "updated_at"])
    return folder_id


def ensure_project_folder(project: Project) -> str:  # pragma: no cover - I/O
    """Garante ``{Conta}/Projetos/{Projeto}`` e persiste o id no projeto.

    No-op (retorna "") quando o Drive está desligado, para o kickoff seguir sem I/O.
    """
    if not is_enabled():
        return ""
    if project.drive_folder_id:
        return project.drive_folder_id
    service = _service()
    account_folder = _ensure_account_folder(service, project.engagement.account)
    bucket_folder = _ensure_subfolder(service, PASTA_DE_PROJETOS, account_folder)
    folder_id = _ensure_subfolder(service, project.name, bucket_folder)
    project.drive_folder_id = folder_id
    project.save(update_fields=["drive_folder_id", "updated_at"])
    return folder_id


def upload_document(document: Document, uploaded_file) -> tuple[str, str]:  # pragma: no cover - I/O
    """Sobe o arquivo para ``{Conta}/{pasta da finalidade}`` e retorna ``(file_id, link)``.

    Traduz a falha do fornecedor para `DriveProviderError`, como `download_document` — e é isso que
    permite ao chamador estreitar o `except`. Enquanto esta função levantava a família crua do SDK,
    o `DocumentSerializer.create` só tinha o `except Exception` genérico à disposição, e ele
    devolvia como "o Drive está fora" também o defeito **nosso**: a mentira sobre a origem do
    problema que a FDD 024 existe para evitar.

    **O que fica de fora do `try` é deliberado.** Documento sem conta-dona é defeito daqui, não
    recusa do Google — o `Document.clean()` e o `DocumentSerializer.validate` já garantem o vínculo
    único, então chegar aqui com `None` significa que alguém contornou os dois, e isso merece 500 e
    não 502. Ler o arquivo local também não é conversa com o Google, e por isso acontece antes de
    ela começar. O `account.save()` de `_ensure_account_folder`, ao contrário, fica **dentro**: ele
    grava o id que o Drive acabou de dar, e sem esse id não existe upload — falhar ali é falhar
    esta operação.
    """
    from googleapiclient.http import MediaIoBaseUpload

    account = account_of(document)
    if account is None:
        raise ValueError("Documento sem conta-dona para o Drive.")
    media = MediaIoBaseUpload(
        io.BytesIO(uploaded_file.read()),
        mimetype=getattr(uploaded_file, "content_type", None) or "application/octet-stream",
        resumable=False,
    )
    try:
        service = _service()
        account_folder = _ensure_account_folder(service, account)
        bucket_folder = _ensure_subfolder(service, pasta_do_documento(document), account_folder)
        created = (
            service.files()
            .create(
                body={"name": document.original_name, "parents": [bucket_folder]},
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        # A leitura da resposta fica dentro: resposta sem `id` é o fornecedor descumprindo o
        # contrato dele, e um `KeyError` cru ali viraria 500 nosso por um erro que não é nosso.
        return created["id"], created.get("webViewLink", "")
    except Exception as exc:  # noqa: BLE001 - a família do SDK vira um tipo só
        raise DriveProviderError(str(exc) or exc.__class__.__name__) from exc


def download_document(document: Document) -> io.BytesIO:  # pragma: no cover - I/O
    """Baixa o conteúdo do arquivo pela conta de serviço, preservando o RBAC do app.

    Traduz qualquer falha para `DriveProviderError`. A FDD 024 blindou o **upload** e deixou esta
    crua: era 500 mudo no caminho em que a pessoa tenta pegar de volta o próprio arquivo.
    """
    from googleapiclient.http import MediaIoBaseDownload

    try:
        service = _service()
        request = service.files().get_media(fileId=document.drive_file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except Exception as exc:  # noqa: BLE001 - a família do SDK vira um tipo só
        raise DriveProviderError(str(exc) or exc.__class__.__name__) from exc
    buffer.seek(0)
    return buffer


def delete_document(document: Document) -> None:  # pragma: no cover - I/O
    """Apaga o arquivo no Drive. Usado pelo expurgo de retenção (LGPD).

    Traduz a falha para `DriveProviderError` como as demais, e **não** engole: quem chama precisa
    saber que o conteúdo continua lá, senão apagaria a linha e deixaria o arquivo órfão — que é o
    pior resultado possível de um expurgo.

    **Com uma exceção, e ela é a regra e não o atalho: 404 é sucesso.** Apagar o que já não existe
    *é* o estado desejado, e um `DELETE` idempotente é o comportamento correto. Sem isto, um arquivo
    que alguém removeu pela interface do Google prendia a linha para sempre — toda execução do
    expurgo falhava igual, e o dado pessoal ficava impossível de esquecer, que é o oposto do que a
    ADR 0017 garante. Nenhuma outra falha entra aqui: credencial ausente **não** é "já foi apagado",
    e confundir as duas apagaria o índice deixando o conteúdo.

    O `pragma` cobre a chamada de rede, não a classificação: os dois ramos do `except` têm teste em
    `tests/test_drive.py`, com um `_service` falso que levanta 404 e 403. A regra de negócio saiu de
    dentro da função fina e por isso precisou de rede própria.
    """
    try:
        _service().files().delete(fileId=document.drive_file_id, supportsAllDrives=True).execute()
    except Exception as exc:  # noqa: BLE001 - a família do SDK vira um tipo só
        if getattr(getattr(exc, "resp", None), "status", None) == 404:
            return
        raise DriveProviderError(str(exc) or exc.__class__.__name__) from exc
