# FDD 024 — Sondas de integração e falhar fechado

## Jornada

O `roadmap.md` tem 33 itens `[x]` e 1 pendente. Só que as quatro entregas anteriores foram todas
defeitos em features **já marcadas como entregues**, e duas auditorias explicaram por quê:

- **As sete flags de integração nascem `false`**, e com os defaults **cerca de metade do roadmap
  entregue está apagada** numa instalação nova. *(Corrigido depois pela ADR 0018: `email` e `esign`
  passaram a nascer ligadas, e nenhuma flag liga sem as credenciais que exige.)*
- **Todo código que fala com um provedor externo está atrás de `# pragma: no cover`** — Drive,
  Calendário, OpenAI, e-sign, Linear/GitHub. Os testes param na fronteira do mock, então nenhuma
  dessas linhas jamais executou, nem em teste nem em produção.

Só uma integração tem homologação registrada (Autentique, ADR 0007) e só um subsistema externo tem
exercício real e recorrente (backup/restauração, FDD 021).

A pergunta que este recorte responde: **a credencial existe ou a credencial funciona?**

## Regras

- **`configured()` cobra o que o código dereferencia.** Antes, cinco das sete flags podiam ser
  ligadas pela tela Configurações faltando a credencial usada na primeira chamada: Drive e
  Calendário pediam o id da pasta/agenda mas não a conta de serviço; e-sign pedia o nome do
  provedor mas não o token nem o segredo do webhook; `tasksync` pedia o segredo de **entrada** e
  nenhuma credencial de fornecedor. A tela dizia "Ligada" e o recurso estourava — enquanto o
  `docs/operacao.md` promete que "o toggle só não liga uma integração cujas credenciais faltem no
  ambiente". A promessa passou a ser verdade.
- **`requires_any` para credencial com duas formas.** A conta de serviço do Google chega como JSON
  inline **ou** como caminho de arquivo; exigir as duas recusaria instalação legítima.
- **A sonda pergunta ao provedor, e não ao ambiente.** `manage.py check_integrations` faz, por
  integração ligada, **uma chamada real, barata e só de leitura**, e sai com código 1 quando
  alguma reprova. É o irmão do `backup_status` (FDD 021) e existe pelo mesmo motivo: a aplicação
  não faz o trabalho, ela **diz se o trabalho é possível**.
- **A sonda nunca levanta e nunca cobra.** Diagnóstico que estoura vira "o diagnóstico quebrou" em
  vez de "a integração quebrou". E nenhuma sonda gera token, cria arquivo, manda e-mail ou abre
  documento — diagnóstico que cobra da conta de quem o roda não é rodado.
- **A sonda reusa o construtor de cliente do próprio adaptador** (`ai._client`, `drive._service`,
  `calendar_sync._service`). É o que a faz valer: credencial malformada estoura no mesmo código que
  estouraria em produção, e não num caminho paralelo que só se parece com ele.
- **`--all` sonda inclusive o desligado**, para conferir a credencial **antes** de ligar.
- **Sem sonda não é falha.** Onde não há como perguntar sem efeito colateral (`tasksync` sem
  credencial nesta instalação, `portal`, Clicksign), o resultado é "não sondável" e não "FALHOU" —
  alerta que grita errado treina quem opera a ignorá-lo.

### Falhar fechado

Quatro defeitos que só apareceriam contra o provedor real, corrigidos antes de qualquer credencial
ser apontada:

- **Evento de dia inteiro terminava no mesmo dia.** `end.date` é **exclusivo** no Google: start
  igual a end é intervalo de comprimento zero e a API recusa — o botão "Adicionar ao calendário"
  falhava em **100%** das tentativas. A regra saiu para `all_day_range()`, testável sem rede.
- **O free/busy falhava aberto.** Sem acesso ao calendário o Google devolve **200** com `errors` no
  lugar de `busy`; ler isso com `.get("busy", [])` produz "tudo livre", e o site passa a oferecer e
  marcar reunião por cima da agenda real. `parse_freebusy` levanta `CalendarUnavailable`, e os dois
  endpoints de agendamento devolvem 503 em vez de mentir sobre a agenda.
