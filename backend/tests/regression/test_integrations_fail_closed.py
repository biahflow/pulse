"""Regressão: os defeitos que só apareceriam contra o provedor real (FDD 024).

Todos moram atrás de `# pragma: no cover` — a chamada HTTP de verdade —, e por isso passaram pela
suíte inteira sem serem notados. Os testes abaixo exercitam a **regra**, extraída da chamada de
rede justamente para poder ser testada sem ela.

A propriedade que os une: **falhar fechado**. Um agendamento que não enxerga a agenda não pode
concluir "está tudo livre"; uma integração sem credencial não pode se dizer pronta; um convite que
não saiu não pode ficar gravado; e a queda de um fornecedor não pode virar erro nosso nem sumir
calada.

O arquivo cresce por rodada de homologação (FDD 024): calendário e credenciais vieram da auditoria
inicial, o convite da rodada 1 (e-mail), a superfície de IA da rodada 2, e o Drive/Calendário da
rodada 3 — que achou o mesmo padrão da 2: a auditoria original blindou **um** ponto por integração
e deixou os vizinhos crus.
"""

from datetime import date, datetime, timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.core import calendar_sync, flags

pytestmark = pytest.mark.django_db


# --- evento de dia inteiro: `end.date` é exclusivo ---------------------------------------------


def test_evento_de_dia_inteiro_termina_no_dia_seguinte() -> None:
    """O Google trata `end.date` como exclusivo: start == end é intervalo de comprimento zero,
    e a API recusa. O botão "Adicionar ao calendário" falhava em 100% das tentativas."""
    inicio, fim = calendar_sync.all_day_range(date(2026, 8, 6))

    assert inicio == "2026-08-06"
    assert fim == "2026-08-07"
    assert inicio != fim


def test_intervalo_de_dia_inteiro_nunca_tem_comprimento_zero() -> None:
    for dia in (date(2026, 1, 1), date(2026, 2, 28), date(2026, 12, 31)):
        inicio, fim = calendar_sync.all_day_range(dia)
        assert date.fromisoformat(fim) - date.fromisoformat(inicio) == timedelta(days=1)


# --- free/busy: falhar fechado ------------------------------------------------------------------


def _resposta(**agenda: object) -> dict:
    return {"calendars": {"agenda@x.test": agenda}}


def test_freebusy_le_os_periodos_ocupados() -> None:
    resultado = calendar_sync.parse_freebusy(
        _resposta(busy=[{"start": "2026-08-06T10:00:00+00:00", "end": "2026-08-06T11:00:00+00:00"}]),
        "agenda@x.test",
    )

    assert resultado == [
        (datetime.fromisoformat("2026-08-06T10:00:00+00:00"),
         datetime.fromisoformat("2026-08-06T11:00:00+00:00"))
    ]


def test_agenda_vazia_e_agenda_livre_de_verdade() -> None:
    """`busy: []` presente é resposta legítima — o dia está livre mesmo."""
    assert calendar_sync.parse_freebusy(_resposta(busy=[]), "agenda@x.test") == []


def test_calendario_inacessivel_nao_vira_agenda_livre() -> None:
    """O defeito. O Google devolve **200** com `errors` no lugar de `busy` quando a conta de
    serviço não enxerga o calendário; ler isso como `[]` significa "tudo livre", e o site passa a
    marcar reunião por cima da agenda real. Falhar aberto é a pior direção possível.
    """
    with pytest.raises(calendar_sync.CalendarUnavailable, match="notFound"):
        calendar_sync.parse_freebusy(
            _resposta(errors=[{"domain": "global", "reason": "notFound"}]), "agenda@x.test"
        )


def test_resposta_sem_busy_nao_vira_agenda_livre() -> None:
    with pytest.raises(calendar_sync.CalendarUnavailable):
        calendar_sync.parse_freebusy(_resposta(), "agenda@x.test")


def test_resposta_sem_o_calendario_pedido_nao_vira_agenda_livre() -> None:
    with pytest.raises(calendar_sync.CalendarUnavailable):
        calendar_sync.parse_freebusy({"calendars": {}}, "agenda@x.test")


# --- `configured()` cobra o que o código dereferencia -------------------------------------------


@override_settings(GOOGLE_DRIVE_ROOT_FOLDER_ID="pasta", GOOGLE_AUTH_MODE="adc")
def test_no_modo_adc_nao_ha_variavel_de_credencial_a_cobrar() -> None:
    """E isso é correto, não descuido (ADR 0016).

    Num pod com Workload Identity a credencial vem do **metadata server**: não existe variável de
    ambiente para conferir. Cobrar uma recusaria justamente a instalação mais segura das duas.
    Quem responde "isto funciona?" aqui é a sonda do `check_integrations`, que pergunta ao provedor
    em vez de ao ambiente — a tese da FDD 024 aplicada ao próprio mecanismo de auth.
    """
    assert flags.configured("drive") is True
    assert flags.missing("drive") == []


