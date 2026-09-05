from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any, cast

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import QueryDict
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from . import (
    blueprints,
    discovery_booking,
    discovery_questions,
    drive,
    esign,
    kickoff,
    knowledge,
    publication,
)
from . import process as process_module
from .document import conteudo_e_pdf
from .exceptions import DriveUnavailable
from .models import (
    ARTIFACT_TRANSITIONS,
    CASE_TRANSITIONS,
    DOCUMENT_KINDS_QUE_ABREM_ENGAGEMENT,
    FINDING_TRANSITIONS,
    INVOICE_TRANSITIONS,
    KPI,
    Account,
    Activity,
    Artifact,
    BlueprintVariant,
    BusinessCase,
    Case,
    CobrancaSuspensao,
    CommercialOpportunity,
    Contact,
    Decisao,
    DigitalEmployee,
    DigitalEmployeeBlueprint,
    Discovery,
    DiscoverySession,
    Document,
    DunningContact,
    Engagement,
    EngineeringHandoff,
    Evidence,
    FeasibilityAssessment,
    Finding,
    GithubDeliveryProjection,
    ImprovementOpportunity,
    Invitation,
    Invoice,
    JourneyPhase,
    KnowledgeArea,
    KnowledgePiece,
    Lead,
    Measurement,
    Meeting,
    Milestone,
    Notification,
    PainPoint,
    Pendencia,
    PhaseChecklistItem,
    PhaseDeliverable,
    PhaseEvent,
    PipelineStage,
    PriorityAssessment,
    Process,
    ProcessObservation,
    ProcessStep,
    Project,
    ProjectChecklistItem,
    ProjectDeliverable,
    ProjectMember,
    ProjectPhase,
    ProveExperiment,
    Qualification,
    Risco,
    SatisfactionRecord,
    Service,
    SignatureRequest,
    SolutionHypothesis,
    Task,
    User,
    ValueLedgerEntry,
    Vertical,
)
from .openapi_aliases import ALIASES_DEPRECIADOS, CANONICO_DA_CHAVE
from .priority import FORMULAS, ranking_da_conta
from .prove import baseline_de, o_que_falta_para_iniciar, outcome_mais_recente_de
from .versioning import V2, frase_da_chave_removida, frase_da_chave_sem_sucessora, versao_de

logger = logging.getLogger(__name__)


def _corpo_mutavel(data: Any) -> Any:
    """Cópia **rasa** do corpo, mutável, preservando a natureza dele.

    `QueryDict.copy()` é `deepcopy` por contrato, e o corpo do `POST /documents/` carrega o
    arquivo enviado: copiá-lo em profundidade duplicaria 10 MB — e o `deepcopy` de um
    `TemporaryUploadedFile` nem chega a terminar. Aqui as listas de valores são recriadas e os
    valores, inclusive o arquivo, são referenciados.

    Continua sendo `QueryDict` quando o corpo é `QueryDict`: o `getlist` é o que faz a DRF tratar
    a entrada como HTML (`html.is_html_input`), e devolver um `dict` mudaria a leitura de campo
    vazio e de lista no formulário.
    """
    if not hasattr(data, "getlist"):
        return dict(data)
    copia = QueryDict(mutable=True)
    encoding = getattr(data, "encoding", None)
    if encoding:
        copia.encoding = encoding
    for chave in data:
        copia.setlist(chave, data.getlist(chave))
    return copia


def _versao_do_contexto(serializer: Any) -> str:
    """A versão da requisição que carrega este serializer — `v1` quando não há requisição.

    Um lugar só, e não `context.get("request")` espalhado por vinte serializers: o fallback é a
    metade que importa, porque um serializer instanciado **fora** de requisição (o portal, os
    agentes, um teste de unidade) tem de manter a forma de sempre. Perder as chaves legadas por
    omissão seria a v2 vazando para quem nunca a pediu.
    """
    return versao_de(serializer.context.get("request"))


def _componente_do_serializer(serializer: Any) -> str:
    """O nome do componente do OpenAPI — a chave de `ALIASES_DEPRECIADOS`.

    É a mesma derivação do drf-spectacular (o nome da classe sem o sufixo `Serializer`), e
    `COMPONENTE_OPENAPI` é a saída para quem não segue o padrão. Sem esta correspondência o mapa
    dirigiria a depreciação no esquema e nada mais — e a v2 continuaria emitindo o que o contrato
    diz que ela não tem.
    """
    declarado = getattr(serializer, "COMPONENTE_OPENAPI", "")
    if declarado:
        return str(declarado)
    return type(serializer).__name__.removesuffix("Serializer")


# A camada de compatibilidade da `/api/v1/`, sensível à versão (issue #122, fatia 1).
#
# A issue #67 renomeou o campo do modelo e a `docs/ontology/aliases.md` §2c manteve a chave de
# payload: quem integrou com a v1 não tem como saber que o nome mudou. A `/api/v2/` é o prazo que
# aquele documento sempre deu a essas chaves, e este mixin é onde as quatro coisas acontecem:
#
# * **escrita na v1** — a chave antiga é normalizada para a canônica antes da validação. Quando as
#   duas vêm no mesmo corpo, **a canônica vence**: um corpo com as duas é confusão do chamador, e
#   resolver pela nova é o que não trava quem já migrou (mesma regra de `apply-gate`).
# * **escrita na v2, por `ALIASES_DE_ENTRADA`** — a chave antiga é **recusada com 400 dizendo o
#   nome canônico**. Ignorar em silêncio é o default do DRF para chave desconhecida, e produziria
#   um `POST` que responde 201 sem ter gravado o vínculo — o modo de falha mudo que a issue #122
#   decidiu não ter.
# * **escrita na v2, por `ALIASES_DEPRECIADOS`** (fatia 3a) — a mesma recusa para as chaves
#   só-de-leitura, que nunca tiveram `ALIASES_DE_ENTRADA` porque não há tradução na v1 (o vínculo é
#   lavrado por uma action própria, ou a escrita já parou antes da v2 existir, §2d). Sem isto,
#   `POST /api/v2/projects/` com `client` voltaria a ser o campo `read_only` ignorando em silêncio
#   — o mesmo modo de falha mudo, só que pela porta que `ALIASES_DE_ENTRADA` não cobre. O nome
#   canônico de cada uma (ou a ausência dele, §2d) vem de `openapi_aliases.CANONICO_DA_CHAVE`.
# * **leitura na v2** — as chaves-alias somem da representação, e quem diz quais são é
#   `openapi_aliases.ALIASES_DEPRECIADOS`, o **mesmo** mapa que marca `deprecated: true` no
#   esquema. Uma segunda lista por serializer divergiria da primeira em silêncio, e o contrato
#   passaria a prometer uma ausência que não acontece.
#
# Um mecanismo só, e não um `if` por serializer: o décimo oitavo esquece, e o defeito não deixa
# nada vermelho aqui dentro — é o modo de falha que a ADR 0026 descreve para as primitivas de UI.
#
# **Comentário e não docstring, e a razão é medida.** A regra do `CLAUDE.md` fala de mixin de
# viewset, mas o drf-spectacular usa a docstring do **serializer** como `description` do
# componente do mesmo jeito: com a docstring aqui, este raciocínio interno aparecia nos schemas
# `Document`, `Activity`, `Artifact` e nos `Patched*` deles — blocos de texto sobre a issue #67
# virando contrato público em `openapi.yaml`.
class AliasesDaV1Mixin:

    ALIASES_DE_ENTRADA: dict[str, str] = {}

    # A segunda tabela, e ela traduz **valor**, não chave (issue #122, fatia 5.1) — campo →
    # {valor_legado: valor_canônico}. `ALIASES_DE_ENTRADA` acima resolve "o campo mudou de nome"
    # (`client` → `account`); esta resolve "o campo é o mesmo, e o que persiste dentro dele mudou de
    # idioma" (`DigitalEmployeeBlueprint.area`: `comercial` → `commercial`, D10 do language-map). As
    # duas precisam de mapas separados porque a v2 as trata diferente: chave errada não tem onde
    # cair — o DRF ignoraria em silêncio — e por isso ganha 400 dedicado
    # (`versioning.frase_da_chave_removida`); valor errado cai sozinho na validação de `choices` do
    # campo, que **já é** um 400 listando o vocabulário inteiro. Escrever uma frase nossa para o
    # valor seria a segunda definição do mesmo erro que o DRF já produz de graça — por isso a v2 não
    # ganha recusa dedicada aqui, só a v1 traduz.
    VALORES_DE_ENTRADA: dict[str, dict[str, str]] = {}

    def to_internal_value(self, data: Any) -> Any:
        if not hasattr(data, "keys"):
            return super().to_internal_value(data)  # type: ignore[misc]
        if _versao_do_contexto(self) == V2:
            recusadas = {
                antiga: [frase_da_chave_removida(antiga, canonica)]
                for antiga, canonica in self.ALIASES_DE_ENTRADA.items()
                if antiga in data
            }
            # A metade que a fatia 1 não alcançava (issue #122, fatia 3a): as chaves só-de-leitura
            # de `ALIASES_DEPRECIADOS` nunca precisaram de `ALIASES_DE_ENTRADA` porque não há
            # tradução na v1 — o vínculo é lavrado por uma action própria, ou (§2d) a escrita já
            # parou antes da v2 existir. Mas mandá-las no corpo da v2 não pode voltar a ser
            # ignorado em silêncio: seria o mesmo modo de falha mudo que a decisão 3 da ADR 0066
            # recusa para `ALIASES_DE_ENTRADA`, só que pela porta que ela não cobria. O `if antiga
            # not in recusadas` evita recusar duas vezes a chave que está nos dois mapas (como
            # `ai_opportunity` em `Project`) com frases redundantes.
            for antiga in ALIASES_DEPRECIADOS.get(_componente_do_serializer(self), ()):
                if antiga in data and antiga not in recusadas:
                    canonica_ou_none = CANONICO_DA_CHAVE.get(antiga)
                    mensagem = (
                        frase_da_chave_removida(antiga, canonica_ou_none)
                        if canonica_ou_none is not None
                        else frase_da_chave_sem_sucessora(antiga)
                    )
                    recusadas[antiga] = [mensagem]
            if recusadas:
                raise serializers.ValidationError(recusadas)
            # `VALORES_DE_ENTRADA` não entra aqui: a v2 não traduz nem recusa o valor legado, ela
            # deixa a validação de `choices` do campo recusar sozinha (ver o comentário da tabela).
        else:
            atualizacoes: dict[str, Any] = {
                canonica: data[antiga]
                for antiga, canonica in self.ALIASES_DE_ENTRADA.items()
                if antiga in data and canonica not in data
            }
            for campo, valores_do_campo in self.VALORES_DE_ENTRADA.items():
                valor = data.get(campo)
                if valor in valores_do_campo:
                    atualizacoes[campo] = valores_do_campo[valor]
            if atualizacoes:
                data = _corpo_mutavel(data)
                for chave, valor in atualizacoes.items():
                    # Cópia e não `pop`: no par (chave, valor) a chave legada continua declarada
                    # como campo `read_only`, então o serializer a ignora — e `QueryDict.pop`
                    # devolveria a **lista** de valores, não o valor. No par (campo, valor) a
                    # atribuição é uma sobrescrita no próprio campo, pelo mesmo motivo de mutação.
                    data[chave] = valor
        return super().to_internal_value(data)  # type: ignore[misc]

    def to_representation(self, instance: Any) -> Any:
        dados = super().to_representation(instance)  # type: ignore[misc]
        if _versao_do_contexto(self) != V2:
            return dados
        for chave in ALIASES_DEPRECIADOS.get(_componente_do_serializer(self), ()):
            dados.pop(chave, None)
        return dados


class UserSerializer(serializers.ModelSerializer[User]):
    # O mesmo predicado que o backend usa em 14 lugares (`RolePermission`, `visible_to`,
    # `agents`...), e não o `is_superuser` cru: assim o SPA **consome** a regra em vez de
    # reconstruí-la em TypeScript como `is_superuser || role === "admin"`, que seria uma segunda
    # expressão dela. Sem isto, `createsuperuser` — o primeiro comando de toda instalação — produz
    # alguém que a API trata como admin e a tela trata como Entrega (FDD 017, ADR 0010).
    is_admin = serializers.BooleanField(source="is_admin_role", read_only=True)
    # O topbar precisa saber **se** existe foto para escolher entre a miniatura e as iniciais, e
    # o byte da foto não cabe aqui: ele sai pela rota autenticada `users/<id>/avatar/`, como o
    # download de documento (ADR 0002). `avatar_updated_at` acompanha porque é o que muda a `src`
    # do `<img>` quando a pessoa troca a foto — sem ele o navegador seguiria mostrando a anterior.
    has_avatar = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "role", "is_admin",
                  "has_avatar", "avatar_updated_at"]
        # `is_admin` explicitamente aqui também: este serializer é o de **leitura** e nenhum
        # endpoint de escrita o usa — a escrita de perfil próprio tem o seu, logo abaixo, com
        # allowlist de dois campos. Os demais campos daqui são graváveis, e o dia em que alguém o
        # pendurar num viewset de escrita não pode ser o dia em que virou caminho de promoção.
        read_only_fields = ["id", "is_admin", "has_avatar", "avatar_updated_at"]

    def get_has_avatar(self, obj: User) -> bool:
        return bool(obj.avatar)


# Comentário e não docstring: o drf-spectacular usa a docstring do serializer como `description`
# do schema, e o raciocínio abaixo é interno — no `openapi.yaml` ele vira contrato público com
# nome de teste dentro. Mesma regra que vale para a docstring de viewset.
#
# **Serializer separado, allowlist de dois campos.** Não é o `UserSerializer` acima com um
# `read_only_fields` maior, e a diferença não é de estilo: aquele tem `role` gravável, então
# reutilizá-lo aqui faria um `PATCH` com `{"role": "admin"}` promover quem o mandou. Uma allowlist
# só se rompe por adição deliberada; uma denylist se rompe por esquecimento, no dia em que um campo
# novo entrar no modelo.
#
# O `ModelSerializer` descarta chave que não esteja em `fields`, então `role`, `is_superuser`,
# `is_staff`, `is_active`, `email`, `username` e `id` não chegam a `validated_data`. Coberto por
# `test_entrega_mandando_role_admin_nao_vira_admin`.
class ProfileSerializer(serializers.ModelSerializer[User]):
    """Nome e sobrenome do próprio usuário."""

    class Meta:
        model = User
        fields = ["first_name", "last_name"]


# Foto de perfil: 2 MB e três tipos, conferidos **no servidor** (a checagem do `<input accept>`
# é conveniência de tela, não controle). O limite é menor que o do documento porque o consumidor
# é uma miniatura de 72px, e o arquivo volta a ser servido pela nossa própria origem.
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}


def _avatar_magic_matches(extension: str, head: bytes) -> bool:
    """Os bytes iniciais batem com a extensão declarada?

    A extensão sozinha não basta: o arquivo volta a ser servido pela rota da foto **sob a origem
    do portal**, e um `.png` que na verdade é HTML seria XSS armazenada. O `nosniff` da rota é a
    segunda tranca; esta é a primeira, e recusa antes de gravar. O WebP precisa dos dois pedaços
    — `RIFF` no começo e `WEBP` no byte 8 —, porque `RIFF` sozinho é também WAV e AVI.
    """
    if extension in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    return head.startswith(b"RIFF") and head[8:12] == b"WEBP"


# Mesmo desenho do `DocumentSerializer.validate`: tamanho e tipo, conferidos no servidor.
class ProfileAvatarSerializer(serializers.Serializer):
    """Foto de perfil: JPG, PNG ou WebP, até 2 MB."""

    avatar = serializers.FileField()

    def validate_avatar(self, value: UploadedFile) -> UploadedFile:
        if (value.size or 0) > AVATAR_MAX_BYTES:
            raise serializers.ValidationError("A imagem excede o limite de 2 MB.")
        extension = os.path.splitext(value.name or "")[1].lower()
        if extension not in AVATAR_CONTENT_TYPES:
            raise serializers.ValidationError("Envie uma imagem JPG, PNG ou WebP.")
        head = value.read(16)
        value.seek(0)
        if not _avatar_magic_matches(extension, head):
            raise serializers.ValidationError("O arquivo não é uma imagem JPG, PNG ou WebP.")
        return value


# A regra de força é a **mesma** do aceite de convite (`AcceptInvitationSerializer`): os
# validadores configurados do Django, com o `user` em mãos para que o
# `UserAttributeSimilarityValidator` tenha o que comparar — sem ele esse validador desiste e a
# regra vira metade dela. Comentário e não docstring: ver `ProfileSerializer` acima.
class ChangePasswordSerializer(serializers.Serializer):
    """Troca da própria senha, conferindo a senha atual."""

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)
    new_password_confirm = serializers.CharField(write_only=True)

    def validate_current_password(self, value: str) -> str:
        user = cast(User, self.context["user"])
        if not user.check_password(value):
            raise serializers.ValidationError("A senha atual está incorreta.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError(
                {"new_password_confirm": "A confirmação não confere com a nova senha."}
            )
        try:
            validate_password(attrs["new_password"], user=cast(User, self.context["user"]))
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)}) from exc
        return attrs


class AccountSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Account]):
    ALIASES_DE_ENTRADA = {"status": "lifecycle_status"}

    vertical_name = serializers.CharField(source="vertical.name", read_only=True, default="")
    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): o campo virou
    # `lifecycle_status` e a chave `status` continua saindo com o mesmo valor, até a `/api/v2/`.
    # A escrita pela chave antiga vem do mixin acima.
    status = serializers.CharField(source="lifecycle_status", read_only=True)
    published_count = serializers.SerializerMethodField()

    class Meta:
        model = Account
        fields = ["id", "name", "legal_name", "tax_id", "owner", "lifecycle_status", "status",
                  "vertical", "vertical_name", "published_count", "created_at", "updated_at"]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]

    # O decorador é o mesmo de `get_projects_count`, e pelo mesmo motivo: sem ele o drf-spectacular
    # copia o docstring inteiro para a `description` do campo no `openapi.yaml`, e o raciocínio
    # interno vira contrato publicado.
    @extend_schema_field(serializers.IntegerField())
    def get_published_count(self, obj: Account) -> int:
        """Quantos registros do Discovery desta conta o cliente está vendo agora (issue `#114`).

        Quem decide o recorte é `publication.py`, e este método não o reexpressa — reescrever
        "publicado e vivo" aqui seria a segunda definição que aquele módulo existe para não ter.

        **Dois caminhos, e o segundo não é preciosismo.** Na listagem e no detalhe o valor chega
        anotado por `AccountViewSet.get_queryset` (uma consulta, não cinco por linha). Fora do
        viewset — a resposta do `POST`, um `Account` montado em teste, o serializer reusado por
        outro código — a anotação não existe, e ler `obj.published_count` direto levantaria
        `AttributeError`, ou seja, 500 num caminho que hoje funciona. Daí o `None` como sentinela
        em vez de `0`: conta sem nada publicado é `0` de verdade, e confundir os dois faria a
        ausência de anotação passar por resposta.
        """
        anotado = getattr(obj, "published_count", None)
        return publication.contagem_publicada(obj) if anotado is None else int(anotado)

    def validate_lifecycle_status(self, value: str) -> str:
        """O estado é afirmado por quem cadastra, mas o que o sistema observou não se desdiz.

        `prospect → active` é do signal `_promote_account_on_won`. Deixar um PATCH devolver a
        conta para prospect apagaria esse fato — e o signal só promove na transição, então ele
        não corrigiria de volta. O critério de "ganha" é o mesmo de `CommercialOpportunity.is_won`.

        **`active → inactive` é permitido**, e é o caminho que o estado existe para ter: a conta
        que terminou o mandato sai da carteira sem sair do histórico. `inactive → prospect` é
        recusado pela mesma razão de `active → prospect` — quem já foi cliente não volta a ser
        alguém que nunca comprou.
        """
        if value != Account.LifecycleStatus.PROSPECT or self.instance is None:
            return value
        if self.instance.lifecycle_status == Account.LifecycleStatus.INACTIVE:
            raise serializers.ValidationError(
                "A conta já foi cliente e não volta a ser prospect."
            )
        if CommercialOpportunity.objects.filter(
            account=self.instance, stage__kind=PipelineStage.Kind.WON, archived_at__isnull=True
        ).exists():
            raise serializers.ValidationError(
                "A conta tem oportunidade ganha e não volta a ser prospect."
            )
        return value


class ContactSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Contact]):
    ALIASES_DE_ENTRADA = {"client": "account"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)

    # Nome composto e só-leitura (issue #55, FDD 001): quem escreve manda `first_name`/
    # `last_name`, nunca este campo — mudança de contrato de escrita deliberada, registrada no
    # CHANGELOG. Lê de `Contact.full_name`, a única definição de "nome composto" (CLAUDE.md).
    name = serializers.ReadOnlyField(source="full_name")

    class Meta:
        model = Contact
        fields = ["id", "account", "client", "first_name", "last_name", "name", "email", "phone",
                  "job_title", "receives_billing", "created_at", "updated_at"]
        read_only_fields = ["id", "name", "created_at", "updated_at"]


class ActivitySerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Activity]):
    ALIASES_DE_ENTRADA = {"opportunity": "commercial_opportunity", "client": "account"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do mixin acima.
    opportunity = serializers.PrimaryKeyRelatedField(
        source="commercial_opportunity", read_only=True
    )
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    dunning_signal_display = serializers.CharField(
        source="get_dunning_signal_display", read_only=True
    )

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): as duas chaves antigas saem
    # com o mesmo valor da canônica e morrem na `/api/v2/`, junto do renome para `DunningSignal`
    # (issue #122, fatia 5.2). **Sem `ALIASES_DE_ENTRADA` nem `VALORES_DE_ENTRADA`**: o campo é
    # `read_only` — só `POST /activities/{id}/classificar/` grava — e não há caminho de escrita
    # para normalizar. A entrada em `ALIASES_DEPRECIADOS["Activity"]` (`openapi_aliases.py`) é o
    # que faz o mixin remover as duas na v2.
    cobranca_sinal = serializers.CharField(source="dunning_signal", read_only=True)
    cobranca_sinal_display = serializers.CharField(
        source="get_dunning_signal_display", read_only=True
    )

    class Meta:
        model = Activity
        fields = ["id", "account", "client", "commercial_opportunity", "opportunity", "invoice",
                  "kind", "kind_display", "happened_on",
                  "summary", "notes", "dunning_signal", "dunning_signal_display",
                  "cobranca_sinal", "cobranca_sinal_display", "owner",
                  "created_at", "updated_at"]
        # `dunning_signal` é só de leitura: ele é lavrado por `POST /activities/{id}/classificar/`,
        # que carrega a `AiInteraction` que o produziu. Um `PATCH` com o campo cru gravaria a mesma
        # coluna sem procedência nenhuma — a distinção que a FDD 028 já fez entre "campo" e "ato"
        # no `status` da fatura.
        read_only_fields = ["id", "kind_display", "dunning_signal", "dunning_signal_display",
                            "cobranca_sinal", "cobranca_sinal_display", "owner",
                            "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        account = cast(Account | None, attrs.get("account", getattr(self.instance, "account", None)))
        # O mixin já normalizou a chave legada, então aqui só existe a canônica.
        venda = cast(
            CommercialOpportunity | None,
            attrs.get(
                "commercial_opportunity",
                getattr(self.instance, "commercial_opportunity", None),
            ),
        )
        if venda and account and venda.account_id != account.id:
            raise serializers.ValidationError(
                {"commercial_opportunity": "A oportunidade deve pertencer ao mesmo cliente."}
            )
        invoice = cast(Invoice | None, attrs.get("invoice", getattr(self.instance, "invoice", None)))
        if invoice and account and invoice.account_id != account.id:
            raise serializers.ValidationError(
                {"invoice": "A fatura deve pertencer ao mesmo cliente."}
            )
        return attrs


class PipelineStageSerializer(serializers.ModelSerializer[PipelineStage]):
    class Meta:
        model = PipelineStage
        fields = ["id", "name", "kind", "position"]
        read_only_fields = ["id"]


class PhaseDeliverableSerializer(serializers.ModelSerializer[PhaseDeliverable]):
    """Entregável do template de uma fase (config admin)."""

    class Meta:
        model = PhaseDeliverable
        fields = ["id", "phase", "name", "position"]
        read_only_fields = ["id"]


class PhaseChecklistItemSerializer(serializers.ModelSerializer[PhaseChecklistItem]):
    """Item do quality gate no template de uma fase (config admin, FDD 033)."""

    class Meta:
        model = PhaseChecklistItem
        fields = ["id", "phase", "text", "position"]
        read_only_fields = ["id"]


class JourneyPhaseSerializer(serializers.ModelSerializer[JourneyPhase]):
    """Fase do template da Jornada de Transformação (config admin, vocabulário editável)."""

    deliverables = PhaseDeliverableSerializer(many=True, read_only=True)
    checklist_items = PhaseChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = JourneyPhase
        fields = [
            "id", "name", "description", "position", "active", "requires_gate",
            "canonical_stage", "deliverables", "checklist_items",
        ]
        read_only_fields = ["id"]


class ProjectDeliverableSerializer(serializers.ModelSerializer[ProjectDeliverable]):
    """Entregável de um projeto — só `status`/`document` são editáveis (marcar entregue)."""

    class Meta:
        model = ProjectDeliverable
        fields = [
            "id", "project_phase", "name", "status", "document", "position",
            "delivered_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "project_phase", "name", "position", "delivered_at", "created_at", "updated_at",
        ]


class ProjectChecklistItemSerializer(serializers.ModelSerializer[ProjectChecklistItem]):
    """Item do quality gate de um projeto — só `checked` é editável (FDD 033)."""

    class Meta:
        model = ProjectChecklistItem
        fields = [
            "id", "project_phase", "text", "position", "checked", "checked_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "project_phase", "text", "position", "checked_at", "created_at", "updated_at",
        ]


# `AliasesDaV1Mixin` sem `ALIASES_DE_ENTRADA`: aqui não há escrita pela chave antiga — a decisão
# entra pela action `apply-gate` —, e o que o mixin traz é a **outra** metade, a de tirar o alias
# da leitura na `/api/v2/`. Vale para os cinco vizinhos que herdam pelo mesmo motivo.
class ProjectPhaseSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[ProjectPhase]):
    """Fase da jornada de um projeto (estado). A equipe edita `target_date` e a justificativa.

    `gate_decision`/`gate_notes` são **read-only de propósito** (FDD 033): a decisão entra só pela
    action `apply-gate`, que é onde moram as consequências de cada saída — concluir e avançar,
    reabrir a fase anterior, ou parar. Um PATCH direto gravaria "REDESIGN" sem nada acontecer, e
    o campo passaria a mentir sobre o estado da jornada.
    """

    phase_name = serializers.CharField(source="phase.name", read_only=True)
    phase_description = serializers.CharField(source="phase.description", read_only=True)
    phase_position = serializers.IntegerField(source="phase.position", read_only=True)
    requires_gate = serializers.BooleanField(source="phase.requires_gate", read_only=True)
    canonical_stage = serializers.CharField(source="phase.canonical_stage", read_only=True)
    # `situation` é o estado semântico derivado (FDD 042): a tela mapeia situação → variante de
    # selo, sem recalcular a regra. `waiting_party`/`blocker_note` são read-only aqui e escritos
    # só pela action `set-waiting`, para a mudança deixar rastro auditável (como a decisão do gate).
    situation = serializers.CharField(read_only=True)
    # Alias de compatibilidade da `/api/v1/`: o campo canônico é `gate_decision` (D7, ADR 0052) e
    # esta chave continua saindo com o mesmo valor até a `/api/v2/`. `CharField` e não
    # `ChoiceField` de propósito — um segundo conjunto com os mesmos quatro valores disputaria o
    # nome do componente no esquema, que é o defeito que `ENUM_NAME_OVERRIDES` existe para evitar.
    gate_outcome = serializers.CharField(source="gate_decision", read_only=True)
    deliverables = ProjectDeliverableSerializer(many=True, read_only=True)
    checklist_items = ProjectChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = ProjectPhase
        fields = [
            "id", "project", "phase", "phase_name", "phase_description", "phase_position",
            "requires_gate", "canonical_stage", "status", "situation", "started_at",
            "completed_at", "target_date", "gate_decision", "gate_outcome", "gate_notes",
            "checklist_waiver", "waiting_party", "blocker_note", "deliverables", "checklist_items",
        ]
        read_only_fields = [
            "id", "project", "phase", "phase_name", "phase_description", "phase_position",
            "requires_gate", "canonical_stage", "status", "situation", "started_at",
            "completed_at", "gate_decision", "gate_outcome", "gate_notes", "waiting_party",
            "blocker_note", "deliverables", "checklist_items",
        ]


class PhaseEventSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[PhaseEvent]):
    """Uma linha do histórico append-only da jornada (FDD 042). Só-leitura — nunca se edita."""

    actor_name = serializers.SerializerMethodField()
    # O mesmo alias de compatibilidade do `ProjectPhaseSerializer`, pelo mesmo motivo: o histórico
    # é lido pela mesma tela, e tirar a chave só de um dos dois quebraria metade do consumidor.
    gate_outcome = serializers.CharField(source="gate_decision", read_only=True)

    class Meta:
        model = PhaseEvent
        fields = [
            "id", "project", "project_phase", "phase_name", "kind", "from_status", "to_status",
            "gate_decision", "gate_outcome", "waiting_party", "note", "actor", "actor_name",
            "source", "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_actor_name(self, obj: PhaseEvent) -> str | None:
        actor = obj.actor
        if actor is None:
            return None
        return actor.get_full_name() or actor.get_username()


class CommercialOpportunitySerializer(
    AliasesDaV1Mixin, serializers.ModelSerializer[CommercialOpportunity]
):
    ALIASES_DE_ENTRADA = {"client": "account"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)

    stage_name = serializers.CharField(source="stage.name", read_only=True)
    stage_kind = serializers.CharField(source="stage.kind", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)
    service_tier = serializers.CharField(source="service.tier", read_only=True)
    project = serializers.SerializerMethodField()
    project_archived = serializers.SerializerMethodField()

    class Meta:
        model = CommercialOpportunity
        fields = [
            "id", "account", "client", "contact", "title", "scope", "estimated_value", "stage",
            "stage_name", "stage_kind", "engagement",
            "owner", "expected_close_date", "service", "service_name", "service_tier", "project",
            "project_archived", "origin_qualification", "created_at", "updated_at",
        ]
        # `origin_qualification` é **só de leitura**, e a assimetria é a guarda: o único caminho
        # que a preenche é `POST /qualifications/{id}/open-opportunity/`, que confere o resultado
        # da avaliação antes. Editável aqui, um `PATCH` cru gravaria a mesma coluna sem passar por
        # `CommercialOpportunity.clean()` — a distinção entre "campo" e "ato" que a FDD 028 já
        # fez no `status` da fatura.
        read_only_fields = [
            "id", "owner", "origin_qualification", "created_at", "updated_at"
        ]

    def _projeto_do_card(self, obj: CommercialOpportunity) -> Project | None:
        """O projeto que representa esta oportunidade no card do pipeline.

        A relação virou 1-N na ADR 0050 e o contrato `/api/v1/` **não** mudou de forma: o card
        continua exibindo um projeto, não uma lista. Qual deles: o **vivo mais antigo**, e só na
        falta de qualquer vivo o arquivado mais antigo. O desempate é por `id` porque é a única
        ordem estável que não depende de campo editável — ordenar por `start_date` faria o card
        trocar de destino quando alguém corrigisse uma data.

        Ordenação em Python e não em SQL: `CommercialOpportunityViewSet` já traz `projects` por
        `prefetch_related`, e um `.order_by()` aqui descartaria o prefetch e devolveria a query
        por card que a ADR 0014 existe para não pagar.
        """
        projetos = sorted(obj.projects.all(), key=lambda p: p.pk)
        vivos = [p for p in projetos if p.archived_at is None]
        return next(iter(vivos or projetos), None)

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_project(self, obj: CommercialOpportunity) -> int | None:
        """Id do projeto que saiu desta oportunidade, ou `None` se ela ainda não foi convertida.

        Sem isto a tela do pipeline não tem como saber que já converteu, e continua oferecendo
        "Criar projeto" numa oportunidade que só pode responder 409.
        """
        project = self._projeto_do_card(obj)
        return project.pk if project else None

    @extend_schema_field(serializers.BooleanField())
    def get_project_archived(self, obj: CommercialOpportunity) -> bool:
        """Só sobrou projeto arquivado?

        `project` continua preenchido nesse caso, de propósito: anulá-lo faria a tela voltar a
        oferecer "Criar projeto" sem dizer que já houve um, e quem clicasse criaria um segundo
        projeto sem saber do primeiro. Com este campo a tela mostra o estado (FDD 025).
        """
        project = self._projeto_do_card(obj)
        return project is not None and project.archived_at is not None

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        account = cast(Account | None, attrs.get("account", getattr(self.instance, "account", None)))
        contact = cast(Contact | None, attrs.get("contact", getattr(self.instance, "contact", None)))
        if contact and account and contact.account_id != account.id:
            raise serializers.ValidationError({"contact": "O contato deve pertencer ao cliente selecionado."})
        return attrs


class EngagementSerializer(serializers.ModelSerializer[Engagement]):
    account_name = serializers.CharField(source="account.name", read_only=True)
    owner_name = serializers.SerializerMethodField()
    sponsor_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    commercial_model_display = serializers.CharField(
        source="get_commercial_model_display", read_only=True
    )
    projects_count = serializers.SerializerMethodField()
    originating_commercial_opportunity_title = serializers.CharField(
        source="originating_commercial_opportunity.title", read_only=True, default=""
    )
    originating_design_partner_agreement_name = serializers.CharField(
        source="originating_design_partner_agreement.original_name", read_only=True, default=""
    )
    discovery_scheduled_at = serializers.SerializerMethodField()
    # Par derivado com fallback, DAP `dap-grupo-de-whatsapp-r1` B1: o do próprio mandato quando
    # existe, senão o do projeto vivo mais antigo com grupo legado. O nome do campo é o mesmo do
    # modelo de propósito — para a `/api/v1/` o fato é um só, "o canal do mandato".
    whatsapp_group_id = serializers.SerializerMethodField()
    whatsapp_group_invite_url = serializers.SerializerMethodField()

    class Meta:
        model = Engagement
        fields = [
            "id", "account", "account_name", "name", "mandate", "sponsor", "sponsor_name",
            "owner", "owner_name",
            "status", "status_display", "commercial_model", "commercial_model_display",
            "originating_commercial_opportunity",
            "originating_commercial_opportunity_title",
            "originating_design_partner_agreement",
            "originating_design_partner_agreement_name",
            "started_at", "ended_at", "success_definition", "projects_count",
            "discovery_scheduled_at", "whatsapp_group_id", "whatsapp_group_invite_url",
            "needs_review", "archived_at", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "archived_at", "created_at", "updated_at"]
        # `owner` deixa de ser obrigatório no POST, e quem o preenche é
        # `EngagementViewSet.perform_create` — a seção do detalhe do cliente não pergunta quem é o
        # responsável, porque quem cria o mandato é quem está logado (DAP `dap-engagement-r1`).
        # Continua **gravável**: relaxar exigência não é tirar o campo, e o admin que cria mandato
        # de outra pessoa segue dizendo de quem ele é.
        extra_kwargs = {"owner": {"required": False}}

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_owner_name(self, obj: Engagement) -> str | None:
        owner = obj.owner
        return (owner.get_full_name() or owner.get_username()) if owner else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_sponsor_name(self, obj: Engagement) -> str | None:
        """O nome de quem responde pelo mandato dentro da conta — **nulo quando não há**.

        `sponsor` é opcional, e é por isso que isto é método e não `source="sponsor.full_name"`:
        aquele estouraria com `AttributeError` no mandato sem patrocinador, que é o caso comum de
        um mandato de escopo único nascido da conversão. Sem patrocinador a linha da tela
        simplesmente não mostra a frase.

        O `select_related("sponsor")` do `EngagementViewSet` é o que mantém isto sem N+1.
        """
        sponsor = obj.sponsor
        return sponsor.full_name if sponsor else None

    @extend_schema_field(serializers.IntegerField())
    def get_projects_count(self, obj: Engagement) -> int:
        """Quantos projetos do mandato **quem está lendo alcança** — não o total do mandato.

        A anotação vem de `EngagementViewSet.get_queryset`, recortada por `project_scope_q`, e a
        consequência está registrada na FDD 046: dois usuários veem números diferentes para o
        mesmo mandato. É o mesmo comportamento de `/clients/overview/`.

        O `0` de fallback cobre um caso só e ele é verdadeiro: a resposta do `POST`, que serializa
        a instância recém-criada em vez de uma linha do queryset — e um mandato que acabou de
        nascer não tem projeto nenhum.
        """
        return getattr(obj, "projects_count", 0)

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_discovery_scheduled_at(self, obj: Engagement) -> str | None:
        """Quando o cliente marcou o Discovery pelo link do convite — `null` quando não marcou.

        Só leitura, e existe para a tela **pré-preencher** o início e o prazo do projeto (DAP
        `dap-engagement-r3`, C1). O servidor não sobrescreve essas datas: quem preencheu o
        formulário escolheu, e um 201 que muda a escolha em silêncio é pior que um campo a menos.

        `discovery_agendado` é a única expressão de "a reserva viva do mandato" — a mesma que a
        rota pública e `registrar_sessao_no_projeto` consultam. Reescrever o filtro aqui seria uma
        segunda definição de "viva", que diverge da primeira no dia em que `Booking` ganhar estado.

        A conversão passa por `DateTimeField.to_representation` e não pelo `datetime` cru: método
        devolve o valor como está, e um `datetime` solto sairia formatado pelo encoder do renderer
        em vez de pelo `DATETIME_FORMAT` do projeto — uma data com formato próprio no meio de um
        recurso onde todas as outras concordam.
        """
        reserva = discovery_booking.discovery_agendado(obj)
        if reserva is None:
            return None
        return cast(str, serializers.DateTimeField().to_representation(reserva.starts_at))

    @extend_schema_field(serializers.CharField())
    def get_whatsapp_group_id(self, obj: Engagement) -> str:
        return kickoff.grupo_do_mandato(obj)[0]

    @extend_schema_field(serializers.CharField())
    def get_whatsapp_group_invite_url(self, obj: Engagement) -> str:
        return kickoff.grupo_do_mandato(obj)[1]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Repete no contrato HTTP a invariante 13 que `Engagement.clean()` sustenta no modelo.

        O legado sem origem continua editável enquanto `needs_review=True`; uma criação nova não
        ganha essa exceção nem quando o cliente tenta mandar o carimbo no payload. Quando a origem
        histórica é preenchida, ela não muda por PATCH — corrigir fato observado exige intervenção
        administrativa deliberada, não edição casual do formulário.
        """
        current = self.instance

        def value(field: str, default: object = None) -> object:
            if field in attrs:
                return attrs[field]
            return getattr(current, field, default) if current is not None else default

        opportunity = cast(
            CommercialOpportunity | None,
            value("originating_commercial_opportunity"),
        )
        agreement = cast(
            Document | None,
            value("originating_design_partner_agreement"),
        )
        if current is not None:
            for field in (
                "originating_commercial_opportunity",
                "originating_design_partner_agreement",
            ):
                current_id = getattr(current, f"{field}_id")
                incoming = attrs.get(field)
                incoming_id = getattr(incoming, "pk", None)
                if field in attrs and current_id is not None and incoming_id != current_id:
                    raise serializers.ValidationError({
                        field: "O instrumento de origem não muda depois de registrado."
                    })
        if current is None and opportunity is None and agreement is None:
            field = (
                "originating_design_partner_agreement"
                if value("commercial_model", Engagement.CommercialModel.PAID)
                == Engagement.CommercialModel.DESIGN_PARTNER
                else "originating_commercial_opportunity"
            )
            raise serializers.ValidationError({
                field: "Informe o instrumento assinado que originou o engagement."
            })

        candidate = Engagement(
            pk=current.pk if current is not None else None,
            account=cast(Account | None, value("account")),
            name=str(value("name", "")),
            owner=cast(User | None, value("owner")),
            sponsor=cast(Contact | None, value("sponsor")),
            status=str(value("status", Engagement.Status.ACTIVE)),
            commercial_model=str(value("commercial_model", Engagement.CommercialModel.PAID)),
            started_at=cast(date | None, value("started_at")),
            ended_at=cast(date | None, value("ended_at")),
            needs_review=bool(value("needs_review", False)),
            originating_commercial_opportunity=opportunity,
            originating_design_partner_agreement=agreement,
        )
        # O objeto-candidato não veio do ORM, mas representa uma linha legada já persistida no
        # PATCH. Sem preservar esse estado, `Engagement.clean()` o trataria como criação nova e
        # fecharia justamente a exceção de edição que `needs_review` existe para manter.
        candidate._state.adding = current is None
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            ) from exc
        return attrs

    @staticmethod
    def _bind_origin(instance: Engagement) -> None:
        opportunity_id = instance.originating_commercial_opportunity_id
        if opportunity_id is not None:
            CommercialOpportunity.objects.filter(
                pk=opportunity_id, engagement__isnull=True
            ).update(engagement=instance)

    @transaction.atomic
    def create(self, validated_data: dict[str, object]) -> Engagement:
        instance = super().create(validated_data)
        self._bind_origin(instance)
        return instance

    @transaction.atomic
    def update(self, instance: Engagement, validated_data: dict[str, object]) -> Engagement:
        updated = super().update(instance, validated_data)
        self._bind_origin(updated)
        return updated

class ProjectSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Project]):
    ALIASES_DE_ENTRADA = {"ai_opportunity": "ai_potential"}

    is_overdue = serializers.SerializerMethodField()
    # A vertical do cliente, aqui, para o detalhe do projeto pedir o catálogo já resolvido sem
    # ter de carregar o cliente inteiro só por causa de um id (FDD 026).
    client = serializers.IntegerField(source="engagement.account_id", read_only=True)
    account_vertical = serializers.IntegerField(
        source="engagement.account.vertical_id", read_only=True
    )
    account_vertical_name = serializers.CharField(
        source="engagement.account.vertical.name", read_only=True, default=""
    )
    # As duas legadas, mesmo valor das canônicas acima, e morrem na `/api/v2/` (issue #122, fatia
    # 4a). **Não houve renome de campo aqui**: `client_vertical` nunca foi coluna — o campo do
    # modelo é `Account.vertical`, e estas quatro chaves são projeção sobre `engagement.account`
    # (FDD 026). O que a fatia paga é a chave de payload, que é o que a §2c trata.
    client_vertical = serializers.IntegerField(source="engagement.account.vertical_id", read_only=True)
    client_vertical_name = serializers.CharField(
        source="engagement.account.vertical.name", read_only=True, default=""
    )
    engagement_name = serializers.CharField(source="engagement.name", read_only=True, default="")
    # **Alias de compatibilidade, e só isso** (`docs/ontology/language-map.md` §7): o campo do
    # modelo virou `originating_commercial_opportunity` na ADR 0050, e o contrato `/api/v1/`
    # continua expondo `opportunity` para não quebrar consumidor nenhum no meio do renome. Some
    # na Fase 6, junto dos outros aliases. Só de leitura porque escrever pelos dois nomes ao
    # mesmo tempo daria duas fontes para a mesma coluna.
    opportunity = serializers.PrimaryKeyRelatedField(
        source="originating_commercial_opportunity", read_only=True
    )
    ai_opportunity = serializers.IntegerField(source="ai_potential", read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "client", "engagement", "engagement_name", "originating_commercial_opportunity",
            "opportunity", "name", "description", "owner", "start_date", "due_date",
            "status", "service", "actual_value", "cost", "is_overdue", "created_at", "updated_at",
            "ai_maturity", "ai_potential", "ai_opportunity", "ai_dimensions", "ai_score_summary",
            "ai_scored_at", "ai_score_reviewed", "account_vertical", "account_vertical_name",
            "client_vertical", "client_vertical_name",
        ]
        read_only_fields = [
            "id", "owner", "is_overdue", "created_at", "updated_at", "ai_scored_at",
        ]

    def __init__(self, *args: Any, engagement_optional: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A origem comercial se escreve **na criação e nunca depois**. Editável num `PATCH`, ela
        # reescreveria a proveniência de um projeto que já existe — e a origem é justamente o
        # dado que a análise de funil e o ciclo médio leem como fato histórico. Read-only sempre
        # seria o outro extremo: `POST /projects/` é o caminho pelo qual a venda recorrente cria
        # o segundo projeto da mesma origem, e ele precisa dizer qual é.
        if self.instance is not None:
            self.fields["originating_commercial_opportunity"].read_only = True
        # `engagement` é obrigatório em `POST /projects/` — é o que faz a invariante 7 do mapa de
        # linguagem valer no caminho normal, e não só na coluna NOT NULL. A exceção é a
        # `convert-to-project`, que passa este sinalizador: lá o mandato é **opcional no payload**
        # porque é a própria conversão que o cria quando a venda é avulsa (D3, ADR 0050). Sem esta
        # brecha, converter passaria a exigir da tela um engajamento que ainda não existe.
        if engagement_optional:
            self.fields["engagement"].required = False

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        start = cast(date | None, attrs.get("start_date", getattr(self.instance, "start_date", None)))
        due = cast(date | None, attrs.get("due_date", getattr(self.instance, "due_date", None)))
        if start and due and due < start:
            raise serializers.ValidationError({"due_date": "A data final não pode ser anterior à inicial."})
        # `client` segue aceito na escrita da `/api/v1/`, mas deixou de ser uma coluna: a fonte
        # canônica agora é exclusivamente `engagement.account`. Validar o alias em vez de só
        # ignorá-lo preserva o contrato anterior — um chamador antigo que mande uma conta alheia
        # continua recebendo 400, em vez de acreditar que moveu o projeto quando nada foi gravado.
        if hasattr(self, "initial_data") and "client" in self.initial_data:
            legacy_account = serializers.PrimaryKeyRelatedField(
                queryset=Account.objects.all()
            ).run_validation(self.initial_data["client"])
            engagement = cast(
                Engagement | None,
                attrs.get("engagement", getattr(self.instance, "engagement", None)),
            )
            if engagement and legacy_account.pk != engagement.account_id:
                raise serializers.ValidationError(
                    {"engagement": "O engajamento deve pertencer ao mesmo cliente do projeto."}
                )
        return attrs

    def get_is_overdue(self, project: Project) -> bool:
        return project.status != Project.Status.COMPLETED and project.due_date < date.today()


class WorkItemSerializer(serializers.ModelSerializer):
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        fields = [
            "id", "project", "title", "description", "owner", "due_date", "completed_at", "status",
            "is_overdue", "party", "source", "external_id", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "owner", "completed_at", "is_overdue", "source", "external_id",
            "created_at", "updated_at",
        ]


class MilestoneSerializer(WorkItemSerializer):
    class Meta(WorkItemSerializer.Meta):
        model = Milestone


class MeetingSerializer(serializers.ModelSerializer[Meeting]):
    class Meta:
        model = Meeting
        fields = ["id", "project", "title", "date", "meeting_url", "recording_url", "transcript",
                  "status", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class PendenciaSerializer(serializers.ModelSerializer[Pendencia]):
    class Meta:
        model = Pendencia
        fields = ["id", "project", "title", "description", "status", "party", "owner",
                  "resolved_at", "created_at", "updated_at"]
        read_only_fields = ["id", "owner", "resolved_at", "created_at", "updated_at"]


class DecisaoSerializer(serializers.ModelSerializer[Decisao]):
    # `published_at` é read-only pela razão do `resolved_at` acima: quem carimba é o `save()` do
    # modelo, não quem manda o PATCH. Aceitar a data do cliente seria deixar reescrever quando uma
    # decisão passou a valer.
    class Meta:
        model = Decisao
        fields = ["id", "project", "project_phase", "title", "rationale", "decided_on",
                  "decided_by", "status", "source_meeting", "published_at", "created_at",
                  "updated_at"]
        read_only_fields = ["id", "published_at", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        instance = self.instance
        project = attrs.get("project", instance.project if instance is not None else None)
        project_phase = attrs.get(
            "project_phase", instance.project_phase if instance is not None else None
        )
        status_value = attrs.get(
            "status", instance.status if instance is not None else Decisao.Status.DRAFT
        )

        if project_phase is not None and project is not None:
            if project_phase.project_id != project.pk:
                raise serializers.ValidationError(
                    {"project_phase": "A fase deve pertencer ao mesmo projeto da decisão."}
                )
        if status_value == Decisao.Status.PUBLISHED and project_phase is None:
            raise serializers.ValidationError(
                {"project_phase": "Escolha uma fase da jornada antes de publicar a decisão."}
            )
        return attrs


class RiscoSerializer(serializers.ModelSerializer[Risco]):
    # `owner` e `resolved_at` são read-only pelo motivo da `Pendencia` acima: o dono sai da sessão
    # de quem registrou e o carimbo sai do `save()` do modelo. Aceitá-los do corpo deixaria
    # reescrever quem viu o risco e quando ele deixou de ameaçar.
    class Meta:
        model = Risco
        fields = ["id", "project", "title", "description", "probability", "impact", "mitigation",
                  "status", "owner", "resolved_at", "created_at", "updated_at"]
        read_only_fields = ["id", "owner", "resolved_at", "created_at", "updated_at"]


class SatisfactionRecordSerializer(
    AliasesDaV1Mixin, serializers.ModelSerializer[SatisfactionRecord]
):
    """O registro de satisfação (FDD 037).

    `registered_by` é só de leitura pelo motivo do `owner` do `Risco` acima: quem registrou sai da
    sessão, não do corpo. Aqui pesa mais que lá — este registro muda o Health Score e a escada da
    cobrança, e "quem ouviu isso do cliente" é metade do que torna o sinal avaliável depois.
    """

    ALIASES_DE_ENTRADA = {"client": "account"}

    # Os dois enums falam inglês desde a migração `0086` (issue #122, fatia 5.3; D10 do
    # language-map). Quem integrou com a `/api/v1/` mandando `"neutro"`/`"declarada"` continua
    # funcionando: o mixin traduz para o canônico antes da validação. A `/api/v2/` não herda a
    # tradução — o valor legado cai na validação de `choices` do campo e leva o 400 padrão do DRF,
    # que já lista o vocabulário inteiro (o argumento da fatia 5.1: uma frase nossa seria a segunda
    # definição do mesmo erro). **As chaves `nivel`/`fonte` não mudam**: elas são chave de payload,
    # e a §2c as congela até a `/api/v2/` — o que atravessou aqui foi só o valor.
    VALORES_DE_ENTRADA = {
        "nivel": {
            "promotor": SatisfactionRecord.Nivel.PROMOTER,
            "satisfeito": SatisfactionRecord.Nivel.SATISFIED,
            "neutro": SatisfactionRecord.Nivel.NEUTRAL,
            "insatisfeito": SatisfactionRecord.Nivel.DISSATISFIED,
        },
        "fonte": {
            "declarada": SatisfactionRecord.Fonte.DECLARED,
            "percebida": SatisfactionRecord.Fonte.PERCEIVED,
        },
    }

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)
    nivel_display = serializers.CharField(source="get_nivel_display", read_only=True)
    fonte_display = serializers.CharField(source="get_fonte_display", read_only=True)

    class Meta:
        model = SatisfactionRecord
        fields = ["id", "account", "client", "project", "source_meeting", "source_activity",
                  "nivel",
                  "nivel_display", "fonte", "fonte_display", "happened_on", "note",
                  "registered_by", "created_at", "updated_at"]
        read_only_fields = ["id", "nivel_display", "fonte_display", "registered_by", "created_at",
                            "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """As mesmas três regras do `clean()` do modelo, repetidas aqui de propósito.

        É o que o `ActivitySerializer` já faz: sem elas a API devolveria 500 no `full_clean` do
        `save()` em vez de um 400 com o campo errado apontado, e a tela não teria o que mostrar.
        """
        account = cast(Account | None, attrs.get("account", getattr(self.instance, "account", None)))
        project = cast(
            Project | None, attrs.get("project", getattr(self.instance, "project", None))
        )
        if project and account and project.engagement.account_id != account.id:
            raise serializers.ValidationError(
                {"project": "O projeto deve pertencer ao mesmo cliente."}
            )
        # A atividade de origem (FDD 038) tem a mesma fronteira do projeto: o atalho do painel
        # manda o id da interação, e uma resposta de outro cliente viraria a satisfação declarada
        # deste — a linha que troca a escada e tira 20 pontos do Health Score.
        source_activity = cast(
            Activity | None,
            attrs.get("source_activity", getattr(self.instance, "source_activity", None)),
        )
        if source_activity and account and source_activity.account_id != account.id:
            raise serializers.ValidationError(
                {"source_activity": "A interação deve pertencer ao mesmo cliente."}
            )
        nivel = attrs.get("nivel", getattr(self.instance, "nivel", None))
        note = cast(str, attrs.get("note", getattr(self.instance, "note", "")) or "")
        if nivel == SatisfactionRecord.Nivel.DISSATISFIED and not note.strip():
            raise serializers.ValidationError(
                {"note": "Diga o que o cliente disse: insatisfeito sem nota não se avalia depois."}
            )
        return attrs


class PublicationStateSerializer(serializers.Serializer):
    """O campo derivado `publication_state` (issue `#108`, DAP `dap-publicacao-discovery-r1`
    decisão E1), reusado pelos cinco serializers publicáveis via `source="*"` — `source="*"`
    entrega o objeto inteiro a `to_representation`, que delega por inteiro a
    `publication.estado_de_publicacao`.

    Um serializer comum (não de modelo) em vez de cinco `SerializerMethodField` sem tipo: assim o
    drf-spectacular gera um componente de verdade, e não um `object` solto repetido cinco vezes.
    Nenhum campo daqui decide nada — a regra e as frases moram só em `publication.py`.
    """

    state = serializers.ChoiceField(choices=["published", "ready", "blocked"], read_only=True)
    missing = serializers.ListField(child=serializers.CharField(), read_only=True)
    missing_phrase = serializers.CharField(read_only=True)
    blocked_by = serializers.IntegerField(read_only=True)
    blocked_phrase = serializers.CharField(read_only=True)

    def to_representation(self, instance: Any) -> dict[str, Any]:
        return publication.estado_de_publicacao(instance)


# `valor` e `total` saem como texto porque `get_custo` converte cada `Decimal` em `str` — a
# docstring de lá diz por quê (o encoder do DRF transformaria dinheiro em `float`). O esquema
# descreve o que trafega, e é `CharField` nos dois.
class ProcessCostLineSerializer(serializers.Serializer):
    """Uma parcela da conta do custo do estado atual. `valor` é texto, com duas casas."""

    label = serializers.CharField()
    valor = serializers.CharField()


class ProcessCostSerializer(serializers.Serializer):
    """A conta do custo do estado atual do processo, por mês.

    `total: "0.00"` com `nao_apurado` cheio **não** significa "custa zero": significa "não há
    insumo para dizer". As duas listas são complementares — juntas somam os seis rótulos —, e é
    por `nao_apurado`, nunca pelo total, que se distinguem os dois casos.
    """

    parcelas = ProcessCostLineSerializer(many=True)
    total = serializers.CharField()
    nao_apurado = serializers.ListField(child=serializers.CharField())
    # `"sustentado"` ou `"hipotese"` — se há `Finding(fact)` vivo por trás do número
    # (`docs/metodologia-fde.md:117`). `CharField` e não `ChoiceField`: as duas saídas são
    # constantes de `process.py`, não `choices` de modelo.
    sustentacao = serializers.CharField()


class ProcessSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Process]):
    """O processo mapeado no Discovery (FDD 039), com a conta do custo do estado atual junto.

    `custo` é derivado e só de leitura: ele é a fórmula de `docs/metodologia-fde.md:118-119` aplicada
    aos nove insumos que já estão no corpo. Persistir o total seria uma segunda verdade sobre o
    mesmo dado — mudaria o volume e o número gravado continuaria dizendo o antigo.

    `registered_by` é só de leitura pelo motivo do `Risco` e do `SatisfactionRecord` acima: quem
    levantou sai da sessão, não do corpo.

    `published_at`/`published_by` são só de leitura pelo motivo dos outros quatro publicáveis
    (FDD 051): quem escreve a marca é a action `publish/`, que confere se o mapa pode sair — e
    `unpublish/`, que recusa quando ele ancora achado publicado.
    """

    ALIASES_DE_ENTRADA = {"client": "account"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)
    # O mesmo par, um degrau adiante: `client_name` era projeção sem canônica — a v2 nasceria sem
    # nome nenhum para o nome da conta. `account_name` é a canônica; a legada sai ao lado na v1 e
    # some na v2 (issue #122, fatia 4a).
    account_name = serializers.CharField(source="account.name", read_only=True)
    client_name = serializers.CharField(source="account.name", read_only=True)
    custo = serializers.SerializerMethodField()
    publication_state = PublicationStateSerializer(source="*", read_only=True)

    class Meta:
        model = Process
        fields = ["id", "account", "client", "account_name", "client_name", "name", "position",
                  "source_project",
                  "source_meeting", "registered_by", "volume_mes", "tempo_horas", "pessoas",
                  "custo_hora", "retrabalho_mes", "erros_mes", "perdas_mes", "espera_mes",
                  "risco_mes", "custo", "published_at", "published_by", "publication_state",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "account_name", "client_name", "registered_by", "custo",
                            "published_at",
                            "published_by", "publication_state", "created_at", "updated_at"]

    @extend_schema_field(ProcessCostSerializer())
    def get_custo(self, processo: Process) -> dict[str, Any]:
        """A conta à vista: parcelas, total, o que não foi apurado e se há fato sustentando.

        Quem lê **não** pode concluir "custa zero" de um total zerado: é `nao_apurado` que separa
        "não há insumo" de "medimos e deu zero" (ver `process.custo_do_estado_atual`).

        **Os valores saem como texto, e não é preciosismo.** `SerializerMethodField` entrega o que
        devolver direto ao renderizador, e o encoder do DRF converte `Decimal` em `float`
        (`rest_framework/utils/encoders.py`) — o próprio comentário de lá diz que aquele ramo
        existe para quem escapa de um `DecimalField`, que é este caso. Sem a conversão,
        `Decimal("5000.00")` chega ao cliente como `5000.0`, dinheiro trafega em ponto flutuante e
        `Invoice.amount` (string, pelo `COERCE_DECIMAL_TO_STRING`) e o custo passam a ter formatos
        diferentes na mesma API. Também contrariaria o `process.py`, que evita `float` por dentro
        justamente para não somar centavos com erro.

        Um teste sobre `response.data` **não pega isto**: ali o valor ainda é `Decimal`, e a
        conversão só acontece na renderização. A regressão afirma sobre o JSON renderizado.
        """
        custo = process_module.custo_do_estado_atual(processo)
        return {
            **custo,
            "total": str(custo["total"]),
            "parcelas": [
                {**parcela, "valor": str(parcela["valor"])} for parcela in custo["parcelas"]
            ],
        }


class ProcessStepSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[ProcessStep]):
    """A etapa e o P-S-D-T-E-R dela (`docs/metodologia-fde.md:106-110`).

    Os seis campos saem na ordem das seis letras de propósito: é assim que a pergunta é feita na
    reunião, e um formulário fora de ordem faz quem preenche pular a pergunta que faltou.
    """

    ALIASES_DE_ENTRADA = {"processo": "process"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    processo = serializers.PrimaryKeyRelatedField(source="process", read_only=True)

    class Meta:
        model = ProcessStep
        fields = ["id", "process", "processo", "name", "position", "pessoas", "sistema", "dados",
                  "tempo", "erro", "retrabalho", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class DiscoverySerializer(serializers.ModelSerializer[Discovery]):
    """O Discovery como unidade de levantamento (FDD 045).

    As duas regras de data repetem o `clean()` do modelo pelo motivo de sempre nesta base: o
    `save()` do DRF não chama `full_clean`, e uma guarda que só vale pelo admin não é guarda.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)

    class Meta:
        model = Discovery
        fields = ["id", "project", "project_name", "scope", "status", "status_display",
                  "started_at", "completed_at", "owner", "created_at", "updated_at"]
        read_only_fields = ["id", "project_name", "status_display", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        started = attrs.get("started_at", getattr(self.instance, "started_at", None))
        completed = attrs.get("completed_at", getattr(self.instance, "completed_at", None))
        estado = attrs.get("status", getattr(self.instance, "status", None))
        if started and completed and cast(date, completed) < cast(date, started):
            raise serializers.ValidationError(
                {"completed_at": "O fim do Discovery não pode ser anterior ao início."}
            )
        if estado == Discovery.Status.COMPLETED and not completed:
            raise serializers.ValidationError(
                {"completed_at": "Um Discovery concluído precisa da data de conclusão."}
            )
        return attrs


class DiscoverySessionSerializer(serializers.ModelSerializer[DiscoverySession]):
    """A sessão do Discovery (FDD 045) — reunião, visita ou leitura de sistema.

    `notes` sai e **não entra por aqui** (FDD 055): a escrita é
    `POST /discovery-sessions/{id}/notes/`, que grava **um bloco** e preserva os outros. A razão é
    a mesma que faz `published_at` ser só de leitura — o que vale depende do estado corrente, e um
    `PATCH` do campo inteiro apagaria em silêncio o bloco que o colega acabou de salvar. A decisão
    H3 do DAP aceita a sobrescrita entre pessoas **dentro** de um bloco; a mitigação que ela
    registra ("um bloco por pessoa durante a sessão") só funciona porque o bloco é a unidade de
    escrita.
    """

    # Quantos achados saíram desta sessão. Vem **anotado** pela queryset do viewset; o `getattr`
    # com zero é para o caminho em que o objeto não veio de lá — a criação, que responde com a
    # instância recém-salva e que não tem achado nenhum mesmo.
    structured_finding_count = serializers.SerializerMethodField()

    class Meta:
        model = DiscoverySession
        fields = ["id", "discovery", "meeting", "happened_at", "participants", "source_artifact",
                  "transcript", "notes", "structured_finding_count", "created_at", "updated_at"]
        read_only_fields = ["id", "notes", "created_at", "updated_at"]

    @extend_schema_field(serializers.IntegerField())
    def get_structured_finding_count(self, obj: DiscoverySession) -> int:
        return int(getattr(obj, "structured_finding_count", 0) or 0)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        discovery = cast(
            Discovery | None, attrs.get("discovery", getattr(self.instance, "discovery", None))
        )
        meeting = cast(
            Meeting | None, attrs.get("meeting", getattr(self.instance, "meeting", None))
        )
        if meeting and discovery and meeting.project_id != discovery.project_id:
            raise serializers.ValidationError(
                {"meeting": "A reunião deve pertencer ao mesmo projeto do Discovery."}
            )
        return attrs


class DiscoverySessionNotesSerializer(serializers.Serializer):
    """O corpo de `POST /discovery-sessions/{id}/notes/`: **um** bloco e as respostas dele.

    Não é `ModelSerializer` porque não há campo de modelo com esta forma — `notes` guarda os seis
    blocos, e o que se escreve é um. É o mesmo desenho do serializer de entrada de qualquer action
    com corpo próprio: o que ele valida é o pedido, não a linha.

    **A validação é contra `discovery_questions.BLOCKS`, e olha só o que está sendo escrito.** O
    que já está gravado nunca é revalidado: uma pergunta removida da base deixa de ser exibida e a
    resposta dela continua no JSON, e revalidar o conjunto inteiro faria essa remoção travar o
    salvamento de um bloco vizinho — apagando, na prática, o registro de uma reunião por causa de
    uma edição de catálogo.
    """

    block = serializers.CharField()
    answers = serializers.DictField(child=serializers.CharField(allow_blank=True))

    def validate_block(self, value: str) -> str:
        if discovery_questions.block(value) is None:
            raise serializers.ValidationError(
                f"Bloco desconhecido: {value}. Os blocos são "
                f"{', '.join(bloco.id for bloco in discovery_questions.BLOCKS)}."
            )
        return value

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        conhecidas = discovery_questions.question_ids(cast(str, attrs["block"]))
        desconhecidas = sorted(set(cast(dict, attrs["answers"])) - conhecidas)
        if desconhecidas:
            raise serializers.ValidationError(
                {"answers": f"Pergunta desconhecida neste bloco: {', '.join(desconhecidas)}."}
            )
        return attrs


class ProcessObservationSerializer(serializers.ModelSerializer[ProcessObservation]):
    """A observação de um processo dentro de um Discovery (FDD 045).

    É esta linha que permite o mesmo processo aparecer em dois Discoveries sem duplicar o mapa —
    e por isso ela **não** tem unicidade por (discovery, process): revisitar o mesmo processo duas
    vezes no mesmo Discovery é o caso normal de uma validação depois da primeira leitura.
    """

    observation_type_display = serializers.CharField(
        source="get_observation_type_display", read_only=True
    )
    # O nome do processo observado e a conta dele, no molde de `ProcessSerializer.account_name`: a
    # tela da Discovery Session lista o que saiu da estruturação e leva a quem o revisa (FDD 055),
    # e sem os dois precisaria de uma segunda chamada por linha só para escrever um rótulo e um
    # `href`. **O processo é da conta**, não do projeto — é a âncora da FDD 039, e é por isso que o
    # caminho de revisão sai daqui e não do projeto que conduziu o Discovery.
    process_name = serializers.CharField(source="process.name", read_only=True)
    account = serializers.IntegerField(source="process.account_id", read_only=True)

    class Meta:
        model = ProcessObservation
        fields = ["id", "discovery", "process", "process_name", "account", "observed_at",
                  "observation_type", "observation_type_display", "source_session", "created_at",
                  "updated_at"]
        read_only_fields = ["id", "process_name", "account", "observation_type_display",
                            "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        discovery = cast(
            Discovery | None, attrs.get("discovery", getattr(self.instance, "discovery", None))
        )
        session = cast(
            DiscoverySession | None,
            attrs.get("source_session", getattr(self.instance, "source_session", None)),
        )
        if session and discovery and session.discovery_id != discovery.pk:
            raise serializers.ValidationError(
                {"source_session": "A sessão deve pertencer ao mesmo Discovery."}
            )
        return attrs


class EvidenceSerializer(serializers.ModelSerializer[Evidence]):
    """O dado bruto que sustenta um achado (FDD 045).

    `content_hash` é só de leitura e sai do `save()` do modelo: um carimbo de integridade que o
    corpo da requisição pudesse escrever não carimbaria nada.

    `captured_by` sai da sessão: quem observou tem nome, e o nome é o de quem está autenticado.

    `published_at`/`published_by` são **só de leitura**, como `content_hash` e pelo mesmo tipo de
    razão: quem escreve a marca é a action `publish/`, que confere a cadeia de sustentação
    (FDD 051). Um `PATCH` que pudesse carimbá-los publicaria sem passar por ela, e a invariante
    viraria sugestão — é a decisão de `journey.apply_gate` e de `prove-experiments/{id}/start/`.
    """

    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    publication_state = PublicationStateSerializer(source="*", read_only=True)

    class Meta:
        model = Evidence
        fields = ["id", "account", "discovery", "process", "step", "kind", "kind_display",
                  "raw_excerpt", "reference", "source_session", "source_meeting", "captured_at",
                  "captured_by", "content_hash", "published_at", "published_by",
                  "publication_state", "created_at", "updated_at"]
        read_only_fields = ["id", "kind_display", "captured_by", "content_hash",
                            "published_at", "published_by", "publication_state",
                            "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """A mesma regra do `clean()` do modelo — o `save()` do DRF não chama `full_clean`."""
        account = cast(
            Account | None, attrs.get("account", getattr(self.instance, "account", None))
        )
        process = cast(
            Process | None, attrs.get("process", getattr(self.instance, "process", None))
        )
        step = cast(
            ProcessStep | None, attrs.get("step", getattr(self.instance, "step", None))
        )
        discovery = cast(
            Discovery | None, attrs.get("discovery", getattr(self.instance, "discovery", None))
        )
        session = cast(
            DiscoverySession | None,
            attrs.get("source_session", getattr(self.instance, "source_session", None)),
        )
        raw = cast(str, attrs.get("raw_excerpt", getattr(self.instance, "raw_excerpt", "")) or "")
        reference = cast(
            str, attrs.get("reference", getattr(self.instance, "reference", "")) or ""
        )
        if step and process and step.process_id != process.pk:
            raise serializers.ValidationError({"step": "A etapa deve pertencer ao mesmo processo."})
        if process and account and process.account_id != account.pk:
            raise serializers.ValidationError(
                {"process": "O processo deve pertencer à mesma conta."}
            )
        if step and account and step.process.account_id != account.pk:
            raise serializers.ValidationError({"step": "A etapa deve pertencer à mesma conta."})
        if discovery and account and discovery.project.engagement.account_id != account.pk:
            raise serializers.ValidationError(
                {"discovery": "O Discovery deve pertencer à mesma conta."}
            )
        if session and discovery and session.discovery_id != discovery.pk:
            raise serializers.ValidationError(
                {"source_session": "A sessão deve pertencer ao mesmo Discovery."}
            )
        if not raw.strip() and not reference.strip():
            raise serializers.ValidationError(
                "Uma evidência precisa do trecho bruto ou de um localizador da fonte."
            )
        return attrs


#: A recusa da quinta porta, por registro: as duas só diferem no gênero da copy.
_ANCORA_MOVIDA: dict[str, str] = {
    "finding": "Este achado está publicado: mover a âncora exige um processo publicado e vivo. "
               "Publique o processo antes, ou despublique o achado.",
    "pain_point": "Esta dor está publicada: mover a âncora exige um processo publicado e vivo. "
                  "Publique o processo antes, ou despublique a dor.",
}


def _recusa_ancora_nao_publicada(
    instance: Finding | PainPoint | None,
    attrs: dict[str, object],
    process: Process | None,
    step: ProcessStep | None,
    registro: str,
) -> None:
    """A quinta porta da cadeia de publicação (FDD 051): mover a âncora por baixo do publicado.

    As quatro primeiras olham a **marca** — `publish/`, `unpublish/`, o `DELETE` e o `PATCH` que
    promove a `fact`. Nenhuma delas vê este caminho: um `PATCH` de `process`/`step` num registro
    **já publicado** não toca em `published_at` nenhum, e ainda assim faz `findings[].process_id`
    (ou `step_id`) apontar para fora de `processes[]` — a mesma referência pendurada, pela porta
    que ninguém olhou. É o argumento das outras quatro, e vale igual para a quinta.

    Só o registro publicado é cobrado: mover a âncora de um achado interno é edição normal do
    levantamento, e é assim que deve ser.

    Quem responde "a âncora atravessa?" é `publication.falta_a_ancora`, sobre o valor **resolvido**
    — o que veio no corpo, ou o que já estava. Reexpressar aqui "publicado e vivo, por `process` e
    por `step`" seria a segunda definição que `publication.py` existe para não ter.
    """
    if instance is None or instance.published_at is None:
        return
    if not publication.falta_a_ancora(process, step):
        return
    # Nomeia o campo que o corpo mexeu. Quando os dois vêm juntos eles apontam para o mesmo mapa —
    # a consistência entre `step` e `process` já foi validada logo acima.
    campo = "step" if "step" in attrs else "process"
    raise serializers.ValidationError({campo: _ANCORA_MOVIDA[registro]})


class FindingSerializer(serializers.ModelSerializer[Finding]):
    """O achado, com o estado epistemológico que a metodologia exige (FDD 045, ADR 0049).

    Duas invariantes da ontologia moram aqui, e nenhuma delas cabe inteira no `clean()`:

    - **§6.9 — `fact` exige revisor humano e `Evidence` viva.** A metade do revisor está no
      modelo; a da evidência não pode estar, porque o M2M só existe depois do save e um `clean()`
      que o consultasse recusaria toda criação. `reviewed_by` é campo **do corpo**, e não da
      sessão como `registered_by`: quem promove nem sempre é quem confirmou — o consultor que
      validou em campo pode não ser quem digita —, e o que a invariante exige é que a promoção
      **tenha nome**, não que o nome seja o de quem está logado. Omitir é 400, nunca carimbo
      silencioso.
    - **A transição lê `FINDING_TRANSITIONS`**, no molde do `ARTIFACT_TRANSITIONS`: de `fact` só
      se volta a `hypothesis`, porque ir direto a `unknown` apagaria a diferença entre "estávamos
      errados" e "nunca soubemos".
    - **Promover a `fact` um achado que já está publicado exige evidência *publicada* viva**
      (FDD 051). Sem esta terceira metade a cadeia de publicação vaza pelo `PATCH`: a action
      `publish/` confere a sustentação no instante em que o achado sobe, mas um achado publicado
      como hipótese — que não exige nada — pode virar fato num `PATCH` depois, e o cliente
      passaria a ler "fato" com evidência interna embaixo. `published_at`/`published_by` são só de
      leitura pela mesma razão: quem carimba a marca é a action.
    - **Mover `process`/`step` de um achado publicado para um mapa não publicado é 400** — a quinta
      porta da mesma cadeia, e a única das cinco que não passa perto de `published_at`.
    """

    epistemic_status_display = serializers.CharField(
        source="get_epistemic_status_display", read_only=True
    )
    publication_state = PublicationStateSerializer(source="*", read_only=True)

    class Meta:
        model = Finding
        fields = ["id", "account", "process", "step", "statement", "epistemic_status",
                  "epistemic_status_display", "confidence", "reviewed_by", "reviewed_at",
                  "evidences", "published_at", "published_by", "publication_state",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "epistemic_status_display", "reviewed_at",
                            "published_at", "published_by", "publication_state",
                            "created_at", "updated_at"]

    def validate_epistemic_status(self, value: str) -> str:
        if self.instance is None:
            return value
        atual = self.instance.epistemic_status
        if value != atual and value not in FINDING_TRANSITIONS[atual]:
            raise serializers.ValidationError(
                f"Não é possível ir de {self.instance.get_epistemic_status_display()} para "
                f"{Finding.EpistemicStatus(value).label}."
            )
        return value

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        account = cast(
            Account | None, attrs.get("account", getattr(self.instance, "account", None))
        )
        process = cast(
            Process | None, attrs.get("process", getattr(self.instance, "process", None))
        )
        step = cast(ProcessStep | None, attrs.get("step", getattr(self.instance, "step", None)))
        confidence = cast(
            int | None, attrs.get("confidence", getattr(self.instance, "confidence", None))
        )
        if confidence is not None and not 0 <= confidence <= 100:
            raise serializers.ValidationError({"confidence": "A confiança vai de 0 a 100."})
        if process and account and process.account_id != account.pk:
            raise serializers.ValidationError(
                {"process": "O processo deve pertencer à mesma conta."}
            )
        if step and account and step.process.account_id != account.pk:
            raise serializers.ValidationError({"step": "A etapa deve pertencer à mesma conta."})
        if step and process and step.process_id != process.pk:
            raise serializers.ValidationError({"step": "A etapa deve pertencer ao mesmo processo."})
        _recusa_ancora_nao_publicada(self.instance, attrs, process, step, "finding")

        estado = attrs.get(
            "epistemic_status",
            getattr(self.instance, "epistemic_status", Finding.EpistemicStatus.HYPOTHESIS),
        )
        if estado == Finding.EpistemicStatus.FACT:
            revisor = attrs.get("reviewed_by", getattr(self.instance, "reviewed_by", None))
            if revisor is None:
                raise serializers.ValidationError(
                    {"reviewed_by": "Promover um achado a fato é ato humano: informe quem revisou."}
                )
            # No PATCH que não mexe no M2M, `evidences` não vem no corpo — a pergunta é sobre o
            # que já está ligado. Na criação, sobre o que veio.
            evidencias = attrs.get("evidences")
            if evidencias is None:
                evidencias = list(self.instance.evidences.all()) if self.instance else []
            evidencias = cast(list[Evidence], evidencias)
            if not any(evidence.archived_at is None for evidence in evidencias):
                raise serializers.ValidationError(
                    {"evidences": "Um fato precisa de ao menos uma evidência viva que o sustente."}
                )
            # A porta do `PATCH` da cadeia de publicação (FDD 051). Só vale para o achado que
            # **já está publicado**: um achado interno vira fato com evidência interna, e é assim
            # que deve ser — o que não pode é o que o cliente lê afirmar "fato" com sustentação
            # que não atravessou.
            if self.instance is not None and self.instance.published_at is not None and not any(
                evidence.archived_at is None and evidence.published_at is not None
                for evidence in evidencias
            ):
                raise serializers.ValidationError(
                    {"evidences": "Este achado está publicado: promovê-lo a fato exige ao menos "
                                  "uma evidência publicada e viva. Publique a evidência antes, ou "
                                  "despublique o achado."}
                )
        return attrs


class PainPointSerializer(serializers.ModelSerializer[PainPoint]):
    """A dor observada, com a invariante de `confirmed` (FDD 048).

    **`confirmed` exige ao menos um `Finding` vivo por baixo**, e a checagem mora aqui inteira —
    não por preferência, mas porque ela pergunta pelo M2M, que só existe depois do save. É a mesma
    razão pela qual a metade "evidência viva" da invariante §6.9 está no `FindingSerializer` e não
    no `clean()` do `Finding`. A terceira metade da mesma regra está no arquivamento
    (`FindingViewSet.perform_destroy`): sem ela, a invariante vazaria pelo `DELETE`, que foi como
    a Fase 3 a perdeu da primeira vez.

    `impact_estimate` ausente fica **nulo**, e o serializer não o converte em zero em lugar
    nenhum: zero afirma que a dor não custa nada, e nulo diz que ninguém estimou.

    **A quinta porta da cadeia de publicação também passa por aqui** (FDD 051): mover
    `process`/`step` de uma dor **publicada** para um mapa não publicado é 400 — a dor emite
    `process_id`/`step_id` no snapshot como o achado emite.
    """

    impact_type_display = serializers.CharField(source="get_impact_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    publication_state = PublicationStateSerializer(source="*", read_only=True)

    class Meta:
        model = PainPoint
        fields = ["id", "account", "process", "step", "title", "description", "impact_type",
                  "impact_type_display", "impact_estimate", "findings", "status",
                  "status_display", "published_at", "published_by", "publication_state",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "impact_type_display", "status_display", "published_at",
                            "published_by", "publication_state", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """As mesmas regras do `clean()` — o `save()` do DRF não chama `full_clean` —, mais o M2M."""
        account = cast(
            Account | None, attrs.get("account", getattr(self.instance, "account", None))
        )
        process = cast(
            Process | None, attrs.get("process", getattr(self.instance, "process", None))
        )
        step = cast(ProcessStep | None, attrs.get("step", getattr(self.instance, "step", None)))
        if process and account and process.account_id != account.pk:
            raise serializers.ValidationError(
                {"process": "O processo deve pertencer à mesma conta."}
            )
        if step and account and step.process.account_id != account.pk:
            raise serializers.ValidationError({"step": "A etapa deve pertencer à mesma conta."})
        if step and process and step.process_id != process.pk:
            raise serializers.ValidationError({"step": "A etapa deve pertencer ao mesmo processo."})
        _recusa_ancora_nao_publicada(self.instance, attrs, process, step, "pain_point")

        # No PATCH que não mexe no M2M, `findings` não vem no corpo — a pergunta é sobre o que já
        # está ligado. Na criação, sobre o que veio. Mesma forma do `FindingSerializer`.
        achados = attrs.get("findings")
        if achados is None:
            achados = list(self.instance.findings.all()) if self.instance else []
        achados = cast(list[Finding], achados)
        # A fronteira de conta também vale pelo M2M, e vale aqui pela razão que a FDD 045 deu para
        # validar os quatro vínculos opcionais da `Evidence` em vez de só os dois caros: é a mesma
        # classe de vínculo cruzado, e deixar um solto faria quem lesse isto depois concluir que
        # existe uma razão para a exceção.
        if account and any(achado.account_id != account.pk for achado in achados):
            raise serializers.ValidationError(
                {"findings": "O achado deve pertencer à mesma conta da dor."}
            )

        estado = attrs.get("status", getattr(self.instance, "status", PainPoint.Status.OBSERVED))
        if estado == PainPoint.Status.CONFIRMED and not any(
            achado.archived_at is None for achado in achados
        ):
            raise serializers.ValidationError(
                {"findings": "Confirmar uma dor exige ao menos um achado vivo que a sustente."}
            )
        return attrs


class ImprovementOpportunitySerializer(serializers.ModelSerializer[ImprovementOpportunity]):
    """A oportunidade de melhoria, com o score e o **rank derivado** (FDD 048).

    `score`, `assessment_version` e `rank` são campos calculados e só de leitura. O rank não é
    coluna: um `rank` gravado que precisa concordar com a ordenação por score é uma segunda
    definição da mesma coisa, e ela diverge da primeira em silêncio. Quem o produz é
    `priority.ranking_da_conta`, num lugar só.

    **Sem avaliação, os três saem `null`** — nunca zero. Zero afirma que a oportunidade foi
    avaliada e vale zero; o nulo diz que ninguém avaliou, e é o `—` que o DAP desenhou.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    score = serializers.SerializerMethodField()
    assessment_version = serializers.SerializerMethodField()
    rank = serializers.SerializerMethodField()
    publication_state = PublicationStateSerializer(source="*", read_only=True)
    # `conta -> {id da oportunidade: posição}`, memorizado por serialização.
    _ranking_cache: dict[int, dict[int, int]] | None = None

    class Meta:
        model = ImprovementOpportunity
        fields = ["id", "account", "engagement", "title", "desired_change", "impact_hypothesis",
                  "pain_points", "status", "status_display", "score", "assessment_version",
                  "rank", "published_at", "published_by", "publication_state",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "status_display", "score", "assessment_version", "rank",
                            "published_at", "published_by", "publication_state",
                            "created_at", "updated_at"]

    @extend_schema_field(serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True))
    def get_score(self, obj: ImprovementOpportunity) -> str | None:
        vigente = obj.current_assessment
        return str(vigente.score) if vigente else None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_assessment_version(self, obj: ImprovementOpportunity) -> int | None:
        """A versão da avaliação vigente. Vai junto do score de propósito (DAP, decisão B1): um
        número sem a versão ao lado é um número que não se pode comparar com o da semana
        passada."""
        vigente = obj.current_assessment
        return vigente.version if vigente else None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_rank(self, obj: ImprovementOpportunity) -> int | None:
        return self._ranking(obj.account_id).get(obj.pk)

    def _ranking(self, account_id: int) -> dict[int, int]:
        """Um ranking por conta e por serialização. O `ListSerializer` reusa **um** filho para
        todos os itens, então o cache de instância cobre a lista inteira sem recalcular por
        linha."""
        if self._ranking_cache is None:
            self._ranking_cache = {}
        if account_id not in self._ranking_cache:
            self._ranking_cache[account_id] = ranking_da_conta(account_id)
        return self._ranking_cache[account_id]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        account = cast(
            Account | None, attrs.get("account", getattr(self.instance, "account", None))
        )
        engagement = cast(
            Engagement | None, attrs.get("engagement", getattr(self.instance, "engagement", None))
        )
        if engagement and account and engagement.account_id != account.pk:
            raise serializers.ValidationError(
                {"engagement": "O engajamento deve pertencer à mesma conta da oportunidade."}
            )
        dores = cast(list[PainPoint] | None, attrs.get("pain_points"))
        if account and dores and any(dor.account_id != account.pk for dor in dores):
            raise serializers.ValidationError(
                {"pain_points": "A dor deve pertencer à mesma conta da oportunidade."}
            )
        return attrs


class PriorityAssessmentSerializer(serializers.ModelSerializer[PriorityAssessment]):
    """A avaliação que produz o Opportunity Score (FDD 048, ADR 0054).

    `version`, `weights` e `score` são **só de leitura**: os três saem do `save()` do modelo. Um
    score que o corpo pudesse informar não seria a fórmula, seria uma opinião com aparência de
    cálculo; e uma versão escolhida por quem escreve deixaria de ser uma sequência.

    A imutabilidade não mora aqui — mora na rota, que não expõe `PUT` nem `PATCH`. Um serializer
    que recusasse toda atualização ainda deixaria `PUT` responder 400 em vez de 405, e 400 diz
    "corrija o corpo" sobre uma operação que não existe.

    **`assessed_by_name` existe porque a tela precisa dizer quem avaliou, e o id não diz.** O board
    aprovado (`docs/design/dap-priorizacao-r1/`) mostra "Avaliado por {nome} em {data}", e resolver
    o nome pelo cliente exigiria `/users/`, que é fechada à Entrega — metade de quem lê a tela
    veria um número. É o mesmo `source="…get_full_name"` de `owner_name` em `KnowledgeArea` e
    `user_name` em `ProjectMember`: campo derivado, só de leitura, aditivo à `/api/v1/`.
    """

    assessed_by_name = serializers.CharField(
        source="assessed_by.get_full_name", read_only=True, default=""
    )

    class Meta:
        model = PriorityAssessment
        fields = ["id", "improvement_opportunity", "version", "impact", "evidence_strength",
                  "feasibility", "time_to_value", "economics", "formula_key", "weights", "score",
                  "rationale", "assessed_by", "assessed_by_name", "created_at", "updated_at"]
        read_only_fields = ["id", "version", "weights", "score", "assessed_by",
                            "assessed_by_name", "created_at", "updated_at"]

    def validate_formula_key(self, value: str) -> str:
        if value not in FORMULAS:
            raise serializers.ValidationError(
                f"Fórmula desconhecida. As disponíveis são: {', '.join(sorted(FORMULAS))}."
            )
        return value


class SolutionHypothesisSerializer(serializers.ModelSerializer[SolutionHypothesis]):
    """A hipótese de solução, com a unicidade da **escolhida** (FDD 048).

    A checagem de "já existe uma escolhida" é escrita à mão, ao contrário do que o `CLAUDE.md`
    pede para as constraints do pipeline, e a exceção tem motivo verificado: o DRF só deriva o
    validador de uma `UniqueConstraint` condicional quando **todos** os campos da condição são
    campos do serializer (`ModelSerializer.get_unique_together_validators`, DRF 3.16). A condição
    aqui cita `archived_at`, que nenhum serializer da casa expõe — então o validador é descartado
    em silêncio e o `IntegrityError` subiria como 500. A constraint continua sendo a garantia; o
    que esta validação faz é transformar a recusa num 400 legível.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = SolutionHypothesis
        fields = ["id", "improvement_opportunity", "statement", "intervention", "assumptions",
                  "expected_effect", "status", "status_display", "created_at", "updated_at"]
        read_only_fields = ["id", "status_display", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        oportunidade = cast(
            ImprovementOpportunity | None,
            attrs.get(
                "improvement_opportunity",
                getattr(self.instance, "improvement_opportunity", None),
            ),
        )
        estado = attrs.get(
            "status", getattr(self.instance, "status", SolutionHypothesis.Status.PROPOSED)
        )
        if estado != SolutionHypothesis.Status.CHOSEN or oportunidade is None:
            return attrs
        concorrentes = SolutionHypothesis.objects.filter(
            improvement_opportunity=oportunidade,
            status=SolutionHypothesis.Status.CHOSEN,
            archived_at__isnull=True,
        )
        if self.instance is not None:
            concorrentes = concorrentes.exclude(pk=self.instance.pk)
        if concorrentes.exists():
            raise serializers.ValidationError(
                {"status": "Esta oportunidade já tem uma hipótese escolhida. Descarte a atual "
                           "antes de escolher outra."}
            )
        return attrs


class BusinessCaseSerializer(serializers.ModelSerializer[BusinessCase]):
    """A justificativa do investimento (FDD 053, ADR 0069).

    A lista de `read_only_fields` é a entrega, e não burocracia — é a mesma do `CaseSerializer`
    pelo mesmo motivo. `current_state_cost` e `current_state_cost_source` são a fotografia do
    custo no instante da criação: mantê-los fora da escrita é o que faz "o número não muda depois
    de congelado" ser verdade **por construção**, sem caminho de escrita em vez de com um caminho
    que ninguém usa. `status`, `decided_at` e `decided_by` ficam de fora pelo motivo oposto: eles
    *devem* mudar, mas só pela action `decide/`, que grava quem decidiu.

    O `validate()` repete as duas guardas do `clean()` porque o serializer não chama `full_clean()`
    — é a porta dupla que o `ImprovementOpportunitySerializer` já mantém para a mesma classe de
    vínculo cruzado. Nenhuma constraint cobre estas duas: sem a repetição aqui, a recusa
    simplesmente **não aconteceria** pela rota, e o `clean()` seguiria valendo só para shell,
    admin e migração.

    **O dinheiro sai como texto**: os três campos decimais passam por `ModelSerializer`, então o
    `COERCE_DECIMAL_TO_STRING` do DRF os emite como string (ADR 0068). O `dict` cru de
    `current_state_cost_source` não passa por lá — quem cuida dele é `business_case.dinheiro()`, na
    origem.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    # Derivado, e não campo: a conta chega pela oportunidade, um hop — o mesmo caminho que a
    # queryset e a permissão de objeto percorrem. Publicá-lo poupa a tela de uma segunda chamada
    # só para saber de quem é a linha.
    account = serializers.IntegerField(
        source="improvement_opportunity.account_id", read_only=True
    )
    # **`decided_by_name` existe porque a decisão de investir é ato com autor, e o id não o diz.**
    # É a metade que o `clean()` protege — `approved` sem `decided_by` é recusado —, e uma tela que
    # mostrasse "Aprovado em 05/09" sem dizer por quem devolveria à conversa a pergunta que este
    # modelo existe para responder. O board aprovado mostra "Aprovado por {nome} em {data}".
    # Mesmo `source="…get_full_name"` de `assessed_by_name` aqui ao lado, de `owner_name` em
    # `KnowledgeArea` e de `user_name` em `ProjectMember`: derivado, só leitura, aditivo à v1 —
    # resolver o nome pelo cliente exigiria `/users/`, que é fechada à Entrega.
    decided_by_name = serializers.CharField(
        source="decided_by.get_full_name", read_only=True, default=""
    )

    class Meta:
        model = BusinessCase
        fields = ["id", "improvement_opportunity", "account", "solution_hypothesis",
                  "priority_assessment", "investment", "expected_return_year", "payback_months",
                  "current_state_cost", "current_state_cost_source", "rationale", "assumptions",
                  "status", "status_display", "decided_at", "decided_by", "decided_by_name",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "account", "current_state_cost", "current_state_cost_source",
                            "status", "status_display", "decided_at", "decided_by",
                            "decided_by_name", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        oportunidade = cast(
            ImprovementOpportunity | None,
            attrs.get(
                "improvement_opportunity",
                getattr(self.instance, "improvement_opportunity", None),
            ),
        )
        hipotese = cast(
            SolutionHypothesis | None,
            attrs.get("solution_hypothesis", getattr(self.instance, "solution_hypothesis", None)),
        )
        avaliacao = cast(
            PriorityAssessment | None,
            attrs.get("priority_assessment", getattr(self.instance, "priority_assessment", None)),
        )
        if oportunidade is None:
            return attrs
        if hipotese is not None and hipotese.improvement_opportunity_id != oportunidade.pk:
            raise serializers.ValidationError(
                {"solution_hypothesis": "A hipótese deve ser da mesma oportunidade de melhoria."}
            )
        if avaliacao is not None and avaliacao.improvement_opportunity_id != oportunidade.pk:
            raise serializers.ValidationError(
                {"priority_assessment": "A avaliação deve ser da mesma oportunidade de melhoria."}
            )
        return attrs


class FeasibilityAssessmentSerializer(serializers.ModelSerializer[FeasibilityAssessment]):
    """O laudo de Feasibility (FDD 049).

    `gate_decision` publica as **quatro** saídas da Feasibility, e não as sete do campo da fase:
    aqui não há ambiguidade sobre de que gate se trata, então a `ChoiceField` derivada do campo já
    recusa `scale` com 400 — a validação que `journey.apply_gate` precisa fazer à mão porque lá o
    vocabulário depende da fase ativa.
    """

    technical_verdict_display = serializers.CharField(
        source="get_technical_verdict_display", read_only=True
    )
    operational_verdict_display = serializers.CharField(
        source="get_operational_verdict_display", read_only=True
    )
    economic_verdict_display = serializers.CharField(
        source="get_economic_verdict_display", read_only=True
    )
    gate_decision_display = serializers.CharField(
        source="get_gate_decision_display", read_only=True
    )

    class Meta:
        model = FeasibilityAssessment
        fields = ["id", "solution_hypothesis", "project",
                  "technical_verdict", "technical_verdict_display", "technical_note",
                  "operational_verdict", "operational_verdict_display", "operational_note",
                  "economic_verdict", "economic_verdict_display", "economic_note",
                  "sample", "error_classes", "evidence",
                  "gate_decision", "gate_decision_display", "created_at", "updated_at"]
        read_only_fields = ["id", "technical_verdict_display", "operational_verdict_display",
                            "economic_verdict_display", "gate_decision_display",
                            "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """As mesmas regras do `clean()` — o `save()` do DRF não chama `full_clean` —, mais o M2M."""
        hipotese = cast(
            SolutionHypothesis | None,
            attrs.get(
                "solution_hypothesis", getattr(self.instance, "solution_hypothesis", None)
            ),
        )
        projeto = cast(
            Project | None, attrs.get("project", getattr(self.instance, "project", None))
        )
        if hipotese and projeto:
            conta_id = hipotese.improvement_opportunity.account_id
            if conta_id != projeto.engagement.account_id:
                raise serializers.ValidationError(
                    {"solution_hypothesis": "A hipótese deve pertencer à mesma conta do projeto."}
                )
            # A fronteira de conta vale pelo M2M também, pela razão que a FDD 048 deu para validar
            # `PainPoint.findings`: é a mesma classe de vínculo cruzado, e deixar um solto faria
            # quem lesse isto depois concluir que existe uma razão para a exceção.
            evidencias = cast(list[Evidence] | None, attrs.get("evidence"))
            if evidencias and any(item.account_id != conta_id for item in evidencias):
                raise serializers.ValidationError(
                    {"evidence": "A evidência deve pertencer à mesma conta do laudo."}
                )
        return attrs


class ProveExperimentSerializer(serializers.ModelSerializer[ProveExperiment]):
    """O experimento do PROVE, com a lista do que falta para ele começar (FDD 049).

    **`status` não recebe `running` por `PATCH`**, e é o que impede a invariante de vazar pela
    porta do formulário: iniciar é a action `start/`, que confere KPI, critério e baseline e
    carimba a lacuna aprovada. Um campo gravável aqui seria um segundo caminho para o mesmo estado
    — o defeito que a decisão C1 do DAP remove do `DigitalEmployee` sendo reintroduzido ao lado.
    Os demais valores continuam graváveis: `planned → concluded` é registro, não início.

    `missing_to_start` é derivado e só de leitura, de `prove.o_que_falta_para_iniciar`. É a mesma
    lista que a action usa para recusar, e é por isso que ela sai daqui em vez de a tela
    recalculá-la: duas expressões da invariante divergiriam, e a tela habilitaria o botão que o
    servidor nega.

    `gap_waiver_at` é **só de leitura**: o carimbo é consequência do ato, e preenchê-lo à mão
    permitiria uma data de aprovação anterior à aprovação (mesma forma do `published_at` do
    `Case`). `gap_waiver_by` vem do corpo, e não da sessão, pelo motivo do `reviewed_by` do
    `Finding`: ele responde "quem aprovou", que pode não ser quem digita.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    gate_decision_display = serializers.CharField(
        source="get_gate_decision_display", read_only=True
    )
    missing_to_start = serializers.SerializerMethodField()

    class Meta:
        model = ProveExperiment
        fields = ["id", "solution_hypothesis", "project", "controlled_scope", "started_at",
                  "ended_at", "success_criteria", "status", "status_display",
                  "gate_decision", "gate_decision_display", "gap_waiver", "gap_waiver_by",
                  "gap_waiver_at", "missing_to_start", "created_at", "updated_at"]
        read_only_fields = ["id", "status_display", "gate_decision_display", "gap_waiver_at",
                            "missing_to_start", "created_at", "updated_at"]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_missing_to_start(self, obj: ProveExperiment) -> list[str]:
        return o_que_falta_para_iniciar(obj)

    def validate_status(self, value: str) -> str:
        se_ja_esta = getattr(self.instance, "status", None)
        if value == ProveExperiment.Status.RUNNING and value != se_ja_esta:
            raise serializers.ValidationError(
                "Iniciar o PROVE é a ação `start/`, que confere KPI, critério de sucesso e "
                "baseline — ou registra a lacuna aprovada."
            )
        return value

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        hipotese = cast(
            SolutionHypothesis | None,
            attrs.get(
                "solution_hypothesis", getattr(self.instance, "solution_hypothesis", None)
            ),
        )
        projeto = cast(
            Project | None, attrs.get("project", getattr(self.instance, "project", None))
        )
        if hipotese and projeto and hipotese.improvement_opportunity.account_id != projeto.engagement.account_id:
            raise serializers.ValidationError(
                {"solution_hypothesis": "A hipótese deve pertencer à mesma conta do projeto."}
            )
        inicio = cast(
            date | None, attrs.get("started_at", getattr(self.instance, "started_at", None))
        )
        fim = cast(date | None, attrs.get("ended_at", getattr(self.instance, "ended_at", None)))
        if inicio and fim and fim < inicio:
            raise serializers.ValidationError(
                {"ended_at": "O fim não pode ser anterior ao início."}
            )
        return attrs


class KPISerializer(serializers.ModelSerializer[KPI]):
    """O indicador (FDD 049, ADR 0055).

    `project` é a âncora obrigatória e `prove_experiment` é opcional — **desvio deliberado** da
    lista de campos da issue #69, cuja razão é a migração e está na docstring do modelo. Quando o
    experimento vier, ele tem de ser do mesmo projeto.
    """

    unit_display = serializers.CharField(source="get_unit_display", read_only=True)
    direction_display = serializers.CharField(source="get_direction_display", read_only=True)

    class Meta:
        model = KPI
        fields = ["id", "project", "prove_experiment", "name", "definition", "formula",
                  "unit", "unit_display", "direction", "direction_display", "data_source",
                  "cadence", "owner", "target", "created_at", "updated_at"]
        read_only_fields = ["id", "unit_display", "direction_display", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        projeto = cast(
            Project | None, attrs.get("project", getattr(self.instance, "project", None))
        )
        experimento = cast(
            ProveExperiment | None,
            attrs.get("prove_experiment", getattr(self.instance, "prove_experiment", None)),
        )
        if experimento and projeto and experimento.project_id != projeto.pk:
            raise serializers.ValidationError(
                {"prove_experiment": "O experimento deve pertencer ao mesmo projeto do KPI."}
            )
        return attrs


class MeasurementSerializer(serializers.ModelSerializer[Measurement]):
    """Uma leitura do KPI — baseline, outcome ou monitoramento (FDD 049).

    **`value` ausente fica nulo, e o serializer não o converte em zero em lugar nenhum**: zero
    afirma que o processo não custava nada antes, e nulo diz que ninguém mediu. É a mesma
    distinção de `PainPoint.impact_estimate` e do `nao_apurado` de `process.custo_do_estado_atual`.

    A checagem de "já existe baseline viva" é escrita à mão, ao contrário do que o `CLAUDE.md`
    pede para as constraints do pipeline, e a exceção tem o mesmo motivo verificado da
    `SolutionHypothesisSerializer`: o DRF só deriva o validador de uma `UniqueConstraint`
    condicional quando **todos** os campos da condição são campos do serializer
    (`ModelSerializer.get_unique_together_validators`, DRF 3.16). A condição cita `archived_at`,
    que nenhum serializer da casa expõe — então o validador é descartado em silêncio e o
    `IntegrityError` subiria como 500. A constraint continua sendo a garantia; isto transforma a
    recusa num 400 legível.

    **Não há `unit` aqui, e a ausência é a garantia.** Ver a docstring de `Measurement`.
    """

    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = Measurement
        fields = ["id", "kpi", "kind", "kind_display", "value", "period_start", "period_end",
                  "measured_at", "source_evidence", "confidence", "created_at", "updated_at"]
        read_only_fields = ["id", "kind_display", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        inicio = cast(
            date | None, attrs.get("period_start", getattr(self.instance, "period_start", None))
        )
        fim = cast(
            date | None, attrs.get("period_end", getattr(self.instance, "period_end", None))
        )
        if inicio and fim and fim < inicio:
            raise serializers.ValidationError(
                {"period_end": "O fim da janela não pode ser anterior ao início."}
            )
        kpi = cast(KPI | None, attrs.get("kpi", getattr(self.instance, "kpi", None)))
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if kind != Measurement.Kind.BASELINE or kpi is None:
            return attrs
        concorrentes = Measurement.objects.filter(
            kpi=kpi, kind=Measurement.Kind.BASELINE, archived_at__isnull=True
        )
        if self.instance is not None:
            concorrentes = concorrentes.exclude(pk=self.instance.pk)
        if concorrentes.exists():
            raise serializers.ValidationError(
                {"kind": "Este KPI já tem uma baseline viva. Arquive a atual antes de registrar "
                         "outra — duas bases fariam a comparação depender de qual foi aberta."}
            )
        return attrs


class ValueLedgerEntrySerializer(serializers.ModelSerializer[ValueLedgerEntry]):
    """A entrada do Value Ledger (FDD 049, `language-map` §6.11 e §6.12).

    Três recusas, e nenhuma é decorativa:

    - **a medição apontada tem de ser um `outcome`** — uma entrada que apontasse para o baseline
      afirmaria resultado onde há ponto de partida, e a leitura da tela não denunciaria nada;
    - **`attribution_method` não pode ser vazio** — é ele que separa valor medido de número
      escrito à mão, e "ROI" como resultado é termo banido (§5) por causa disso;
    - **`approved` exige `approved_by`** — aprovar é ato com autor, e o carimbo `approved_at` sai
      do `save()` do modelo, como o `published_at` do `Case`.
    """

    value_type_display = serializers.CharField(source="get_value_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = ValueLedgerEntry
        fields = ["id", "engagement", "project", "outcome_measurement", "value_type",
                  "value_type_display", "amount", "quantity", "period_start", "period_end",
                  "attribution_method", "status", "status_display", "approved_by", "approved_at",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "value_type_display", "status_display", "approved_at",
                            "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        medicao = cast(
            Measurement | None,
            attrs.get("outcome_measurement", getattr(self.instance, "outcome_measurement", None)),
        )
        if medicao is not None and medicao.kind != Measurement.Kind.OUTCOME:
            raise serializers.ValidationError(
                {"outcome_measurement": "A entrada de valor aponta para uma medição do tipo "
                                        "Outcome; baseline e monitoramento não afirmam resultado."}
            )
        metodo = cast(
            str,
            attrs.get(
                "attribution_method", getattr(self.instance, "attribution_method", "")
            ),
        )
        if not (metodo or "").strip():
            raise serializers.ValidationError(
                {"attribution_method": "Descreva o método de atribuição — é ele que separa valor "
                                       "medido de número escrito à mão."}
            )
        engagement = cast(
            Engagement | None,
            attrs.get("engagement", getattr(self.instance, "engagement", None)),
        )
        projeto = cast(
            Project | None, attrs.get("project", getattr(self.instance, "project", None))
        )
        if projeto is not None and engagement is not None and projeto.engagement_id != engagement.pk:
            raise serializers.ValidationError(
                {"project": "O projeto deve pertencer ao mesmo engajamento da entrada."}
            )
        inicio = cast(
            date | None, attrs.get("period_start", getattr(self.instance, "period_start", None))
        )
        fim = cast(
            date | None, attrs.get("period_end", getattr(self.instance, "period_end", None))
        )
        if inicio and fim and fim < inicio:
            raise serializers.ValidationError(
                {"period_end": "O fim da janela não pode ser anterior ao início."}
            )
        estado = attrs.get("status", getattr(self.instance, "status", ValueLedgerEntry.Status.DRAFT))
        autor = attrs.get("approved_by", getattr(self.instance, "approved_by", None))
        if estado == ValueLedgerEntry.Status.APPROVED and autor is None:
            raise serializers.ValidationError(
                {"approved_by": "Aprovar é ato com autor: informe quem aprovou."}
            )
        return attrs


class TaskSerializer(WorkItemSerializer):
    class Meta(WorkItemSerializer.Meta):
        model = Task
        fields = WorkItemSerializer.Meta.fields + ["milestone"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        project = cast(Project | None, attrs.get("project", getattr(self.instance, "project", None)))
        milestone = cast(Milestone | None, attrs.get("milestone", getattr(self.instance, "milestone", None)))
        if milestone and project and milestone.project_id != project.id:
            raise serializers.ValidationError({"milestone": "O marco deve pertencer ao mesmo projeto."})
        return attrs


class EngineeringHandoffSerializer(serializers.ModelSerializer[EngineeringHandoff]):
    """Handoff de engenharia (FDD 040). Número/URL/status saem do provisionamento, não do corpo.

    O UniqueValidator de `pulse_work_item_id` é desligado de propósito: o POST duplicado não é
    400, é 200 no registro existente — a chave de idempotência.
    """

    class Meta:
        model = EngineeringHandoff
        fields = [
            "id", "project", "source_task", "pulse_work_item_id", "title", "objective",
            "context", "acceptance_criteria", "scope_text", "out_of_scope_text",
            "repository", "milestone_ref", "adr_refs", "nfr_refs", "fdd_refs",
            "correlation_id", "github_issue_number", "github_issue_url", "github_node_id",
            "status", "attempt_count", "last_attempt_at", "last_error_code",
            "last_error_message", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "correlation_id", "github_issue_number", "github_issue_url",
            "github_node_id", "status", "attempt_count", "last_attempt_at",
            "last_error_code", "last_error_message", "created_at", "updated_at",
        ]
        extra_kwargs: dict[str, dict[str, list[object]]] = {
            "pulse_work_item_id": {"validators": []},
        }
        validators: list[Any] = []

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        def valor(campo: str, default: object = "") -> object:
            if campo in attrs:
                return attrs[campo]
            if self.instance is not None:
                return getattr(self.instance, campo)
            return default

        if "pulse_work_item_id" in attrs:
            attrs["pulse_work_item_id"] = str(attrs["pulse_work_item_id"] or "").strip()
        pulse_id = str(valor("pulse_work_item_id") or "").strip()
        if self.instance is not None and "pulse_work_item_id" in attrs:
            if pulse_id != self.instance.pulse_work_item_id:
                raise serializers.ValidationError(
                    {"pulse_work_item_id": "O identificador Pulse não muda depois de criado."}
                )

        instancia = EngineeringHandoff(
            project=cast(Project | None, valor("project", None)),
            source_task=cast(Task | None, valor("source_task", None)),
            pulse_work_item_id=pulse_id,
            title=str(valor("title") or ""),
            objective=str(valor("objective") or ""),
            context=str(valor("context") or ""),
            acceptance_criteria=str(valor("acceptance_criteria") or ""),
            scope_text=str(valor("scope_text") or ""),
            out_of_scope_text=str(valor("out_of_scope_text") or ""),
            repository=str(valor("repository") or ""),
            milestone_ref=str(valor("milestone_ref") or ""),
            adr_refs=valor("adr_refs", []),
            nfr_refs=valor("nfr_refs", []),
            fdd_refs=valor("fdd_refs", []),
            status=str(valor("status", EngineeringHandoff.Status.PENDING) or ""),
            github_issue_number=cast(int | None, valor("github_issue_number", None)),
            github_issue_url=str(valor("github_issue_url") or ""),
        )
        try:
            instancia.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            ) from exc
        return attrs


class GithubDeliveryProjectionSerializer(
    serializers.ModelSerializer[GithubDeliveryProjection]
):
    """Projeção de entrega GitHub (FDD 041). O corpo só escreve o **mapeamento**.

    `project`, `handoff`, `repository` e `issue_number` são a referência canônica; todo o estado de
    engenharia (`issue_state`, `pr_state`, `head_sha`, `ci_state`, ...) é **somente-leitura** — quem
    o move é o webhook ou a reconciliação, nunca um PATCH do Pulse. É a fronteira da ADR 0046 escrita
    no serializer: uma edição normal do Pulse não reescreve o estado do GitHub.

    `repository`/`issue_number` não mudam depois de criados: re-ancorar uma projeção é reescrever
    qual Issue ela espelha, e isso está fora deste recorte.
    """

    state = serializers.SerializerMethodField()
    stale_after_seconds = serializers.SerializerMethodField()

    class Meta:
        model = GithubDeliveryProjection
        fields = [
            "id", "project", "handoff", "repository", "issue_number", "issue_url",
            "projection_status", "state", "stale_after_seconds",
            "issue_state", "pr_state", "pr_number", "pr_url", "head_sha", "head_ref",
            "review_state", "ci_state", "observed_at", "last_event_at",
            "last_delivery_id", "last_event_type", "last_error_code", "last_error_message",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "issue_url", "projection_status", "issue_state", "pr_state", "pr_number",
            "pr_url", "head_sha", "head_ref", "review_state", "ci_state", "observed_at",
            "last_event_at", "last_delivery_id", "last_event_type", "last_error_code",
            "last_error_message", "created_at", "updated_at",
        ]

    @extend_schema_field(serializers.CharField())
    def get_state(self, obj: GithubDeliveryProjection) -> str:
        from django.conf import settings

        return obj.display_state(
            int(getattr(settings, "GITHUB_PROJECTION_STALE_AFTER_SECONDS", 3600))
        )

    @extend_schema_field(serializers.IntegerField())
    def get_stale_after_seconds(self, obj: GithubDeliveryProjection) -> int:
        from django.conf import settings

        return int(getattr(settings, "GITHUB_PROJECTION_STALE_AFTER_SECONDS", 3600))

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.instance is not None:
            for campo in ("repository", "issue_number"):
                if campo in attrs and attrs[campo] != getattr(self.instance, campo):
                    raise serializers.ValidationError(
                        {campo: "A referência da Issue não muda depois de criada."}
                    )

        def valor(campo: str, default: object = "") -> object:
            if campo in attrs:
                return attrs[campo]
            if self.instance is not None:
                return getattr(self.instance, campo)
            return default

        instancia = GithubDeliveryProjection(
            project=cast(Project | None, valor("project", None)),
            handoff=cast(EngineeringHandoff | None, valor("handoff", None)),
            repository=str(valor("repository") or ""),
            issue_number=cast(int, valor("issue_number", 0) or 0),
        )
        try:
            instancia.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            ) from exc
        return attrs


class SignatureRequestSerializer(serializers.ModelSerializer[SignatureRequest]):
    class Meta:
        model = SignatureRequest
        fields = [
            "id", "signer_email", "signer_role", "status", "sign_url", "reminded_at", "signed_at",
            "created_at",
        ]
        read_only_fields = fields


# Tipos aceitos no upload de documento (FDD 017). O download já força `as_attachment`, mas o
# arquivo também segue para o Drive e para o fornecedor de assinatura — lugares onde um
# `.html`/`.svg` volta a ser servido como página.
ALLOWED_DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
    ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".zip",
})

_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _safe_original_name(name: str | None) -> str:
    """Nome de arquivo é entrada do usuário e viaja para o Drive, o e-sign e o portal do cliente.

    O download não é o risco (o Django já faz `basename` e escapa o header); os outros
    consumidores é que recebem o valor cru.
    """
    cleaned = _UNSAFE_NAME_CHARS.sub("", (name or "").replace("\\", "/"))
    cleaned = os.path.basename(cleaned).strip().lstrip(".")
    return cleaned[:255] or "documento"


class DocumentSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Document]):
    ALIASES_DE_ENTRADA = {"opportunity": "commercial_opportunity", "client": "account"}

    # Aliases de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): as chaves antigas saem
    # com o mesmo valor das canônicas e morrem na `/api/v2/`. A escrita vem do mixin acima.
    opportunity = serializers.PrimaryKeyRelatedField(
        source="commercial_opportunity", read_only=True
    )
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)
    signature_requests = SignatureRequestSerializer(many=True, read_only=True)
    originated_engagement = serializers.SerializerMethodField()
    # A conta-dona **derivada** (DAP `dap-assinatura-com-papeis-r1`, B1) — não é a chave `client`,
    # que é alias de leitura do vínculo direto. São fatos diferentes e não compartilham nome: um
    # contrato pendurado em oportunidade ou projeto sai com `client: null` e conta-dona preenchida.
    owning_account = serializers.SerializerMethodField()
    signature_positioning_gap = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id", "account", "client", "commercial_opportunity", "opportunity", "project", "kind",
            "file", "drive_link", "original_name",
            "uploaded_by", "created_at", "signature_requests", "originated_engagement",
            "owning_account", "signature_positioning_gap",
        ]
        read_only_fields = ["id", "drive_link", "original_name", "uploaded_by", "created_at"]

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_originated_engagement(self, obj: Document) -> int | None:
        try:
            return obj.originated_design_partner_engagement.pk
        except Engagement.DoesNotExist:
            return None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_owning_account(self, obj: Document) -> int | None:
        # `drive.account_of` é o lugar único da cadeia conta → oportunidade → projeto.engagement;
        # reexpressá-la aqui seria a segunda definição de "de quem é este documento".
        account = drive.account_of(obj)
        return account.pk if account is not None else None

    @extend_schema_field(
        serializers.ChoiceField(choices=["not_pdf", "kind_without_block"], allow_null=True)
    )
    def get_signature_positioning_gap(self, obj: Document) -> str | None:
        return esign.lacuna_de_posicionamento(obj)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        links = [
            attrs.get("account"), attrs.get("commercial_opportunity"), attrs.get("project")
        ]
        if sum(value is not None for value in links) != 1:
            raise serializers.ValidationError("Vincule o documento a exatamente um cliente, oportunidade ou projeto.")
        escolhido = cast(str, attrs.get("kind") or "")
        if escolhido in DOCUMENT_KINDS_QUE_ABREM_ENGAGEMENT and attrs.get("account") is None:
            # Mesma razão do `Document.clean()`: o rótulo vem do valor escolhido.
            raise serializers.ValidationError({
                "kind": f"O documento marcado como «{Document.Kind(escolhido).label}» deve "
                        "estar vinculado a uma conta, nunca a uma oportunidade ou projeto."
            })
        uploaded_file = cast(UploadedFile | None, attrs.get("file"))
        if uploaded_file is None:
            raise serializers.ValidationError({"file": "Envie um arquivo."})
        if (uploaded_file.size or 0) > 10 * 1024 * 1024:
            raise serializers.ValidationError({"file": "O arquivo excede o limite de 10 MB."})
        extension = os.path.splitext(uploaded_file.name or "")[1].lower()
        if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
            raise serializers.ValidationError({
                "file": "Tipo de arquivo não aceito. Aceitos: "
                        f"{', '.join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))}."
            })
        return attrs

    def create(self, validated_data: dict[str, object]) -> Document:
        uploaded_file = cast(UploadedFile, validated_data.pop("file"))
        # O único momento em que os bytes estão em mãos (`Document.content_is_pdf`). O `seek(0)`
        # não é higiene: `drive.upload_document` faz `uploaded_file.read()` cru, e sem rebobinar
        # o Drive receberia o arquivo sem os cinco primeiros bytes — falha muda, porque aquele
        # caminho é I/O e não roda nos testes.
        prefixo = uploaded_file.read(5)
        uploaded_file.seek(0)
        document = Document(
            **validated_data,
            original_name=_safe_original_name(uploaded_file.name),
            content_is_pdf=conteudo_e_pdf(prefixo),
            uploaded_by=self.context["request"].user,
        )
        if drive.is_enabled():
            try:
                document.drive_file_id, document.drive_link = drive.upload_document(
                    document, uploaded_file
                )
            except drive.DriveProviderError as exc:
                # Era o único ponto de integração num caminho de **escrita** sem tratamento:
                # credencial errada ou pasta inexistente davam 500 mudo e o arquivo do usuário
                # sumia. 502 diz de quem é o problema (o fornecedor, não o pedido) e deixa claro
                # que vale repetir — nada é gravado pela metade, porque o `save()` vem depois.
                #
                # **Tipo estreito, como no `download` da rodada 3.** Aqui estava o `except
                # Exception` genérico, e num caminho de escrita ele engolia também o defeito nosso
                # — o `ValueError` de documento sem conta-dona, um `KeyError` na resposta — e o
                # devolvia como "o Drive está fora", que é mentira sobre de quem é o problema.
                # Estreitar só ficou honesto quando `drive.upload_document` passou a embrulhar a
                # conversa com o Google, como `download_document` já fazia: sem isso, a recusa do
                # fornecedor subiria como a família crua do SDK e viraria 500.
                logger.exception("upload ao Drive falhou para %s", document.original_name)
                raise DriveUnavailable(
                    "O Google Drive não aceitou o arquivo agora. Tente de novo em instantes."
                ) from exc
        else:
            document.file = uploaded_file
        document.save()
        return document