- **O digest contava envio que não houve.** `send_mail` devolve quantas mensagens saíram e, com
  `fail_silently=True`, isso é `0` quando o SMTP recusa ou não existe. O laço somava 1 assim mesmo,
  então o agendador logava "Digests enviados: 12" com zero entregues — o defeito do agendador
  inexistente uma camada abaixo, agora com o número dizendo que estava tudo bem.
- **Dois pontos derrubavam o pedido.** O upload no Drive era a única integração num caminho de
  **escrita** sem tratamento: credencial errada dava 500 mudo e o arquivo do usuário sumia (agora
  502, que diz de quem é o problema e que vale repetir). E `qualify_lead` chamava a OpenAI de forma
  síncrona **dentro do POST público** do formulário de leads, sem guarda e sem teto — um 429 virava
  500 para o visitante de um cadastro que na verdade funcionou, e o SDK espera 10 min por padrão,
  o que prenderia o worker. Ganhou `try/except` e `AI_TIMEOUT_SECONDS` (default 30 s).

## Configuração

| Variável | Default | O que faz |
| --- | --- | --- |
| `AI_TIMEOUT_SECONDS` | `30` | teto da chamada à OpenAI; protege o formulário público. É o teto **de verdade** desde a rodada 2: o cliente vai com `max_retries=0`, senão o SDK triplicaria o tempo por baixo |

## Critérios de aceite

- `manage.py check_integrations` com tudo desligado sai **0** e lista as sete como `desligada`.
- Integração ligada sem credencial **reprova antes de gastar rede**, nomeando a variável que falta.
- Sonda que estoura vira reprovação com a **mensagem do provedor** — é ela que diz o que consertar.
- A tela Configurações recusa ligar Drive/Calendário sem conta de serviço, e-sign sem token ou
  segredo de webhook, e `tasksync` sem credencial de fornecedor.
- Agenda inacessível **não** vira agenda livre: `/booking/slots/` e `/booking/book/` devolvem 503.

Testes em `apps/core/tests/test_integrations.py` e `tests/regression/test_integrations_fail_closed.py`.
Sabotagem deliberada, como nas entregas anteriores: repor a data final igual ao início e o
`.get("busy", [])` reprova três testes de regressão.

## Rodada 1 — e-mail, homologada em 06/08/2026

Primeira integração exercitada contra infra real. Procedimento e evidência em
`docs/runbooks/homologacao-de-integracoes.md`. O que a rodada produziu:

- **A sonda SMTP funciona** (`SMTP mailpit:1025 respondeu`) — primeira vez que uma sonda deste
  módulo roda contra algo de verdade.
- **A codificação do assunto está correta no fio**: RFC 2047 base64/utf-8, travessão e cedilha
  intactos. Era o principal risco, porque o backend de teste do Django guarda objetos em memória e
  **nunca codifica nada** — nenhum dos assuntos acentuados jamais tinha passado por MIME.
- **A correção da contagem do digest foi observada**: com o SMTP morto, `Digests enviados: 0` e um
  aviso por destinatário. Único item desta FDD que saiu de "corrigido por análise" para
  "corrigido e visto".
- **Defeito novo, achado e corrigido na rodada**: o convite ficava **órfão** quando o SMTP recusava
  — a linha era gravada e o `fail_silently=False` devolvia 500, deixando um convite válido que
  ninguém recebeu e que cada retentativa duplicava. Agora grava e envia na mesma transação, e
  devolve 502.
- **Comportamento documentado**: convite e kickoff **ignoram a flag `email`** (são transacionais).
  A FDD 010 dizia "desligada → nada muda (só in-app)", o que se lia como "nenhum e-mail sai".

## Rodada 2 — IA (OpenAI), homologada em 06/08/2026

Segunda integração exercitada contra o provedor real, e a primeira que custa dinheiro: **7 115
tokens em 15 chamadas** de `gpt-4o-mini`. Procedimento e evidência no runbook. As 12 superfícies de
IA responderam 200 e os quatro artefatos nasceram em `draft` com conteúdo (FDD 016).