@override_settings(GOOGLE_DRIVE_ROOT_FOLDER_ID="", GOOGLE_AUTH_MODE="adc")
def test_o_recurso_em_si_continua_sendo_cobrado() -> None:
    """O que não depende do modo de auth segue exigido: sem a pasta raiz não há onde gravar."""
    assert flags.configured("drive") is False
    assert "GOOGLE_DRIVE_ROOT_FOLDER_ID" in flags.missing("drive")


@override_settings(GOOGLE_DRIVE_ROOT_FOLDER_ID="pasta", GOOGLE_AUTH_MODE="oauth",
                   GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="",
                   GOOGLE_OAUTH_REFRESH_TOKEN="")
def test_no_modo_oauth_o_trio_e_obrigatorio() -> None:
    """Aqui há o que cobrar, e a promessa do `docs/operacao.md` vale: o toggle não liga uma
    integração cujas credenciais faltem."""
    assert flags.configured("drive") is False
    faltando = flags.missing("drive")
    assert "GOOGLE_OAUTH_CLIENT_ID" in faltando
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in faltando
    assert "GOOGLE_OAUTH_REFRESH_TOKEN" in faltando


@override_settings(GOOGLE_CALENDAR_ID="agenda", GOOGLE_AUTH_MODE="oauth",
                   GOOGLE_OAUTH_CLIENT_ID="id", GOOGLE_OAUTH_CLIENT_SECRET="segredo",
                   GOOGLE_OAUTH_REFRESH_TOKEN="refresh")
def test_oauth_completo_libera_o_calendario() -> None:
    assert flags.configured("calendar") is True


@override_settings(GOOGLE_AUTH_MODE="chave-de-conta-de-servico")
def test_modo_de_auth_invalido_e_recusado_com_nome() -> None:
    """A chave de conta de serviço deixou de ser um caminho: a política da organização a proíbe
    (`iam.managed.disableServiceAccountKeyCreation`), e manter um caminho que ninguém pode tomar é
    a mesma mentira que a FDD 024 existe para consertar. Quem tem chave aponta
    `GOOGLE_APPLICATION_CREDENTIALS` e o ADC a lê.
    """
    from apps.core import google_auth

    with pytest.raises(google_auth.GoogleAuthError, match="GOOGLE_AUTH_MODE"):
        google_auth.credentials(["https://www.googleapis.com/auth/drive"])


@override_settings(ESIGN_PROVIDER="autentique", ESIGN_API_TOKEN="", ESIGN_WEBHOOK_SECRET="")
def test_esign_nao_se_diz_pronto_so_com_o_nome_do_provedor() -> None:
    assert flags.configured("esign") is False
    assert "ESIGN_API_TOKEN" in flags.missing("esign")
    # Sem o segredo do webhook o status do fornecedor leva 401 e a assinatura nunca fecha sozinha.
    assert "ESIGN_WEBHOOK_SECRET" in flags.missing("esign")


@override_settings(TASKSYNC_TOKEN="entrada", LINEAR_API_KEY="", GITHUB_TOKEN="")
def test_tasksync_nao_se_diz_pronto_so_com_o_segredo_de_entrada() -> None:
    """`TASKSYNC_TOKEN` autentica quem entra; sem credencial de fornecedor a saída fica muda."""
    assert flags.configured("tasksync") is False


@override_settings(TASKSYNC_TOKEN="entrada", LINEAR_API_KEY="", GITHUB_TOKEN="ghp_x")
def test_tasksync_aceita_um_fornecedor_so() -> None:
    assert flags.configured("tasksync") is True