class ProjectMemberSerializer(serializers.ModelSerializer[ProjectMember]):
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)
    user_username = serializers.CharField(source="user.username", read_only=True)
    user_role = serializers.CharField(source="user.role", read_only=True)

    class Meta:
        model = ProjectMember
        fields = ["id", "project", "user", "user_name", "user_username", "user_role",
                  "added_by", "created_at"]
        read_only_fields = ["id", "added_by", "created_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        # A constraint do banco é condicional ao arquivamento; aqui é só para o erro sair
        # como mensagem de campo em vez de 500.
        project = cast(Project | None, attrs.get("project"))
        user = cast(User | None, attrs.get("user"))
        if ProjectMember.objects.filter(
            project=project, user=user, archived_at__isnull=True
        ).exists():
            raise serializers.ValidationError("Esta pessoa já está na equipe do projeto.")
        return attrs


class ServiceSerializer(serializers.ModelSerializer[Service]):
    tier_display = serializers.CharField(source="get_tier_display", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = Service
        fields = ["id", "name", "active", "tier", "tier_display", "category", "category_display",
                  "list_price", "summary", "created_at", "updated_at"]
        # A unicidade do `tier` ativo vem da UniqueConstraint do modelo; o DRF a deriva
        # sozinho (respeitando a condição), como no `PipelineStage`.
        read_only_fields = ["id", "tier_display", "category_display", "created_at", "updated_at"]


class VerticalSerializer(serializers.ModelSerializer[Vertical]):
    """Setor do cliente (config admin, vocabulário editável — FDD 026)."""

    class Meta:
        model = Vertical
        fields = ["id", "name", "slug", "position", "active"]
        read_only_fields = ["id"]


class BlueprintVariantSerializer(serializers.ModelSerializer[BlueprintVariant]):
    """Parametrização de um blueprint por vertical. Campo em branco herda o do blueprint."""

    vertical_name = serializers.CharField(source="vertical.name", read_only=True)

    class Meta:
        model = BlueprintVariant
        fields = ["id", "blueprint", "vertical", "vertical_name", "description", "kpi_label",
                  "default_hours_saved_month", "default_roi_month"]
        # A unicidade (blueprint, vertical) vem da UniqueConstraint do modelo; o DRF a deriva
        # sozinho, como faz com o `tier` do `Service`.
        read_only_fields = ["id"]


# `area` recebe `area_display` (o rótulo, de `get_area_display()`), e não o valor do enum: é o que
# `get_resolved` monta. `kpi_unit`/`kpi_direction` saem sempre do blueprint e nunca da variante
# (FDD 027) — `CharField` nos dois, sem choice a reusar. `hours_saved_month`/`roi_month` são texto
# pela mesma conversão de `get_custo`.
class BlueprintResolvedSerializer(serializers.Serializer):
    """Os valores do bloco do catálogo com a variante da vertical já aplicada — o que a
    instanciação vai copiar. `area` é o rótulo da área; as duas últimas são texto decimal.
    """

    name = serializers.CharField()
    area = serializers.CharField()
    description = serializers.CharField()
    kpi_label = serializers.CharField()
    kpi_unit = serializers.CharField()
    kpi_direction = serializers.CharField()
    hours_saved_month = serializers.CharField()
    roi_month = serializers.CharField()


class DigitalEmployeeBlueprintSerializer(
    AliasesDaV1Mixin, serializers.ModelSerializer[DigitalEmployeeBlueprint]
):
    """Bloco do catálogo, com as variantes aninhadas (forma do `JourneyPhaseSerializer`).

    `resolved` só aparece quando o viewset recebe `?vertical=`: são os valores já com a variante
    aplicada, que é o que a instanciação vai copiar. Sem o parâmetro o campo é omitido — quem
    lista o catálogo para editá-lo não quer ver os valores de um setor em particular.
    """

    # A área fala inglês desde a migração `0084` (issue #122, fatia 5.1; D10 do language-map).
    # Quem integrou com a `/api/v1/` mandando `"comercial"` continua funcionando: o mixin traduz
    # para `"commercial"` antes da validação. A `/api/v2/` não herda esta tradução — o valor
    # legado cai na validação de `choices` do campo `area` e leva o 400 padrão do DRF.
    VALORES_DE_ENTRADA = {
        "area": {
            "comercial": DigitalEmployeeBlueprint.Area.COMMERCIAL,
            "financeiro": DigitalEmployeeBlueprint.Area.FINANCE,
            "rh": DigitalEmployeeBlueprint.Area.HR,
            "juridico": DigitalEmployeeBlueprint.Area.LEGAL,
            "atendimento": DigitalEmployeeBlueprint.Area.SUPPORT,
        }
    }

    variants = BlueprintVariantSerializer(many=True, read_only=True)
    area_display = serializers.CharField(source="get_area_display", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True, default="")
    resolved = serializers.SerializerMethodField()
    has_variant = serializers.SerializerMethodField()

    class Meta:
        model = DigitalEmployeeBlueprint
        fields = ["id", "name", "area", "area_display", "description", "kpi_label",
                  "kpi_unit", "kpi_direction",
                  "default_hours_saved_month", "default_roi_month", "service", "service_name",
                  "active", "variants", "resolved", "has_variant"]
        read_only_fields = ["id"]

    def _vertical(self) -> Vertical | None:
        return self.context.get("vertical")

    # `None` inteiro quando o viewset não recebeu `?vertical=` — a chave sai, o objeto é que não
    # existe (quem lista o catálogo para editá-lo não pede os valores de um setor em particular).
    @extend_schema_field(BlueprintResolvedSerializer(allow_null=True))
    def get_resolved(self, blueprint: DigitalEmployeeBlueprint) -> dict[str, object] | None:
        vertical = self._vertical()
        if vertical is None:
            return None
        valores = blueprints.resolve(blueprint, vertical)
        return {
            "name": valores["name"],
            "area": valores["area_display"],
            "description": valores["description"],
            "kpi_label": valores["kpi_label"],
            "kpi_unit": valores["kpi_unit"],
            "kpi_direction": valores["kpi_direction"],
            "hours_saved_month": str(valores["hours_saved_month"]),
            "roi_month": str(valores["roi_month"]),
        }

    def get_has_variant(self, blueprint: DigitalEmployeeBlueprint) -> bool:
        return blueprints.variant_for(blueprint, self._vertical()) is not None


class DigitalEmployeeSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[DigitalEmployee]):
    """O Funcionário Digital, que agora **referencia** um KPI em vez de possuí-lo (ADR 0055).

    `kpi_baseline` e `kpi_current` continuam saindo na `/api/v1/`, agora derivados das
    `Measurement` do KPI referenciado — a baseline viva e o `Outcome` mais recente. `null`
    continua sendo "não medido", nunca zero.

    **As duas chaves são só de leitura.** Enviá-las no corpo é aceito e ignorado; para registrar
    medição, use `/measurements/`. As duas chaves morrem na `/api/v2/`, com o resto dos aliases.

    O porquê de ignorar em vez de recusar, e o que isso custa, estão na FDD 049 — e não aqui,
    porque esta docstring é a descrição do componente no `openapi.yaml` e quem a lê é quem
    consome a API, não quem mantém o repositório. Regressão do contrato em
    `tests/regression/test_a_medicao_do_ativo_sobrevive_na_v1.py`.
    """

    kpi_baseline = serializers.SerializerMethodField()
    kpi_current = serializers.SerializerMethodField()

    class Meta:
        model = DigitalEmployee
        fields = ["id", "project", "blueprint", "kpi", "name", "area", "description", "status",
                  "kpi_label", "kpi_value", "kpi_unit", "kpi_direction",
                  "kpi_baseline", "kpi_current", "hours_saved_month", "roi_month",
                  "created_at", "updated_at"]
        # `blueprint` é procedência, gravada pela rota `from-blueprint` (FDD 026). Gravável aqui,
        # abriria um segundo caminho que aponta para o template **sem copiar** — exatamente o que
        # a cópia por instância existe para impedir.
        read_only_fields = ["id", "blueprint", "created_at", "updated_at"]

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True))
    def get_kpi_baseline(self, obj: DigitalEmployee) -> str | None:
        valor = baseline_de(obj.kpi)
        return None if valor is None else str(valor)

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True))
    def get_kpi_current(self, obj: DigitalEmployee) -> str | None:
        valor = outcome_mais_recente_de(obj.kpi)
        return None if valor is None else str(valor)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """O KPI referenciado tem de ser do mesmo projeto do ativo.

        A mesma classe de vínculo cruzado que a FDD 048 valida em `PainPoint.findings`: sem isto, o
        `ProjectScopedMixin` — que olha a chave `project` — deixaria passar um `PATCH` que aponta
        para o indicador de um projeto que quem escreve não alcança.
        """
        projeto = cast(
            Project | None, attrs.get("project", getattr(self.instance, "project", None))
        )
        kpi = cast(KPI | None, attrs.get("kpi", getattr(self.instance, "kpi", None)))
        if kpi is not None and projeto is not None and kpi.project_id != projeto.pk:
            raise serializers.ValidationError(
                {"kpi": "O KPI deve pertencer ao mesmo projeto do funcionário digital."}
            )
        return attrs


class ArtifactSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Artifact]):
    ALIASES_DE_ENTRADA = {"opportunity": "commercial_opportunity"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do mixin acima.
    opportunity = serializers.PrimaryKeyRelatedField(
        source="commercial_opportunity", read_only=True
    )
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Artifact
        fields = ["id", "kind", "kind_display", "status", "status_display", "title", "content",
                  "commercial_opportunity", "opportunity", "project", "source_meeting",
                  "document", "ai_interaction",
                  "created_by", "sent_at", "decided_at", "created_at", "updated_at"]
        read_only_fields = ["id", "kind_display", "status_display", "source_meeting",
                            "ai_interaction", "created_by", "sent_at", "decided_at",
                            "created_at", "updated_at"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        # Na edição parcial só temos o que veio no corpo; o resto vem da instância.
        venda = attrs.get(
            "commercial_opportunity",
            getattr(self.instance, "commercial_opportunity", None),
        )
        project = attrs.get("project", getattr(self.instance, "project", None))
        if sum(value is not None for value in [venda, project]) != 1:
            raise serializers.ValidationError(
                "Vincule o artefato a exatamente uma oportunidade ou projeto."
            )
        return attrs

    def validate_status(self, value: str) -> str:
        if self.instance is None:
            return value
        current = self.instance.status
        if value != current and value not in ARTIFACT_TRANSITIONS[current]:
            raise serializers.ValidationError(
                f"Não é possível ir de {self.instance.get_status_display()} para "
                f"{Artifact.Status(value).label}."
            )
        return value


class CaseSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Case]):
    """O case de um projeto concluído (FDD 027).

    A lista de `read_only_fields` é a entrega, não burocracia: `metrics`, `health_snapshot` e
    `roi_snapshot` são a fotografia, e mantê-los fora da escrita é o que faz "os números não mudam
    depois de congelados" ser verdade por construção — não há caminho, em vez de haver um caminho
    que ninguém usa. O trio do consentimento fica de fora pelo motivo oposto: ele *deve* mudar, mas
    só pela ação `record-consent`, que grava quem autorizou.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    vertical_name = serializers.CharField(source="vertical.name", read_only=True, default="")
    account_name = serializers.SerializerMethodField()
    # `client_name` é o alias de leitura que morre na `/api/v2/` (issue #122, fatia 4a), e aponta
    # para o **mesmo** método da canônica: duas implementações da anonimização divergiriam, e a
    # divergência aqui vaza o nome que o cliente não autorizou.
    client_name = serializers.SerializerMethodField(method_name="get_account_name")
    project_name = serializers.CharField(source="project.name", read_only=True)
    # Alias de leitura da `/api/v1/` — morre na v2.
    client_consent = serializers.BooleanField(source="account_consent", read_only=True)

    class Meta:
        model = Case
        fields = ["id", "project", "project_name", "title", "summary", "vertical",
                  "vertical_name", "account_name", "client_name", "metrics", "health_snapshot",
                  "roi_snapshot",
                  "status", "status_display", "published_at", "account_consent", "client_consent",
                  "consent_recorded_at", "consent_recorded_by", "anonymized",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "project", "project_name", "vertical", "vertical_name",
                            "account_name", "client_name", "metrics", "health_snapshot",
                            "roi_snapshot",
                            "status_display", "published_at", "account_consent", "client_consent",
                            "consent_recorded_at", "consent_recorded_by",
                            "created_at", "updated_at"]

    def get_account_name(self, case: Case) -> str:
        """Vazio quando anonimizado — a anonimização vive aqui, e não na tela.

        Deixá-la para o frontend faria a resposta da API carregar o nome mesmo assim, e "não
        aparece" passaria a depender de todo consumidor lembrar de escondê-lo. Razão social e CNPJ
        nunca são projetados, anonimizado ou não: o case não precisa deles.
        """
        return "" if case.anonymized else case.project.engagement.account.name

    def _anonimo(self, case: Case) -> str:
        setor = case.vertical.name if case.vertical else None
        return f"Uma empresa do setor {setor}" if setor else "Uma empresa cliente"

    def to_representation(self, instance: Case) -> dict[str, Any]:
        """Apagar o nome da conta não bastava: ele também vive no **texto**.

        O congelamento monta o título como "Cliente — Projeto", então um case anonimizado saía com
        o nome no título enquanto o campo dedicado vinha vazio — a permissão que o cliente deu
        virando a que ele não deu, por uma porta que ninguém olha. A substituição alcança também o
        resumo escrito à mão, onde o mesmo nome costuma reaparecer.

        Substituir, e não esconder o título inteiro: quem revisa precisa saber de que case se
        trata, e "Uma empresa do setor Imobiliárias — Implantação de agentes" continua dizendo isso.
        """
        dados = super().to_representation(instance)
        if not instance.anonymized:
            return dados
        nome = instance.project.engagement.account.name
        rotulo = self._anonimo(instance)
        for campo in ("title", "summary"):
            if isinstance(dados.get(campo), str):
                dados[campo] = dados[campo].replace(nome, rotulo)
        return dados

    def validate_status(self, value: str) -> str:
        if self.instance is None:
            return value
        current = self.instance.status
        if value != current and value not in CASE_TRANSITIONS[current]:
            raise serializers.ValidationError(
                f"Não é possível ir de {self.instance.get_status_display()} para "
                f"{Case.Status(value).label}."
            )
        return value

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        # A guarda de consentimento é repetida aqui e no `Case.clean()` porque as duas portas são
        # de verdade: o `clean()` cobre admin do Django, shell e job; esta cobre a API e é a que
        # devolve 400 com o campo certo. `anonymized` não abre exceção — anonimizar autoriza omitir
        # a marca, não usar o resultado.
        status = attrs.get("status", getattr(self.instance, "status", Case.Status.DRAFT))
        consent = getattr(self.instance, "account_consent", False)
        if status == Case.Status.PUBLISHED and not consent:
            raise serializers.ValidationError(
                {"status": "Registre o consentimento do cliente antes de publicar o case."}
            )
        return attrs


# Os estados que **existem** no mapa de transições mas não se alcançam por digitação: cada um tem
# carimbo, autor ou chamada ao gateway por trás, e um PATCH não produz nenhum dos três.
_INVOICE_ACTION_FOR: dict[str, str] = {
    "issued": "issue",
    "paid": "mark-paid",
    "cancelled": "cancel",
}

# O que a fatura emitida não admite mais. **Não** são `read_only_fields`: em rascunho eles são o
# próprio trabalho de quem monta a cobrança.
_FROZEN_ONCE_ISSUED = (
    "account", "project", "service", "amount", "due_date", "description",
)


class InvoiceSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Invoice]):
    """A fatura (FDD 028).

    Duas travas que valem ser lidas juntas, porque a diferença entre elas é deliberada.

    **A do estado** é o mapa `INVOICE_TRANSITIONS` mais um segundo degrau que aponta a ação certa.
    O primeiro devolve o 400 de `paga → emitida`; o segundo existe porque emitir, baixar e cancelar
    são **atos com autor e carimbo** — um `PATCH status=paid` não carrega nem a data do provedor nem
    quem baixou, e aceitar isso produziria uma baixa sem procedência.

    **A dos campos** é um erro alto, e não o descarte silencioso que o `CaseSerializer` escolheu
    para a fotografia do case. Lá ninguém *queria* escrever `health_snapshot`; aqui, quem digita um
    novo `amount` numa fatura emitida **quis** — e um 200 que joga fora uma edição de dinheiro é o
    pior modo de falha disponível.
    """

    ALIASES_DE_ENTRADA = {"client": "account"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    method_display = serializers.CharField(source="get_method_display", read_only=True)
    # `account_name` é a canônica e `client_name` o alias que morre na `/api/v2/` — o mesmo par de
    # `account`/`client` acima (issue #122, fatia 4a).
    account_name = serializers.CharField(source="account.name", read_only=True)
    client_name = serializers.CharField(source="account.name", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True, default="")
    service_name = serializers.CharField(source="service.name", read_only=True, default="")
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id", "account", "client", "account_name", "client_name", "project", "project_name",
            "service",
            "service_name",
            "number", "amount", "description", "due_date", "method", "method_display",
            "status", "status_display", "is_overdue", "issued_at", "issued_by", "paid_at",
            "settled_by", "cancelled_at", "cancelled_by", "cancel_reason",
            "provider", "external_reference", "payment_url", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "account_name", "client_name", "project_name", "service_name", "number",
            "method_display",
            "status_display", "is_overdue", "issued_at", "issued_by", "paid_at", "settled_by",
            "cancelled_at", "cancelled_by", "cancel_reason",
            "provider", "external_reference", "payment_url", "created_at", "updated_at",
        ]

    def validate_status(self, value: str) -> str:
        if self.instance is None:
            return value
        current = self.instance.status
        if value == current:
            return value
        if value not in INVOICE_TRANSITIONS[current]:
            raise serializers.ValidationError(
                f"Não é possível ir de {self.instance.get_status_display()} para "
                f"{Invoice.Status(value).label}."
            )
        acao = _INVOICE_ACTION_FOR.get(value)
        if acao:
            raise serializers.ValidationError(
                f"{Invoice.Status(value).label} não é campo, é ação: "
                f"use POST /invoices/{{id}}/{acao}/."
            )
        return value

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if self.instance is not None and self.instance.status != Invoice.Status.DRAFT:
            travados = sorted(set(attrs) & set(_FROZEN_ONCE_ISSUED))
            if travados:
                raise serializers.ValidationError(
                    {c: "Fatura emitida não se edita. Cancele e emita outra." for c in travados}
                )
        return attrs


class DunningContactSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[DunningContact]):
    """O que a casa já disse sobre uma fatura (FDD 036). **Só de leitura pelo router.**

    Nenhum campo é gravável aqui, e a ausência é a entrega: um `POST /cobranca/` criaria a prova de
    um contato que não aconteceu. Contato nasce de `POST /invoices/{id}/cobranca/enviar/` ou do job
    — os dois mandam o e-mail **antes** de gravar.

    O degrau é `dunning_step` desde a `/api/v1/` da fatia 5.4 da issue #122; `degrau` e
    `degrau_display` continuam saindo aqui, com o mesmo valor, e morrem na `/api/v2/` como toda
    chave de payload legada.
    """

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)
    dunning_step_display = serializers.CharField(source="get_dunning_step_display", read_only=True)
    # O par legado do degrau (issue #122, fatia 5.4). **Sem `ALIASES_DE_ENTRADA`**: nada aqui é
    # gravável — o contato nasce da action de envio ou do job —, então não há escrita a normalizar.
    # O que a v1 promete é a leitura pelas duas chaves, e quem some com as legadas na v2 é o
    # `AliasesDaV1Mixin`, lendo `ALIASES_DEPRECIADOS["DunningContact"]`.
    degrau = serializers.CharField(source="dunning_step", read_only=True)
    degrau_display = serializers.CharField(source="get_dunning_step_display", read_only=True)
    canal_display = serializers.CharField(source="get_canal_display", read_only=True)
    # `account_name` é a canônica e `client_name` o alias que morre na `/api/v2/` — o mesmo par de
    # `account`/`client` acima (issue #122, fatia 4a).
    account_name = serializers.CharField(source="account.name", read_only=True)
    client_name = serializers.CharField(source="account.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.number", read_only=True, default="")

    class Meta:
        model = DunningContact
        fields = ["id", "invoice", "invoice_number", "account", "client", "account_name",
                  "client_name", "dunning_step", "dunning_step_display", "degrau",
                  "degrau_display", "canal", "canal_display", "sent_on", "subject", "to_email",
                  "body", "sent_by", "ai_interaction", "created_at"]
        read_only_fields = fields


class CobrancaSuspensaoSerializer(
    AliasesDaV1Mixin, serializers.ModelSerializer[CobrancaSuspensao]
):
    """Suspender a cobrança — com dono, prazo e motivo, os três obrigatórios (RFC 0004).

    A validação de "exatamente uma fatura ou um cliente" mora no `clean()` do modelo, como a do
    `Document`, e é chamada daqui: uma suspensão que valesse para os dois níveis teria duas
    leituras de "levantar", e a errada devolve a cobrança a quem ainda não devia ouvi-la.
    """

    ALIASES_DE_ENTRADA = {"client": "account"}

    # Alias de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c): a chave antiga sai com
    # o mesmo valor da canônica e morre na `/api/v2/`. A escrita vem do `AliasesDaV1Mixin`.
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)
    # `account_name` é a canônica e `client_name` o alias que morre na `/api/v2/` — o mesmo par de
    # `account`/`client` acima (issue #122, fatia 4a).
    account_name = serializers.CharField(source="account.name", read_only=True, default="")
    client_name = serializers.CharField(source="account.name", read_only=True, default="")
    invoice_number = serializers.CharField(source="invoice.number", read_only=True, default="")
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = CobrancaSuspensao
        fields = ["id", "invoice", "invoice_number", "account", "client", "account_name",
                  "client_name", "owner",
                  "until",
                  "reason", "created_by", "lifted_at", "lifted_by", "is_active",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "invoice_number", "account_name", "client_name", "created_by",
                            "lifted_at",
                            "lifted_by", "is_active", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # Cai no que já está gravado quando o campo não veio no corpo — o molde do
        # `ActivitySerializer` logo acima. Sem isto, um `PATCH` que só corrige o motivo montaria uma
        # instância sem fatura nem cliente e seria recusado por "vale para exatamente uma fatura ou
        # um cliente", que é o oposto do que a suspensão em disco diz.
        def valor(campo: str) -> Any:
            return attrs.get(campo, getattr(self.instance, campo, None))

        instancia = CobrancaSuspensao(
            **{campo: valor(campo) for campo in ("invoice", "account", "owner", "until")},
            reason=str(valor("reason") or ""),
        )
        try:
            instancia.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return attrs


class KnowledgeAreaSerializer(serializers.ModelSerializer[KnowledgeArea]):
    owner_name = serializers.CharField(source="owner.get_full_name", read_only=True, default="")
    piece_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = KnowledgeArea
        fields = ["id", "name", "slug", "position", "active", "owner", "owner_name",
                  "review_interval_days", "piece_count"]
        read_only_fields = ["id", "owner_name", "piece_count"]


class KnowledgePieceSerializer(serializers.ModelSerializer[KnowledgePiece]):
    """A peça do inventário (FDD 029).

    `last_verified_at` e `verified_by` são **read-only**: verificar é ato com autor e carimbo, pela
    ação `verify` — no molde do `record-consent` da FDD 027. Um `PATCH` que ligue a data diria "foi
    conferido" sem dizer por quem, que é a alegação de ninguém.
    """

    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    area_name = serializers.CharField(source="area.name", read_only=True, default="")
    owner_name = serializers.CharField(source="area.owner.get_full_name", read_only=True, default="")
    status = serializers.SerializerMethodField()
    next_review_at = serializers.SerializerMethodField()
    is_gap = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgePiece
        fields = ["id", "area", "area_name", "owner_name", "title", "kind", "kind_display",
                  "source_path", "summary", "last_verified_at", "verified_by",
                  "review_interval_days", "status", "next_review_at", "is_gap",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "area_name", "owner_name", "kind_display", "last_verified_at",
                            "verified_by", "status", "next_review_at", "is_gap",
                            "created_at", "updated_at"]

    def get_status(self, piece: KnowledgePiece) -> str:
        return knowledge.freshness(piece)

    def get_next_review_at(self, piece: KnowledgePiece) -> str | None:
        vence = knowledge.due_date(piece)
        return vence.isoformat() if vence else None

    def get_is_gap(self, piece: KnowledgePiece) -> bool:
        """Peça sem arquivo é **lacuna tácita** — o que só alguém sabe e ainda não está escrito."""
        return not piece.source_path


class NotificationSerializer(serializers.ModelSerializer[Notification]):
    class Meta:
        model = Notification
        fields = ["id", "kind", "message", "url", "read", "created_at"]
        read_only_fields = fields


class QualificationSerializer(serializers.ModelSerializer[Qualification]):
    """A avaliação de um lead (ADR 0049). `outcome` é obrigatório e não tem default."""

    outcome_display = serializers.CharField(source="get_outcome_display", read_only=True)
    lead_name = serializers.CharField(source="lead.name", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True, default="")

    class Meta:
        model = Qualification
        fields = [
            "id", "lead", "lead_name", "account", "account_name", "happened_at", "assessor",
            "fit", "need", "urgency", "authority", "capacity", "evidence", "outcome",
            "outcome_display", "rationale", "next_step", "nurture_until", "ai_suggested_outcome",
            "ai_score_snapshot", "legacy_opportunity", "created_at", "updated_at",
        ]
        # `ai_*` são só de leitura pela regra do mapa de linguagem §5: a IA é insumo, e um campo
        # editável que diz "a IA sugeriu" logo deixa de dizer o que a IA sugeriu.
        # `legacy_opportunity` é vínculo do backfill e não se digita.
        read_only_fields = [
            "id", "lead_name", "account_name", "outcome_display", "ai_suggested_outcome",
            "ai_score_snapshot", "legacy_opportunity", "created_at", "updated_at",
        ]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        # Espelha `Qualification.clean()` em vez de duplicar o critério: o DRF não chama o
        # `full_clean` do modelo, e sem isto o 400 de "nutrir sem data" viraria IntegrityError
        # nenhum — a linha entraria e a lista de nutrição nasceria com um registro sem retorno.
        instancia = Qualification(
            outcome=cast(str, attrs.get("outcome", getattr(self.instance, "outcome", ""))),
            nurture_until=cast(
                date | None,
                attrs.get("nurture_until", getattr(self.instance, "nurture_until", None)),
            ),
            lead=cast(Lead | None, attrs.get("lead", getattr(self.instance, "lead", None))),
            account=cast(
                Account | None, attrs.get("account", getattr(self.instance, "account", None))
            ),
        )
        try:
            instancia.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs


class LeadConvertSerializer(serializers.Serializer):
    """Corpo do `POST /leads/{id}/convert/` — a avaliação que a conversão registra.

    Tudo é opcional, e `outcome` cai em `qualified` quando ausente: converter um lead sempre
    significou "isto é real", e o botão que já existe na tela não manda corpo nenhum. Mudar o
    default aqui trocaria uma decisão de produto por um efeito colateral de refatoração.
    """

    outcome = serializers.ChoiceField(
        choices=Qualification.Outcome.choices, required=False,
        default=Qualification.Outcome.QUALIFIED,
    )
    fit = serializers.ChoiceField(
        choices=Qualification.Level.choices, required=False, allow_blank=True, default=""
    )
    need = serializers.ChoiceField(
        choices=Qualification.Level.choices, required=False, allow_blank=True, default=""
    )
    urgency = serializers.ChoiceField(
        choices=Qualification.Level.choices, required=False, allow_blank=True, default=""
    )
    authority = serializers.ChoiceField(
        choices=Qualification.Level.choices, required=False, allow_blank=True, default=""
    )
    capacity = serializers.ChoiceField(
        choices=Qualification.Level.choices, required=False, allow_blank=True, default=""
    )
    evidence = serializers.CharField(required=False, allow_blank=True, default="")
    rationale = serializers.CharField(required=False, allow_blank=True, default="")
    next_step = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    nurture_until = serializers.DateField(required=False, allow_null=True, default=None)
    # A conta existente que a avaliação deve usar, em vez de criar outra. A queryset já exclui a
    # arquivada: reabrir uma conta que alguém tirou da lista não é efeito de converter um lead.
    account_id = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.filter(archived_at__isnull=True),
        source="account", required=False, allow_null=True, default=None,
    )


class OpenCommercialOpportunitySerializer(serializers.Serializer):
    """Corpo do `POST /qualifications/{id}/open-opportunity/` — o único caminho lead→venda.

    Todos os campos são opcionais porque a ação existe para ser o passo seguinte de uma avaliação
    já registrada: o que ela precisa saber (conta, origem, dono) vem da qualificação e da sessão.
    O corpo serve para quem já sabe o que está vendendo.
    """

    title = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    scope = serializers.CharField(required=False, allow_blank=True, default="")
    estimated_value = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.filter(archived_at__isnull=True),
        required=False, allow_null=True, default=None,
    )
    expected_close_date = serializers.DateField(required=False, allow_null=True, default=None)
    contact = serializers.PrimaryKeyRelatedField(
        queryset=Contact.objects.filter(archived_at__isnull=True),
        required=False, allow_null=True, default=None,
    )


class LeadSerializer(AliasesDaV1Mixin, serializers.ModelSerializer[Lead]):
    # Aliases de leitura da `/api/v1/` (`docs/ontology/aliases.md` §2c); morrem na `/api/v2/`.
    # `AliasesDaV1Mixin` sem `ALIASES_DE_ENTRADA` porque aqui não há escrita: os dois vínculos são
    # lavrados por `POST /leads/{id}/convert/` e `POST /qualifications/{id}/open-opportunity/`, e
    # as quatro chaves são só de leitura. O mixin entra pela metade que falta — tirá-las da
    # resposta na `/api/v2/`, dirigido pelo mesmo `ALIASES_DEPRECIADOS` do esquema.
    opportunity = serializers.PrimaryKeyRelatedField(
        source="commercial_opportunity", read_only=True
    )
    client = serializers.PrimaryKeyRelatedField(source="account", read_only=True)
    # A avaliação mais recente do lead, para a tela saber que ele já passou pela qualificação sem
    # ter de pedir `/qualifications/?lead=`. Não substitui a coleção: o lead tem várias, e o que a
    # lista precisa é do estado atual.
    qualification = serializers.SerializerMethodField()
    qualification_outcome = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = [
            "id", "name", "email", "company", "phone", "cnpj", "message", "source", "status",
            "ai_fit", "ai_score", "ai_summary", "ai_recommended_action", "qualified_at",
            "enrichment", "account", "client", "commercial_opportunity", "opportunity",
            "qualification",
            "qualification_outcome",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "source", "ai_fit", "ai_score", "ai_summary", "ai_recommended_action",
            # `enrichment` é retrato do fornecedor, não campo de trabalho: editável pela tela, ele
            # deixaria de responder "o que a Receita diz" e passaria a responder "o que alguém
            # digitou", que é a diferença entre dado enriquecido e dado inventado.
            "qualified_at", "enrichment", "account", "client", "commercial_opportunity",
            "qualification",
            "qualification_outcome", "created_at", "updated_at",
        ]

    def _ultima(self, lead: Lead) -> Qualification | None:
        # Em Python e não com `.first()`: o viewset faz `prefetch_related("qualifications")`, e uma
        # consulta por linha aqui devolveria o N+1 pela porta dos fundos, na tela que lista leads.
        vivas = [q for q in lead.qualifications.all() if q.archived_at is None]
        return vivas[0] if vivas else None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_qualification(self, lead: Lead) -> int | None:
        ultima = self._ultima(lead)
        return ultima.pk if ultima else None

    @extend_schema_field(serializers.CharField())
    def get_qualification_outcome(self, lead: Lead) -> str:
        ultima = self._ultima(lead)
        return ultima.outcome if ultima else ""


class LeadIntakeSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    email = serializers.EmailField()
    company = serializers.CharField(max_length=255, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    # Opcional, e tem de continuar opcional: é a chave do enriquecimento (FDD 030), mas exigi-lo
    # colocaria um campo a mais entre o visitante e o formulário enviado — trocar volume de lead
    # por qualidade de cadastro é o negócio errado para quem depende de demanda de topo.
    cnpj = serializers.CharField(max_length=18, required=False, allow_blank=True)
    message = serializers.CharField(required=False, allow_blank=True)
    # Respostas de perguntas de triagem do formulário (rótulo → resposta), usadas na qualificação.
    answers = serializers.DictField(child=serializers.CharField(allow_blank=True), required=False)
    website = serializers.CharField(required=False, allow_blank=True)  # honeypot anti-spam


class BookingCreateSerializer(serializers.Serializer):
    token = serializers.CharField()
    slot_start = serializers.DateTimeField()


class TaskSyncSerializer(serializers.Serializer):
    """Entrada do webhook de sincronia (Linear/GitHub → Biahflow)."""

    source = serializers.ChoiceField(choices=["linear", "github"])
    external_id = serializers.CharField(max_length=128)
    external_status = serializers.CharField(max_length=64)


class LinkExternalSerializer(serializers.Serializer):
    """Vincula uma Task a uma issue existente no fornecedor."""

    source = serializers.ChoiceField(choices=["linear", "github"])
    external_id = serializers.CharField(max_length=128)


class InvitationSerializer(serializers.ModelSerializer[Invitation]):
    class Meta:
        model = Invitation
        fields = ["id", "email", "role", "expires_at", "accepted_at", "created_at"]
        read_only_fields = ["id", "expires_at", "accepted_at", "created_at"]


class AcceptInvitationSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_username(self, value: str) -> str:
        # Sem isso o `create_user` da view estourava `IntegrityError` — 500 em vez de 400.
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("Este nome de usuário já está em uso.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # A validação é de objeto, não de campo, porque o `UserAttributeSimilarityValidator`
        # desiste sem um `user` — passar só a senha o tornaria decorativo. O usuário aqui é
        # instanciado e não salvo, só para o validador ter o que comparar. O e-mail fica de fora:
        # ele vem do `Invitation`, que só é resolvido na view.
        candidate = User(
            username=attrs["username"],
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
        )
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True)