**O que a sonda provou.** `models.retrieve` distingue, de graça, "a chave funciona" de "a chave
funciona mas esta conta não usa este modelo": com `AI_MODEL` inexistente e credencial boa, a sonda
**reprova** com a mensagem do provedor. É a tese desta FDD demonstrada em vez de argumentada.

**O que saiu de "corrigido por análise" para "corrigido e visto".** Os dois itens de blindagem que
faltavam: `qualify_lead` com o fornecedor fora do ar **não derruba** o POST público (lead gravado,
triagem manual), e o digest **entrega a todos** em texto estruturado em vez de morrer no primeiro.

**Antivazamento confere contra o modelo real.** A transcrição semeada omitia o orçamento de
propósito; Discovery, Assessment e o chat não o inventaram, e perguntado direto o chat recusou.
**E `_parse` aguenta**: o AI Score voltou como JSON válido de primeira, sem precisar de
`response_format`.

**Quatro defeitos novos, três corrigidos na rodada:**

- **O assistente do projeto respondia "Não sei." a pergunta que o contexto respondia** — três
  tokens de resposta, enquanto o `summary`, com o mesmo contexto, acertava. Duas causas: o contexto
  **não dizia que dia é hoje** (então "está atrasado?" era indecidível) e o texto de sistema
  proibia raciocinar ("use somente o contexto" virou "só repita o que está escrito"). Corrigidas e
  reconferidas: a resposta certa aparece, e o antivazamento não afrouxou.
- **`AI_TIMEOUT_SECONDS` não era o teto que prometia.** Com o teto em 1 s a chamada levou **5,5 s**
  — o SDK tenta 3 vezes por padrão, então o teto real era `timeout × 3` mais backoff. Com o default
  de 30 s, mais de um minuto e meio segurando um worker por causa de um formulário público. Agora
  `max_retries=0`; a retentativa mudou de dono, porque depois desta rodada todo ponto de chamada ou
  degrada ou devolve 502 dizendo que vale repetir.
- **O digest cobrava a cota de IA de quem nem pediu**, auditando com `user=user` — e sem consultar
  o limite, então era isento dele e cobrava dele ao mesmo tempo.
- **O agente de Entrega não sabe o que está atrasado** — e responde isso honestamente, porque
  `build_delivery_context` manda um resumo de resumos (`risco médio — Itens atrasados`) sem os
  itens. **Não corrigido**: é um dos agregadores recortados à mão pela ADR 0010, e ampliá-lo pede
  revisar o escopo com cuidado próprio.

## Varredura do Google — antes da rodada 3

A rodada 2 ensinou a lição: **a auditoria original desta FDD foi parcial**. Ela blindou 1 dos 4
pontos que chamavam a OpenAI. A varredura equivalente do Google, feita antes de apontar credencial,
achou o mesmo padrão — o **upload** no Drive estava protegido, e seis vizinhos não:

- **A reserva órfã** (`booking.book`), a mais grave e no caminho público: a `Booking` é gravada e a
  transação **fecha** antes de o evento ser criado. Recusa do Google deixava a reserva bloqueando o
  horário, sem evento, sem aviso ao dono, sem confirmação ao lead, e 500 para o visitante. Corrigida
  por **degradação**, que é o que o próprio código já fazia com o retorno vazio (`if event_id or
  link:`) — o defeito era a exceção não ser tolerada como o vazio era.
- **O download do Drive** (na action de documento e no `request-signature`), o `add-to-calendar` e a
  sincronia disparada pela tela: **502**, como o upload.
- **O transporte do free/busy**: o `parse_freebusy` falha fechado desde esta FDD, mas uma falha de
  rede um passo antes escapava crua. Vira **503** pela `CalendarUnavailable` que já existe — ali a
  pergunta é "o que há na agenda?", e a resposta honesta é "não sei".
- **O `kickoff.finalize`**, que engolia a falha do Drive com `pass` mudo: o projeto ficava sem pasta
  e ninguém sabendo.