# --- o número não pode mentir -------------------------------------------------------------------


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, AI_ENABLED=False)
def test_digest_nao_conta_envio_que_o_smtp_recusou(monkeypatch: pytest.MonkeyPatch) -> None:
    """O laço somava 1 por usuário independentemente do resultado, e `fail_silently=True` engole a
    falha — então o scheduler logava "Digests enviados: 12" com zero entregues. É o defeito do
    agendador que não existia, uma camada abaixo: o número dizia que estava tudo bem.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.core import digest
    from apps.core.models import Task
    from apps.core.tests.factories import ProjectFactory, UserFactory

    dono = UserFactory(email="dono@x.test")
    projeto = ProjectFactory(owner=dono)
    Task.objects.create(
        project=projeto, title="Atrasada", owner=dono,
        due_date=timezone.localdate() - timedelta(days=2),
    )

    # SMTP recusando: `send_mail` devolve 0 em vez de levantar.
    monkeypatch.setattr(digest, "send_mail", lambda *a, **k: 0)

    assert digest.send_daily_digest() == 0


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, AI_ENABLED=False)
def test_digest_conta_o_que_saiu_de_verdade(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.core import digest
    from apps.core.models import Task
    from apps.core.tests.factories import ProjectFactory, UserFactory

    dono = UserFactory(email="dono@x.test")
    projeto = ProjectFactory(owner=dono)
    Task.objects.create(
        project=projeto, title="Atrasada", owner=dono,
        due_date=timezone.localdate() - timedelta(days=2),
    )
    monkeypatch.setattr(digest, "send_mail", lambda *a, **k: 1)

    assert digest.send_daily_digest() == 1


# --- o fornecedor não derruba o pedido ----------------------------------------------------------


@override_settings(AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_openai_fora_do_ar_nao_derruba_o_formulario_publico(monkeypatch: pytest.MonkeyPatch) -> None:
    """`qualify_lead` roda dentro do POST público de intake. Sem guarda, um 429 ou um timeout
    da OpenAI viravam 500 para o visitante — e o `Lead` já estava gravado, então ele via erro
    para um cadastro que funcionou.
    """
    from apps.core import ai, qualification
    from apps.core.models import Lead

    def explode(*a: object, **k: object) -> tuple[str, dict]:
        raise RuntimeError("429 Rate limit reached")

    monkeypatch.setattr(ai, "complete", explode)
    lead = Lead.objects.create(name="Fulano", email="fulano@x.test")

    # Não levanta, e devolve o mesmo que devolveria com a IA desligada: triagem manual.
    assert qualification.qualify_lead(lead) is False


# --- convite: o e-mail é o convite ---------------------------------------------------------------


@override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                   EMAIL_HOST="127.0.0.1", EMAIL_PORT=1)
def test_convite_nao_fica_orfao_quando_o_smtp_recusa() -> None:
    """Observado na homologação da FDD 024, com o SMTP apontado para uma porta morta.

    O convite **é** o e-mail: quem recebe não tem outro caminho para o token. Antes, a linha era
    gravada e o `fail_silently=False` devolvia 500 — sobrava um convite válido que ninguém recebeu,
    o admin achava que falhara, e cada tentativa criava mais um. Agora grava e envia na mesma
    transação: ou os dois acontecem, ou nenhum.
    """
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core.models import Invitation, User
    from apps.core.tests.factories import UserFactory

    admin = UserFactory(role=User.Role.ADMIN)
    client = APIClient()
    client.force_authenticate(admin)
    antes = Invitation.objects.count()

    resposta = client.post(
        reverse("invitation"), {"email": "ninguem@exemplo.test", "role": "delivery"}, format="json"
    )

    assert resposta.status_code == 502
    assert Invitation.objects.count() == antes
    assert not Invitation.objects.filter(email="ninguem@exemplo.test").exists()


# --- a OpenAI cai: 502 no pedido, degradação no job (rodada 2) ----------------------------------
#
# A FDD 024 blindou **um** dos quatro pontos que chamam `ai.complete()` — o `qualify_lead`, por
# rodar dentro do POST público. Os outros três ficaram crus, e falham do mesmo jeito: para usuário
# autenticado e para o agendador. A regra que os une é a que o `qualify_lead` já seguia: **falha de
# IA degrada para o comportamento de IA desligada**; o que não pode é virar 500 ou sumir calada.


def _explode(*a: object, **k: object) -> tuple[str, dict]:
    from apps.core.ai import AiProviderError

    raise AiProviderError("429 Rate limit reached")


def test_complete_traduz_qualquer_falha_do_sdk_em_erro_de_fornecedor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rede fica em `_client()`; a **tradução** é regra, e regra se testa sem rede.

    É o que permite que os três pontos abaixo tratem falha de fornecedor com um tipo estreito, em
    vez de `except Exception` em volta de todo o trabalho — que reportaria um erro de banco como
    "a IA está fora do ar", a mentira que esta FDD existe para evitar.
    """
    from apps.core import ai

    def cliente_quebrado() -> object:
        raise RuntimeError("Connection error")

    monkeypatch.setattr(ai, "_client", cliente_quebrado)

    with pytest.raises(ai.AiProviderError, match="Connection error"):
        ai.complete("sistema", "usuário")


def test_complete_devolve_texto_e_uso_quando_o_modelo_responde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O caminho feliz também sai do `# pragma: no cover` — senão a tradução acima seria a única
    coisa coberta e a leitura da resposta seguiria sem rede de segurança."""
    from types import SimpleNamespace

    from apps.core import ai

    resposta = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Resumo"))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )
    monkeypatch.setattr(ai, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: resposta))
    ))

    texto, uso = ai.complete("sistema", "usuário")

    assert texto == "Resumo"
    assert uso == {"prompt_tokens": 11, "completion_tokens": 7}


@override_settings(AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_falha_da_openai_vira_502_e_nao_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_ai_run` é o helper de **nove** actions e chamava `ai.complete` sem guarda.

    429, timeout, chave revogada ou modelo sem acesso na conta viravam 500 do Django: erro nosso
    para um problema que não é nosso, e sem dizer que vale repetir. 502 é o que a FDD 024 já tinha
    decidido para o upload no Drive — "diz de quem é o problema e que vale repetir".
    """
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import ai
    from apps.core.models import AiInteraction, User
    from apps.core.tests.factories import ProjectFactory, UserFactory

    monkeypatch.setattr(ai, "complete", _explode)
    admin = UserFactory(role=User.Role.ADMIN)
    projeto = ProjectFactory(owner=admin)
    client = APIClient()
    client.force_authenticate(admin)

    resposta = client.post(reverse("project-summary", args=[projeto.id]), {}, format="json")

    assert resposta.status_code == 502
    assert "IA" in resposta.data["detail"]
    # Auditoria só existe quando houve consumo: chamada que não completou não gera `AiInteraction`,
    # senão a falha do fornecedor comeria a cota diária de quem tentou.
    assert not AiInteraction.objects.exists()