Tipos estreitos (`DriveProviderError`, `CalendarProviderError`), pelo mesmo motivo da rodada 2. E
nasce `apps/core/exceptions.py`, porque a regra "falha de fornecedor é 502" já estava expressa em
três módulos e ia para quatro — *uma regra, uma expressão* (ADR 0010).

## Rodada 3, primeira tentativa — bloqueada pela política, e o que ela ensinou

Apontar a credencial do Google **não foi possível**: a organização aplica
`iam.managed.disableServiceAccountKeyCreation`, e as duas únicas variáveis que o código sabia ler
(`GOOGLE_SERVICE_ACCOUNT_INFO`/`_FILE`) são exatamente o artefato proibido.

Isso é um achado da homologação tanto quanto um defeito seria, e do tipo que só aparece quando
alguém tenta de verdade: **o desenho não era subótimo, era inconstruível** nesta organização — e o
`docs/operacao.md` seguia prescrevendo um caminho que ninguém podia tomar, que é a mesma classe de
mentira que esta FDD existe para consertar. É, quase com certeza, a razão de esta integração nunca
ter sido homologada.

A autenticação foi trocada antes de a rodada seguir (**ADR 0016**): ADC por padrão — o que cobre
Workload Identity em container/pod, sem segredo nenhum no ambiente — e OAuth de usuário como modo
explícito, para o que exige agir como uma pessoa. Com isso a rodada passa a exercitar o desenho que
vai para produção, em vez de um que seria substituído.

Detalhe que fecha o argumento desta FDD: no modo `adc` **não há variável de ambiente a conferir** —
num pod a credencial vem do metadata server. `configured()` não tem o que cobrar, e quem responde
"isto funciona?" é a **sonda**. A tese "pergunte ao provedor, não ao ambiente" passou a valer para
o próprio mecanismo de autenticação.

## Rodada 3 — Google (Drive + Calendário), homologada em 06/08/2026

Terceira integração exercitada contra o provedor real, depois de a primeira tentativa ter sido
bloqueada pela política e de a autenticação ter sido trocada (ADR 0016). Procedimento e evidência
no runbook.

**Tudo o que a varredura tinha corrigido por análise foi confirmado**, e a que mais importa é o
`end.date` exclusivo: o `add-to-calendar` respondeu **200 com link**, contra 100% de falha antes da
correção. O Drive atravessou inteiro — árvore PARA criada pelo kickoff, upload, e download **byte a
byte idêntico**. A sincronia inbound criou a tarefa e **não duplicou** ao repetir.

Com o fornecedor recusando, os quatro pontos da varredura devolveram **502** com mensagem legível,
e a reserva **sobreviveu**: sem evento, mas com o dono avisado da ressalva e sem 500 no endpoint
público.

**Dois achados:**

- **O `forbiddenForServiceAccounts` não aconteceu.** Esta FDD previa a recusa do convite ao
  participante, e o runbook dizia "já se sabe que vai falhar". Não falhou: com a credencial de
  usuário da ADR 0016, o convite foi aceito de primeira. A troca de autenticação — que nasceu de
  uma política bloqueando a chave — resolveu de carona o defeito funcional que se esperava
  contornar. A degradação do `booking.book` passou de caminho cotidiano a **rede de segurança**.
- **A tese desta FDD se provou três vezes numa rodada só, por três motivos diferentes.** A
  credencial pode estar **ausente** (o refresh token vazio, que o `configured()` nomeou); **presente
  com o valor errado** (a URL da pasta colada no lugar do id — que o `configured()` **não** pega,
  porque a variável está preenchida, e que virou correção própria); e **presente e correta com a
  integração morta mesmo assim** (a API não habilitada no projeto, `403 accessNotConfigured`). A
  terceira é inalcançável por qualquer verificação de ambiente. As três só apareceram porque alguém
  perguntou ao provedor.

## Rodada 4 — assinatura eletrônica, homologada em 06/08/2026

Última integração, e a única cuja ação principal alcança uma pessoa. Rodada com
`ESIGN_DELIVERY=link`, que faz o Autentique **não** notificar ninguém e devolve o convite para o
portal — ou seja, para o Mailpit. A API real foi exercitada de ponta a ponta sem sair da máquina.

**O laço inteiro da ADR 0007 funciona**: pedido com `provider_ref`/`document_ref`/`sign_url` reais,
convite, lembrete, webhook com HMAC fechando a assinatura **sozinho**, reentrega idempotente, e
HMAC falso recusado com 401.

**A sonda que faltava nasceu aqui.** O gancho existia desde este recorte — `_probe_esign` procura
um `ping` no adaptador —, mas nenhum fornecedor o implementava, e o e-sign era a única integração
configurada que respondia "sem sonda disponível". A query `me` do Autentique serve: valida o token,
é só leitura, não cria documento. Das sete flags, sobram sem sonda apenas as que **não têm como**
ter uma (`portal`, que é destino de webhook, e `tasksync`, sem credencial nesta instalação).
*(O `portal` deixou de ser a única flag não-alternável na ADR 0018 — segue sem sonda, mas agora
pode ser desligado pela tela.)*

**Dois achados:**

- **A solicitação fantasma.** `_http_raw` engole a falha do fornecedor e devolve `None` — de
  propósito. O degrau seguinte é que estava errado: `None` virava um `SignatureRef` vazio,
  **indistinguível de sucesso**, e a view gravava a `SignatureRequest` e respondia **201**. Ficava
  uma assinatura "pendente" que ninguém assinaria, que o webhook nunca poderia fechar (sem
  `provider_ref` não há o que casar) e sobre a qual o lembrete ainda cobraria uma pessoa de
  verdade, sem link. É a terceira encarnação do padrão das rodadas 1 e 3 e a **pior das três**: o
  convite órfão devolvia 500, a reserva órfã avisava o dono, esta respondia 201 Created.
- **A primeira versão da sonda deu 401 com um token válido**, porque usava o helper HTTP do
  **Clicksign** — que leva o token na URL e não manda header de autorização. Só apareceu por rodar
  contra o fornecedor real; contra um dublê, teria passado. É a tese desta FDD aplicada à própria
  ferramenta que ela criou.

## As quatro rodadas, em uma linha

E-mail, IA, Google e assinatura foram homologadas — **e as quatro acharam defeito**. Três delas
acharam a *mesma* classe: uma linha gravada afirmando um efeito externo que não aconteceu (convite,
reserva, solicitação de assinatura). A regra que sobrou vale para a próxima integração que entrar:
**se o registro local só faz sentido porque algo saiu daqui, ele não pode existir sem isso** — ou
falha alto, ou degrada dizendo o que perdeu.

## Fora deste recorte

**O Clicksign**, que segue sem homologação — o adaptador existe, e agora também sem `ping`. É a
única superfície desta FDD que continua corrigida **por análise, não por observação**: o teste
prova a regra, só a credencial real prova a integração.

**O contexto do agente de Entrega — resolvido.** O achado 4 da rodada 2 (ele descrevia risco em
vez de listar o que está atrasado) foi corrigido depois: `build_delivery_context` passou a nomear
os itens vencidos, com o projeto de cada um. O recorte segue saindo de `visible_to`, e o vazamento
possível — que deixou de ser o nome do projeto e passou a ser o título do item — ganhou teste de
regressão próprio.

**Teto de tokens — resolvido, e por feature.** `AI_DAILY_LIMIT` conta chamadas, não custo. A
rodada 2 mediu a saída real (média ~225 tokens, máximo 854 no contrato), e foi essa medição que
definiu o desenho: teto global seria alto demais para servir de teto, ou truncaria um contrato no
meio de uma cláusula. Então ele vale só onde a saída tem forma fixa e pequena — qualificação de
lead (300) e AI Score (500) —, e a regra saiu para `ai.completion_kwargs`, pura e testável sem
rede.

**Rodar `check_integrations` pelo agendador.** O gancho é natural (o `scheduler` já faz isso com o
`backup_status`), mas fica para depois de as sondas provarem que não dão falso positivo.

**Convite de participante em evento por conta de serviço** (`create_timed_event` com `attendees`):
uma conta de serviço não convida sem delegação em todo o domínio, e o Google responde
`forbiddenForServiceAccounts`. É configuração do Workspace, não código, e vai no runbook da rodada
do Google.