@override_settings(AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_falha_da_openai_no_ai_score_vira_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """`score_meeting` é chamado **fora** do `_ai_run` (persiste no projeto em vez de só devolver
    texto), então a guarda do `_ai_run` não o alcança — precisa da sua."""
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import ai
    from apps.core.models import User
    from apps.core.tests.factories import MeetingFactory, UserFactory

    monkeypatch.setattr(ai, "complete", _explode)
    reuniao = MeetingFactory()
    client = APIClient()
    client.force_authenticate(UserFactory(role=User.Role.ADMIN))

    resposta = client.post(reverse("meeting-ai-score", args=[reuniao.id]), {}, format="json")

    assert resposta.status_code == 502
    # O projeto não fica com um AI Score pela metade.
    reuniao.project.refresh_from_db()
    assert reuniao.project.ai_scored_at is None


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_openai_fora_do_ar_nao_para_o_digest_no_meio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chamada crua ficava **dentro do laço** `for user in ...`: levantava no primeiro usuário e
    matava o resto da fila.

    O agendador não cai — ele isola o job de propósito (`scheduler.py`) —, e é isso que torna a
    falha ruim: vira uma linha de log, o carimbo do dia é gravado assim mesmo e **não há
    retentativa até amanhã**. Todo dia às 07:30, desde a FDD 023.

    A degradação certa já estava escrita no arquivo: o ramo "IA desligada" manda o contexto como
    texto simples. Digest sem redação é muito melhor que digest nenhum.
    """
    from django.utils import timezone

    from apps.core import ai, digest
    from apps.core.models import Task
    from apps.core.tests.factories import ProjectFactory, UserFactory

    monkeypatch.setattr(ai, "complete", _explode)
    for indice in range(3):
        dono = UserFactory(email=f"dono{indice}@exemplo.test")
        projeto = ProjectFactory(owner=dono)
        Task.objects.create(
            project=projeto, title=f"Atrasada {indice}", owner=dono,
            due_date=timezone.localdate() - timedelta(days=2),
        )
    monkeypatch.setattr(digest, "send_mail", lambda *a, **k: 1)

    # Os três recebem, e a contagem diz a verdade — antes, morria no primeiro.
    assert digest.send_daily_digest() == 3


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_digest_degradado_leva_o_conteudo_e_nao_uma_casca(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entregar mesmo é o ponto: o e-mail precisa trazer os itens atrasados, não um aviso de erro.
    É o mesmo texto que sairia com a flag de IA desligada."""
    from django.core import mail
    from django.utils import timezone

    from apps.core import ai, digest
    from apps.core.models import Task
    from apps.core.tests.factories import ProjectFactory, UserFactory

    monkeypatch.setattr(ai, "complete", _explode)
    dono = UserFactory(email="dono@exemplo.test")
    projeto = ProjectFactory(owner=dono)
    Task.objects.create(
        project=projeto, title="Migração do faturamento", owner=dono,
        due_date=timezone.localdate() - timedelta(days=2),
    )

    assert digest.send_daily_digest() == 1
    assert "Migração do faturamento" in mail.outbox[0].body


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_digest_degradado_nao_audita_interacao(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chamada que não completou não gera `AiInteraction`: uma linha com zero token sujaria as
    métricas de IA e — antes da correção da cota — comeria a franquia de quem nem pediu."""
    from django.utils import timezone

    from apps.core import ai, digest
    from apps.core.models import AiInteraction, Task
    from apps.core.tests.factories import ProjectFactory, UserFactory

    monkeypatch.setattr(ai, "complete", _explode)
    dono = UserFactory(email="dono@exemplo.test")
    projeto = ProjectFactory(owner=dono)
    Task.objects.create(
        project=projeto, title="Atrasada", owner=dono,
        due_date=timezone.localdate() - timedelta(days=2),
    )
    monkeypatch.setattr(digest, "send_mail", lambda *a, **k: 1)

    assert digest.send_daily_digest() == 1
    assert not AiInteraction.objects.filter(feature="daily_digest").exists()


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, AI_ENABLED=True, OPENAI_API_KEY="sk-x",
                   AI_DAILY_LIMIT=1)
def test_digest_nao_consome_a_cota_diaria_da_pessoa(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cota existe para limitar o que uma **pessoa** gasta.

    O digest é o único ponto de IA que roda sozinho, uma vez por usuário ativo com itens, todo dia
    — e ele auditava com `user=user`, então tirava 1 das 50 chamadas de cada um por um e-mail que
    ninguém pediu. Pior: nem consultava o limite, então era isento dele e cobrava dele ao mesmo
    tempo, nos dois sentidos errados.
    """
    from django.utils import timezone

    from apps.core import ai, digest
    from apps.core.models import Task
    from apps.core.tests.factories import ProjectFactory, UserFactory

    monkeypatch.setattr(ai, "complete", lambda s, u: ("Resumo", {"prompt_tokens": 1, "completion_tokens": 1}))
    dono = UserFactory(email="dono@exemplo.test")
    projeto = ProjectFactory(owner=dono)
    Task.objects.create(
        project=projeto, title="Atrasada", owner=dono,
        due_date=timezone.localdate() - timedelta(days=2),
    )
    monkeypatch.setattr(digest, "send_mail", lambda *a, **k: 1)

    assert digest.send_daily_digest() == 1
    # A pessoa ainda tem a sua chamada do dia: o digest não gastou por ela.
    assert ai.within_daily_limit(dono) is True


def test_cliente_da_openai_nao_retenta_por_baixo_do_teto(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_TIMEOUT_SECONDS` tem de ser o teto que a variável promete.

    Medido na rodada 2 contra a OpenAI real: com o teto em 1 s, a resposta levou **5,5 s** — o SDK
    tenta 3 vezes por padrão, então o teto real era `timeout × 3` mais backoff. Com o default de
    30 s isso segura um worker do gunicorn por mais de um minuto e meio, e uma dessas chamadas
    roda dentro do POST público do formulário de leads.
    """
    from apps.core import ai

    capturado: dict[str, object] = {}

    class OpenAIFalso:
        def __init__(self, **kwargs: object) -> None:
            capturado.update(kwargs)

    monkeypatch.setitem(__import__("sys").modules, "openai",
                        type("m", (), {"OpenAI": OpenAIFalso})())

    with override_settings(OPENAI_API_KEY="sk-x", AI_TIMEOUT_SECONDS=30, AI_BASE_URL=""):
        ai._client()

    assert capturado["max_retries"] == 0
    assert capturado["timeout"] == 30


@override_settings(AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_contexto_do_projeto_ancora_a_data_de_hoje() -> None:
    """Sem "hoje", o modelo recebe uma lista de prazos e não tem como saber qual já venceu.

    Observado na rodada 2: perguntado sobre o maior risco de um projeto com tarefa vencida há três
    dias, o assistente respondeu "Não sei." em três tokens — e estava certo, faltava a âncora.
    """
    from django.utils import timezone

    from apps.core import ai
    from apps.core.tests.factories import ProjectFactory

    contexto = ai.build_project_context(ProjectFactory())

    assert f"Hoje é {timezone.localdate()}" in contexto


@override_settings(AI_ENABLED=True, OPENAI_API_KEY="sk-x")
def test_desligada_limite_e_fornecedor_sao_tres_respostas_diferentes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503, 429 e 502 dizem coisas diferentes a quem opera, e precisam continuar dizendo.

    Colapsar a falha do fornecedor em 503 faria "um admin desligou o recurso" e "a OpenAI caiu"
    chegarem iguais na tela; mapeá-la para 429 (que é o que a OpenAI devolve num rate limit) diria
    à pessoa que a **cota dela** acabou, quando não acabou.
    """
    from django.test import override_settings as ovr
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import ai
    from apps.core.models import User
    from apps.core.tests.factories import ProjectFactory, UserFactory

    admin = UserFactory(role=User.Role.ADMIN)
    projeto = ProjectFactory(owner=admin)
    client = APIClient()
    client.force_authenticate(admin)
    url = reverse("project-summary", args=[projeto.id])

    monkeypatch.setattr(ai, "complete", _explode)
    assert client.post(url, {}, format="json").status_code == 502

    with ovr(AI_DAILY_LIMIT=0):
        assert client.post(url, {}, format="json").status_code == 429

    with ovr(AI_ENABLED=False):
        assert client.post(url, {}, format="json").status_code == 503


# --- Google: o fornecedor recusa (rodada 3) -----------------------------------------------------
#
# Mesmo padrão que a rodada 2 achou na IA: a FDD 024 blindou o **upload** no Drive e deixou crus o
# download, o evento de calendário, a sincronia manual e — o mais grave — a criação do evento da
# reserva pública. Os tipos são estreitos (`DriveProviderError`, `CalendarProviderError`) para que
# um erro de banco depois da chamada não seja reportado como "o Google caiu".


def _recusa_do_google(*a: object, **k: object):
    from apps.core.calendar_sync import CalendarProviderError

    raise CalendarProviderError("forbiddenForServiceAccounts")


def _recusa_do_drive(*a: object, **k: object):
    from apps.core.drive import DriveProviderError

    raise DriveProviderError("404 File not found")


@override_settings(CALENDAR_ENABLED=True, GOOGLE_CALENDAR_ID="cal")
def test_recusa_do_google_nao_deixa_reserva_orfa(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reserva sobrevive à recusa do Google, e o resto do fluxo **também acontece**.

    `book()` fecha a transação com a `Booking` já gravada e só então cria o evento. Com o Google
    recusando, sobrava uma reserva que **bloqueia o horário** para todo mundo, sem evento na
    agenda, sem aviso ao dono e sem confirmação ao lead — e o visitante levava 500 no endpoint
    público. É a "reserva órfã", uma integração adiante do convite órfão da rodada 1.

    E é o modo de falha mais provável de todos: a FDD 024 já registrava que conta de serviço não
    convida participante sem delegação em todo o domínio (`forbiddenForServiceAccounts`).

    A correção é **degradar, não desfazer** — o próprio código já tolerava o evento não vir
    (`if event_id or link:`); o defeito era a exceção não ser tolerada como o retorno vazio era.
    """
    from django.core import mail

    from apps.core import booking, calendar_sync
    from apps.core.models import Booking, Lead, Notification
    from apps.core.tests.factories import UserFactory

    monkeypatch.setattr(calendar_sync, "freebusy", lambda a, b: [])
    monkeypatch.setattr(calendar_sync, "create_timed_event", _recusa_do_google)
    dono = UserFactory()
    lead = Lead.objects.create(name="Visitante", email="visitante@exemplo.test")
    quando = timezone.localtime() + timedelta(days=3)

    reserva = booking.book(lead, quando.replace(hour=9, minute=0, second=0, microsecond=0))

    # A reserva vale — o horário está de fato comprometido.
    assert Booking.objects.filter(status=Booking.Status.SCHEDULED).count() == 1
    assert reserva.calendar_event_id == ""
    # E o resto do fluxo aconteceu: sem isto, a degradação seria invisível para quem precisa agir.
    assert Notification.objects.filter(user=dono, kind="booking").exists()
    assert any(lead.email in m.to for m in mail.outbox)


@override_settings(CALENDAR_ENABLED=True, GOOGLE_CALENDAR_ID="cal")
def test_dono_fica_sabendo_que_o_evento_nao_entrou_na_agenda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degradação silenciosa é meio defeito: quem responde pela reunião precisa saber que ela não
    está na agenda, senão conta com um convite que não existe."""
    from apps.core import booking, calendar_sync
    from apps.core.models import Lead, Notification
    from apps.core.tests.factories import UserFactory

    monkeypatch.setattr(calendar_sync, "freebusy", lambda a, b: [])
    monkeypatch.setattr(calendar_sync, "create_timed_event", _recusa_do_google)
    dono = UserFactory()
    lead = Lead.objects.create(name="Visitante", email="visitante@exemplo.test")
    quando = timezone.localtime() + timedelta(days=3)

    booking.book(lead, quando.replace(hour=9, minute=0, second=0, microsecond=0))

    aviso = Notification.objects.get(user=dono, kind="booking")
    assert "agenda" in aviso.message.lower()


@override_settings(GOOGLE_DRIVE_ENABLED=True, GOOGLE_DRIVE_ROOT_FOLDER_ID="raiz")
def test_download_do_drive_fora_do_ar_vira_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FDD 024 consertou o **upload** (502) e deixou o download cru — o caminho em que a pessoa
    tenta pegar de volta o próprio arquivo, e levava 500."""
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import drive
    from apps.core.models import Document, User
    from apps.core.tests.factories import AccountFactory, UserFactory

    monkeypatch.setattr(drive, "download_document", _recusa_do_drive)
    admin = UserFactory(role=User.Role.ADMIN)
    documento = Document.objects.create(
        account=AccountFactory(owner=admin), original_name="proposta.pdf",
        drive_file_id="arquivo-123", uploaded_by=admin,
    )
    client = APIClient()
    client.force_authenticate(admin)

    resposta = client.get(reverse("document-download", args=[documento.id]))

    assert resposta.status_code == 502


@override_settings(GOOGLE_DRIVE_ENABLED=True, GOOGLE_DRIVE_ROOT_FOLDER_ID="raiz")
def test_upload_ao_drive_fora_do_ar_vira_502_e_nao_grava_documento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O **download** tinha teste desde a rodada 3 e o upload, que é o caminho de escrita, não.

    A assimetria é a mesma que a varredura do Google achou entre os dois: o upload foi o primeiro
    ponto blindado desta FDD, e por ser o primeiro ninguém voltou para cobrar dele a evidência que
    os vizinhos ganharam depois. A metade que só o caminho de escrita tem é a segunda asserção —
    **nenhum `Document` gravado**: o serializer sobe ao Drive antes do `save()` justamente para não
    deixar linha apontando para arquivo que não existe, e essa ordem é a promessa que o comentário
    dele faz e que nada verificava.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import drive
    from apps.core.models import Document, User
    from apps.core.tests.factories import AccountFactory, UserFactory

    monkeypatch.setattr(drive, "upload_document", _recusa_do_drive)
    admin = UserFactory(role=User.Role.ADMIN)
    account = AccountFactory(owner=admin)
    client = APIClient()
    client.force_authenticate(admin)

    resposta = client.post(
        reverse("document-list"),
        {
            "account": account.id,
            "file": SimpleUploadedFile(
                "contrato.pdf", b"%PDF-1.7 x", content_type="application/pdf"
            ),
        },
    )

    assert resposta.status_code == 502
    assert Document.objects.count() == 0


@override_settings(CALENDAR_ENABLED=True, GOOGLE_CALENDAR_ID="cal")
def test_evento_de_calendario_recusado_vira_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """`add-to-calendar` chamava `create_event` sem guarda."""
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import calendar_sync
    from apps.core.models import Milestone, User
    from apps.core.tests.factories import ProjectFactory, UserFactory

    monkeypatch.setattr(calendar_sync, "create_event", _recusa_do_google)
    admin = UserFactory(role=User.Role.ADMIN)
    projeto = ProjectFactory(owner=admin)
    marco = Milestone.objects.create(
        project=projeto, title="Entrega", owner=admin,
        due_date=timezone.localdate() + timedelta(days=5),
    )
    client = APIClient()
    client.force_authenticate(admin)

    resposta = client.post(reverse("milestone-add-to-calendar", args=[marco.id]), {}, format="json")

    assert resposta.status_code == 502


@override_settings(CALENDAR_ENABLED=True, GOOGLE_CALENDAR_ID="cal")
def test_sincronia_manual_recusada_vira_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sincronia disparada por admin pela tela também levava 500."""
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import calendar_sync
    from apps.core.models import User
    from apps.core.tests.factories import UserFactory

    monkeypatch.setattr(calendar_sync, "sync_calendar", _recusa_do_google)
    client = APIClient()
    client.force_authenticate(UserFactory(role=User.Role.ADMIN))

    resposta = client.post(reverse("config-sync-calendar"), {}, format="json")

    assert resposta.status_code == 502


def test_kickoff_sem_pasta_no_drive_deixa_rastro(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Best-effort é a decisão certa — o kickoff não pode falhar porque o Drive caiu. Mas o
    `except Exception: pass` mudo deixava o projeto sem pasta **e ninguém sabendo**."""
    import logging

    from apps.core import drive, kickoff
    from apps.core.tests.factories import ProjectFactory

    monkeypatch.setattr(drive, "ensure_project_folder", _recusa_do_drive)
    projeto = ProjectFactory()

    with caplog.at_level(logging.ERROR):
        kickoff.finalize(projeto)  # não levanta: o kickoff segue

    assert any("drive" in r.message.lower() or "pasta" in r.message.lower() for r in caplog.records)


@override_settings(
    WHATSAPP_ENABLED=True,
    WHATSAPP_PROVIDERS="zapi",
    WHATSAPP_ZAPI_INSTANCE_ID="inst",
    WHATSAPP_ZAPI_TOKEN="tok",
)
def test_kickoff_sem_grupo_no_whatsapp_deixa_rastro_e_nao_desfaz_o_projeto(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mailoutbox: list
) -> None:
    """O gêmeo do teste do Drive, para o efeito externo que a issue #110 acrescentou.

    O grupo do cliente é o segundo efeito externo do kickoff, e vale a mesma regra: o projeto já
    existe quando `finalize` roda, e derrubá-lo porque o provedor de WhatsApp caiu trocaria um
    canal ausente por uma conversão que responde 500. O que **não** pode acontecer é o silêncio.
    """
    import logging

    from apps.core import kickoff, whatsapp
    from apps.core.models import Contact
    from apps.core.tests.factories import ProjectFactory, UserFactory

    def _recusa_do_whatsapp(*a: object, **k: object):
        raise RuntimeError("provedor fora do ar")

    monkeypatch.setattr(whatsapp, "create_group", _recusa_do_whatsapp)
    projeto = ProjectFactory(owner=UserFactory(email="dono@example.test"))
    Contact.objects.create(
        account=projeto.engagement.account, first_name="Ana", phone="5511999990001"
    )

    with caplog.at_level(logging.ERROR):
        kickoff.finalize(projeto)  # não levanta: o kickoff segue

    projeto.refresh_from_db()
    assert projeto.pk is not None
    assert projeto.whatsapp_group_id == ""
    assert any(mail for mail in mailoutbox if projeto.name in mail.subject), "o e-mail sai igual"
    assert any("whatsapp" in r.message.lower() for r in caplog.records)


# --- assinatura: a solicitação é o pedido ao fornecedor (rodada 4) ------------------------------
#
# Terceira encarnação do padrão das rodadas 1 e 3 — e a pior das três, porque aqui o silêncio é
# **por desenho**: `_http_raw` engole a falha do fornecedor e devolve `None`, e `_parse_created`
# transforma `None` num `SignatureRef` vazio, indistinguível de sucesso. O convite órfão devolvia
# 500 (o admin via que falhou); a reserva órfã avisava o dono. Esta devolvia **201 Created**.


@override_settings(ESIGN_ENABLED=True, ESIGN_PROVIDER="autentique",
                   ESIGN_API_TOKEN="tok", ESIGN_WEBHOOK_SECRET="seg")
def test_fornecedor_fora_do_ar_nao_deixa_solicitacao_orfa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Com o fornecedor recusando, o portal gravava uma `SignatureRequest` sem referência nenhuma
    e respondia **201**.

    A linha ficava no detalhe do documento como "pendente", ninguém jamais assinaria, e o webhook
    **nunca** poderia fechá-la — não há `provider_ref` para casar o evento. Pior: `remind_pending`
    passaria a mandar "consta pendente a sua assinatura" a uma pessoa de verdade, sobre um
    documento que nunca lhe foi enviado, e sem link, porque `sign_url` também está vazio.

    A solicitação de assinatura **é** o pedido ao fornecedor, como o convite é o e-mail.
    """
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import esign
    from apps.core.models import Document, SignatureRequest, User
    from apps.core.tests.factories import AccountFactory, UserFactory

    # É o que `_http_raw` devolve quando a rede, o 401 ou o timeout acontecem.
    monkeypatch.setattr(esign.AutentiqueProvider, "_post", lambda self, body, ct: None)
    monkeypatch.setattr(esign, "_document_bytes", lambda doc: b"conteudo")

    admin = UserFactory(role=User.Role.ADMIN)
    documento = Document.objects.create(
        account=AccountFactory(owner=admin), original_name="contrato.pdf", uploaded_by=admin,
    )
    client = APIClient()
    client.force_authenticate(admin)
    antes = SignatureRequest.objects.count()

    resposta = client.post(
        reverse("document-request-signature", args=[documento.id]),
        {"signer_email": "signatario@exemplo.test"}, format="json",
    )

    assert resposta.status_code == 502
    assert SignatureRequest.objects.count() == antes


@override_settings(ESIGN_ENABLED=True, ESIGN_PROVIDER="", ESIGN_API_TOKEN="", ESIGN_WEBHOOK_SECRET="")
def test_sem_provedor_a_solicitacao_local_continua_valendo() -> None:
    """O contraponto que impede a correção de virar regressão.

    Sem fornecedor configurado, o `NullProvider` "registra a intenção e não promete nada" — e o
    `mark-signed` manual é o único caminho que funciona assim (roadmap). Uma referência vazia aqui
    é **correta**, não falha, e a solicitação tem de ser criada.
    """
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core.models import Document, SignatureRequest, User
    from apps.core.tests.factories import AccountFactory, UserFactory

    admin = UserFactory(role=User.Role.ADMIN)
    documento = Document.objects.create(
        account=AccountFactory(owner=admin), original_name="contrato.pdf", uploaded_by=admin,
    )
    client = APIClient()
    client.force_authenticate(admin)

    resposta = client.post(
        reverse("document-request-signature", args=[documento.id]),
        {"signer_email": "signatario@exemplo.test"}, format="json",
    )

    assert resposta.status_code == 201
    assert SignatureRequest.objects.filter(document=documento).count() == 1


@override_settings(ESIGN_ENABLED=True, ESIGN_PROVIDER="autentique",
                   ESIGN_API_TOKEN="tok", ESIGN_WEBHOOK_SECRET="seg")
def test_documento_vazio_nao_vira_solicitacao_fantasma(monkeypatch: pytest.MonkeyPatch) -> None:
    """O outro caminho que produzia referência vazia: documento sem conteúdo. O `send` loga um
    aviso e devolve `SignatureRef()` — que era gravado como se tivesse dado certo."""
    from django.urls import reverse
    from rest_framework.test import APIClient

    from apps.core import esign
    from apps.core.models import Document, SignatureRequest, User
    from apps.core.tests.factories import AccountFactory, UserFactory

    monkeypatch.setattr(esign, "_document_bytes", lambda doc: b"")
    admin = UserFactory(role=User.Role.ADMIN)
    documento = Document.objects.create(
        account=AccountFactory(owner=admin), original_name="vazio.pdf", uploaded_by=admin,
    )
    client = APIClient()
    client.force_authenticate(admin)

    resposta = client.post(
        reverse("document-request-signature", args=[documento.id]),
        {"signer_email": "signatario@exemplo.test"}, format="json",
    )

    assert resposta.status_code == 502
    assert not SignatureRequest.objects.filter(document=documento).exists()