**Clicksign.** O adaptador existe e não tem homologação — o `roadmap.md` creditava a ele a
homologação que o Autentique ganhou (ADR 0007). A linha foi corrigida; homologar o Clicksign é
item próprio.

## Varredura do WhatsApp — antes da rodada

No molde da varredura do Google (acima): antes de apontar credencial contra o WhatsApp — a única
integração **em uso sem rodada de homologação** —, uma varredura de falhar-fechado percorreu
`kickoff.py` e `whatsapp.py` procurando o mesmo padrão das quatro rodadas: uma linha gravada
afirmando um efeito externo que não aconteceu.

**O que se achou.** `abrir_grupo_de_whatsapp` (`kickoff.py`) tinha exatamente um ponto cego:
`whatsapp.create_group` devolve `DELIVERED` — o grupo existe no WhatsApp, com o cliente já
dentro —, e a gravação seguinte (`project.engagement.save()`) podia falhar por indisponibilidade
do banco, lock ou timeout de conexão. A exceção subia até `finalize`, que a engolia no `except
Exception` best-effort e seguia: `convite_do_grupo` ficava `""`, o e-mail e a notificação de
kickoff saíam sem menção a grupo nenhum, e **ninguém ficava sabendo que existe um grupo com o
cliente dentro** — nem o id, nem o link. A própria mensagem do log de `finalize` piorava o quadro,
ao afirmar "grupo de WhatsApp não criado" — o contrário do que tinha acontecido.

A assimetria que prova que era defeito e não decisão: o estado `UNCERTAIN`, em que o grupo *pode*
não existir, notifica o dono do projeto (emenda da issue #117 na ADR 0062). Este caminho, em que o
grupo *certamente* existe, não notificava ninguém. É a mesma classe do defeito que a varredura do
Google achou em `booking.book` (reserva órfã), com a janela invertida — lá o registro vinha antes
do efeito externo, aqui vem depois. Corrigido: a gravação ganhou tratamento próprio dentro de
`abrir_grupo_de_whatsapp`, com log em nível `exception` carregando `group_id`/`invite_url` — a
única coisa que sobrevive se o banco está fora — e aviso best-effort ao dono dizendo que o grupo
**foi criado** (distinto do "pode haver um grupo criado" do `UNCERTAIN`). O convite continua sendo
devolvido: o grupo existe, e é o que o e-mail de kickoff precisa para sair com o link. A mensagem
do `except Exception` de `finalize` deixou de afirmar "grupo não criado", porque uma exceção que
chegue até ali, depois da correção, não sabe mais se o grupo existe ou não.

**O que se achou de limpo, e por que isso é resultado.** Diferente da IA (rodada 2) e do Google
(varredura acima), a auditoria original desta superfície — issue #110 (o chamador nasceu), #117 (a
dívida do `UNCERTAIN` sem destinatário fechada) e #119 (a referência migrou do projeto para o
`Engagement`) — **não foi parcial**. O que ela deixou de pé se confirma aqui:

- `_request`, em `whatsapp.py`, nunca levanta — classifica todo desfecho de rede nos quatro estados
  da ADR 0062 (`DELIVERED`/`UNAVAILABLE`/`REFUSED`/`UNCERTAIN`), e é por isso que o ponto cego
  achado acima estava no chamador, não no adaptador;
- a gravação em `kickoff` já era estritamente **posterior** a `DELIVERED` — o defeito nunca foi
  gravar antes de confirmar (o erro das rodadas 1, 3 e 4), foi não tratar a própria gravação como
  um passo que pode falhar;
- os cinco cenários de falha do adaptador (`DELIVERED`, `UNAVAILABLE`, `REFUSED`, `UNCERTAIN` cru e
  `UNCERTAIN` reconciliado) já tinham teste em `test_whatsapp.py`.

Registrar que a auditoria original não foi parcial é o que impede a próxima varredura de refazer o
mesmo caminho achando que ninguém olhou: o achado desta rodada é um ramo sem cobertura que as três
issues anteriores não tinham como cobrir — nenhuma delas exercitava uma falha na gravação depois de
`DELIVERED` —, não uma classe inteira de defeito deixada passar.

Ver ADR 0062, ADR 0064 e `docs/runbooks/homologacao-de-integracoes.md`, seção 7 (pendente).

## Emenda (05/09/2026) — o outro lado do upload

Uma varredura da superfície de documento saiu atrás de uma dívida que **já estava paga** — o
`PUT`/`PATCH` de `/documents/{id}/`, fechado quando os verbos saíram do `http_method_names` do
`DocumentViewSet`, com regressão em `tests/regression/test_documento_e_imutavel.py` — e achou três
outras no caminho. Duas são a mesma classe que as varreduras acima já acharam duas vezes (a
auditoria blinda um ponto e deixa o vizinho cru), só que aqui o vizinho é o **outro lado da mesma
integração**: o upload no Drive foi o primeiro ponto blindado desta FDD, e por ser o primeiro nunca
recebeu de volta o que os vizinhos ganharam depois.

- **O upload não tinha teste de 502, e o download tinha.** A rodada 3 escreveu
  `test_download_do_drive_fora_do_ar_vira_502` e não o par de escrita — justamente o caminho em que
  o arquivo do usuário some. Agora tem
  (`test_upload_ao_drive_fora_do_ar_vira_502_e_nao_grava_documento`), e com a metade que só o
  caminho de escrita exige: **nenhum `Document` gravado**. O serializer sobe ao Drive antes do
  `save()` para não deixar linha apontando para arquivo que não existe, e essa ordem era uma
  promessa escrita em comentário que nada verificava — repô-la ao contrário reprova a segunda
  asserção sem mexer na primeira, que continua devolvendo 502.
- **A captura do `create` era genérica, e a do `download` estreita.** `DocumentSerializer.create`
  tinha `except Exception` e transformava tudo em 502; a action de download já capturava
  `drive.DriveProviderError`. Num caminho de escrita, o genérico devolve como "o Drive está fora"
  também o defeito **nosso** — e essa é exatamente a mentira sobre a origem do problema que os
  tipos estreitos da varredura do Google existem para evitar. O conserto **não** foi estreitar o
  `except` e torcer: `drive.upload_document` não embrulhava nada, então estreitar sozinho trocaria
  um 502 correto por um 500. Foi o adaptador passar a traduzir a conversa com o Google para
  `DriveProviderError`, como `download_document` já fazia, e só então o chamador estreitou. Ficam
  **fora** do `try` do adaptador, de propósito, o documento sem conta-dona (defeito daqui — o
  `Document.clean()` e o `DocumentSerializer.validate` já garantem o vínculo único, e chegar ali
  com `None` merece 500) e a leitura do arquivo local, que não é conversa com o Google.
- **O farejamento do `%PDF` morava em três lugares.** O carimbo `Document.content_is_pdf` no
  upload, o aviso da tela de assinatura (`esign.lacuna_de_posicionamento`) e a leitura das âncoras
  no envio real (`esign._itens_da_ultima_pagina`) comparavam o mesmo literal por conta própria.
  Não é falhar-fechado; é *uma regra, uma expressão* (ADR 0010), a mesma razão que fez
  `apps/core/dinheiro.py` nascer — e com consequência prática, porque os dois primeiros decidem o
  que a tela promete e o terceiro decide onde a assinatura cai. A decisão virou
  `apps/core/document.py`, que recebe **bytes** e devolve `bool`: cada chamador continua lendo da
  fonte que tem (upload em memória, arquivo local, conteúdo baixado), porque centralizar a leitura
  junto da decisão acoplaria três contextos de I/O num lugar só. Não mora em `esign.py` porque o
  farejamento é sobre o documento, não sobre a assinatura — e o carimbo existe mesmo numa
  instalação que nunca ligue o e-sign.

Continua fora: o backfill de `content_is_pdf` para o legado que só existe no Drive (a linha antiga
fica sem carimbo em vez de ganhar um valor inventado, decisão da issue #120 — `null` é "não
medido", nunca `False`).
