import { ArrowLeft, Workflow } from "lucide-react";
import type { KeyboardEvent } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  getDiscoverySession,
  listDiscoveryQuestions,
  listProcessObservations,
  saveDiscoverySessionBlock,
  structureDiscoverySession,
} from "../api";
import { mensagemDeFalha } from "../erros";
import type {
  Discovery,
  DiscoveryQuestionBlock,
  DiscoverySession,
  DiscoverySessionNotes,
  ProcessObservation,
} from "../types";

/**
 * A **Discovery Session** — `/projetos/:id/sessoes/:sessionId`.
 *
 * Governada pelo DAP `docs/design/dap-discovery-session-e-business-case-r2/`, revisão 2, decisões
 * **C1 · D2 · E1 · G1 · H3**. É a primeira tela do produto usada *durante* uma reunião de duas
 * horas, com uma pessoa digitando enquanto outra fala, e essa diferença carrega tudo o que é
 * incomum aqui.
 *
 * **A tela captura texto e nunca grava `Finding`** (C1). Estruturar em `Process`/`Evidence`/
 * `Finding` é o ato explícito que já existia, disparado depois da sessão, e passa pelo mesmo
 * coletor da extração por reunião — é ele que impõe `hypothesis` por constante. Uma segunda porta
 * de gravação recriaria exatamente o defeito que aquela decisão fechou.
 *
 * **As perguntas vêm do backend** (E1), espelho da ficha *Discovery Questions* do Notion. Nenhuma
 * está escrita neste arquivo, e a chave de cada resposta é o **id** da pergunta — nunca a posição
 * dela na lista.
 *
 * **O autosave é o primeiro mecanismo de escrita periódica do produto** (D2), e o estado que
 * sustenta a decisão é o de falha: se o salvamento falhar, a tela **diz**, mostra a hora da última
 * versão salva e devolve o botão manual. Um `catch` silencioso com retentativa invisível é a única
 * variante pior que salvar no clique.
 *
 * **Última escrita vence, sem aviso** (H3). Não há comparação de versão nem aviso de edição
 * concorrente: a consequência — a anotação do colega desaparecer sem ninguém ver — está aceita e
 * registrada no DAP, com a mitigação de uso "um bloco por pessoa durante a sessão". É o bloco ser
 * a unidade de escrita que faz essa mitigação funcionar.
 */

/**
 * 2 s após a última tecla. **Escolha, não medição** — está aqui para ser corrigido pela primeira
 * sessão real, como o teto de 90 s da ADR 0064. Curto o bastante para o risco de perder o bloco
 * caber num parágrafo digitado, longo o bastante para não mandar uma requisição por palavra.
 */
const INTERVALO_DE_SALVAMENTO = 2000;

/** Variante, nunca a cor (ADR 0026). */
const VARIANTE_DO_SALVAMENTO = {
  salvando: "state--0", salvo: "state--1", falhou: "state--3",
} as const;

/** Um só painel existe por vez — o bloco ativo —, e é ele que todo chip da faixa controla. */
const PAINEL_ID = "discovery-session-bloco-painel";

type EstadoDoSalvamento =
  | { tipo: "parado" }
  | { tipo: "salvando" }
  | { tipo: "salvo" }
  | { tipo: "falhou"; mensagem: string };

/** O que fica pendente de gravação: **um** bloco e as respostas dele. */
type Pendente = { bloco: string; respostas: Record<string, string> };

const hora = (iso: string) =>
  new Date(iso).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });

const dataEHora = (iso: string) =>
  new Date(iso).toLocaleString("pt-BR", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });

const plural = (quantos: number, um: string, varios: string) =>
  `${quantos} ${quantos === 1 ? um : varios}`;

export function DiscoverySessionPage({ projectId, sessionId }: { projectId: number; sessionId: number }) {
  const [blocos, setBlocos] = useState<DiscoveryQuestionBlock[]>([]);
  const [sessao, setSessao] = useState<DiscoverySession>();
  const [discovery, setDiscovery] = useState<Discovery>();
  const [observacoes, setObservacoes] = useState<ProcessObservation[]>([]);
  const [blocoAtivo, setBlocoAtivo] = useState("");
  const [respostas, setRespostas] = useState<DiscoverySessionNotes>({});
  const [estado, setEstado] = useState<EstadoDoSalvamento>({ tipo: "parado" });
  const [ultimoSalvamento, setUltimoSalvamento] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [foraDoProjeto, setForaDoProjeto] = useState(false);
  const [estruturando, setEstruturando] = useState(false);

  // **Refs e não estado** para o que o salvamento lê: o `setTimeout` e os ouvintes de saída da
  // página disparam fora do ciclo de renderização, e uma leitura de estado ali seria a versão de
  // quando o efeito foi registrado — isto é, o texto de dois segundos atrás.
  const respostasRef = useRef<DiscoverySessionNotes>({});
  const pendente = useRef<Pendente | null>(null);
  const relogio = useRef<ReturnType<typeof setTimeout>>(undefined);
  const vivo = useRef(true);
  // A retentativa se agenda por este ref, e não pelo próprio nome: um `useCallback` que se
  // referencia é acesso antes da declaração, e a regra `react-hooks/immutability` reprova.
  const salvarDeNovo = useRef<() => void>(() => {});

  // A faixa de blocos como `tablist`: um ref por chip, para a navegação por seta focar o vizinho
  // sem esperar o re-render, e o ref do primeiro campo do painel ativo, para onde o foco desce na
  // ativação deliberada. `moverFocoAoTrocar` é a marca dessa distinção — ver `trocarBloco`.
  const chipRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const primeiroCampoRef = useRef<HTMLTextAreaElement | null>(null);
  const moverFocoAoTrocar = useRef(false);

  /**
   * Grava o bloco pendente **agora**, e é o único caminho de escrita da tela.
   *
   * O pendente só é limpo quando o servidor confirma **aquele** conteúdo: se a pessoa digitou de
   * novo durante a chamada, o objeto pendente é outro e o bloco continua sujo. Na falha ele também
   * fica, e a tela reagenda — é o "a tela segue tentando" da copy, e ele não é silencioso porque o
   * alerta continua na frente até um salvamento dar certo.
   *
   * Devolve a promessa porque **um** chamador precisa esperá-la: estruturar. Extrair de uma
   * anotação que ainda não chegou ao servidor produziria um mapa sem o último bloco, e ninguém
   * veria falta. Os outros — o relógio e a saída da página — a descartam de propósito.
   */
  const salvarAgora = useCallback((): Promise<void> => {
    const enviado = pendente.current;
    clearTimeout(relogio.current);
    if (!enviado) return Promise.resolve();
    // **A retentativa não apaga o alerta.** Voltar o selo para "salvando" aqui esconderia o aviso
    // enquanto a nova tentativa está no ar — e com uma requisição que leva segundos para expirar,
    // ele acenderia e apagaria a cada ciclo. Numa reunião isso lê como "ora está salvo, ora não",
    // que é o oposto do que a decisão D2 comprou: o alerta sai quando um salvamento **dá certo**,
    // não quando outro começa.
    if (vivo.current) setEstado(atual => (atual.tipo === "falhou" ? atual : { tipo: "salvando" }));
    return saveDiscoverySessionBlock(sessionId, enviado.bloco, enviado.respostas)
      .then(atualizada => {
        if (pendente.current === enviado) pendente.current = null;
        if (!vivo.current) return;
        setUltimoSalvamento(atualizada.updated_at);
        setEstado({ tipo: "salvo" });
      })
      .catch((cause: unknown) => {
        if (!vivo.current) return;
        setEstado({ tipo: "falhou", mensagem: mensagemDeFalha(cause) });
        relogio.current = setTimeout(() => salvarDeNovo.current(), INTERVALO_DE_SALVAMENTO);
      });
  }, [sessionId]);
  useEffect(() => { salvarDeNovo.current = () => { void salvarAgora(); }; }, [salvarAgora]);

  const load = useCallback(() => Promise.all([
    listDiscoveryQuestions(),
    getDiscoverySession(sessionId),
    listProcessObservations(sessionId),
  ]).then(async ([base, carregada, observadas]) => {
    // O Discovery diz de qual projeto a sessão é. Sem esta conferência, `/projetos/9/sessoes/3`
    // mostraria o rastro do projeto 9 com o conteúdo de uma sessão de outro — o servidor recusa a
    // sessão fora do escopo, mas não sabe que a rota da SPA prometeu um projeto.
    const levantamento = await api<Discovery>(`/discoveries/${carregada.discovery}/`);
    if (levantamento.project !== projectId) { setForaDoProjeto(true); return; }
    respostasRef.current = carregada.notes ?? {};
    setBlocos(base);
    setSessao(carregada);
    setDiscovery(levantamento);
    setObservacoes(observadas);
    setRespostas(respostasRef.current);
    setBlocoAtivo(base[0]?.id ?? "");
    // Sessão que já tem anotação foi salva alguma vez, e `updated_at` é quando. Sessão em branco
    // não recebe carimbo: "Salvo às 14:38" sobre uma sessão que ninguém tocou seria mentira.
    setUltimoSalvamento(
      Object.values(carregada.notes ?? {}).some(bloco => Object.keys(bloco).length)
        ? carregada.updated_at
        : null,
    );
  }).catch((cause: unknown) => setError(mensagemDeFalha(cause))), [projectId, sessionId]);

  useEffect(() => { void load(); }, [load]);

  // Salvamento imediato **ao sair da página**, e ele é melhor esforço declarado: o navegador pode
  // encerrar a requisição antes de ela chegar. A garantia é o intervalo de 2 s; isto é a última
  // chance, não uma promessa. Fila offline e reenvio em ordem são issue própria (DAP).
  useEffect(() => {
    vivo.current = true;
    const aoEsconder = () => { if (document.visibilityState === "hidden") void salvarAgora(); };
    const aoSair = () => { void salvarAgora(); };
    window.addEventListener("pagehide", aoSair);
    document.addEventListener("visibilitychange", aoEsconder);
    return () => {
      window.removeEventListener("pagehide", aoSair);
      document.removeEventListener("visibilitychange", aoEsconder);
      vivo.current = false;
      void salvarAgora();
    };
  }, [salvarAgora]);

  // O foco desce ao primeiro campo **depois** de o painel novo renderizar — daí ler a marca aqui,
  // e não dentro da própria troca. Roda em toda troca de `blocoAtivo`, inclusive a da carga
  // inicial; a marca começa falsa e só liga na ativação deliberada, então a carga não move foco
  // nenhum.
  useEffect(() => {
    if (!moverFocoAoTrocar.current) return;
    moverFocoAoTrocar.current = false;
    primeiroCampoRef.current?.focus();
  }, [blocoAtivo]);

  /** Uma tecla. **Nunca bloqueia o campo** — travar a digitação é pior que o risco do autosave. */
  const anotar = (blocoId: string, perguntaId: string, texto: string) => {
    const doBloco = { ...(respostasRef.current[blocoId] ?? {}), [perguntaId]: texto };
    respostasRef.current = { ...respostasRef.current, [blocoId]: doBloco };
    setRespostas(respostasRef.current);
    pendente.current = { bloco: blocoId, respostas: doBloco };
    clearTimeout(relogio.current);
    relogio.current = setTimeout(salvarAgora, INTERVALO_DE_SALVAMENTO);
  };

  /**
   * Trocar de bloco salva o anterior **antes**, sem esperar os 2 s.
   *
   * `moverFoco` só é `true` na ativação deliberada do chip — clique, Enter ou Espaço, que o
   * próprio `<button>` já traduz em clique nativo. A navegação por seta chama isto com `false`:
   * ela também troca o bloco (seleção automática, o comportamento certo de um `tablist` — cada
   * bloco é conteúdo, não um formulário que se perde), mas o foco tem de **ficar no chip**, senão
   * tabular a faixa vira impossível de continuar navegando por seta.
   */
  const trocarBloco = (blocoId: string, moverFoco: boolean) => {
    void salvarAgora();
    moverFocoAoTrocar.current = moverFoco;
    setBlocoAtivo(blocoId);
  };

  /**
   * ← → movem a seleção entre os chips da faixa, com volta nas pontas; `Home`/`End` vão ao
   * primeiro/último. O foco do teclado **fica no chip que recebe a seta** — nunca desce ao
   * campo —, porque descê-lo tornaria a própria navegação por seta impossível de continuar.
   */
  const navegarNaFaixa = (event: KeyboardEvent<HTMLDivElement>) => {
    const indiceAtual = blocos.findIndex(item => item.id === blocoAtivo);
    if (indiceAtual === -1) return;

    let proximo: number;
    if (event.key === "ArrowRight") proximo = (indiceAtual + 1) % blocos.length;
    else if (event.key === "ArrowLeft") proximo = (indiceAtual - 1 + blocos.length) % blocos.length;
    else if (event.key === "Home") proximo = 0;
    else if (event.key === "End") proximo = blocos.length - 1;
    else return;

    event.preventDefault();
    trocarBloco(blocos[proximo].id, false);
    chipRefs.current[proximo]?.focus();
  };

  const estruturar = () => {
    setEstruturando(true);
    setError("");
    // O pendente vai **antes**, e esperado: extrair de uma anotação que ainda não chegou ao
    // servidor produziria um mapa sem o último bloco, e ninguém veria falta.
    //
    // Se aquele salvamento falhar, `salvarAgora` trata a falha e devolve uma promessa cumprida —
    // é o desenho dela, porque a tela segue tentando. Aqui isso não basta: estruturar é ato de uma
    // vez só (o segundo é 409), e fazê-lo sobre um bloco que ficou para trás congelaria o mapa sem
    // ele. O pendente ainda cheio é a evidência disso, e é o que barra a extração.
    void salvarAgora()
      .then(() => {
        if (pendente.current) {
          throw new Error("O último bloco ainda não foi salvo — estruturar agora deixaria de fora o que ele tem.");
        }
        return structureDiscoverySession(sessionId);
      })
      .then(() => Promise.all([getDiscoverySession(sessionId), listProcessObservations(sessionId)]))
      .then(([atualizada, observadas]) => { setSessao(atualizada); setObservacoes(observadas); })
      .catch((cause: unknown) => setError(mensagemDeFalha(cause)))
      .finally(() => setEstruturando(false));
  };

  if (foraDoProjeto) return <section className="space-y-7">
    <a href={`/projetos/${projectId}`} className="back-link"><ArrowLeft className="size-4" />Voltar para o projeto</a>
    <section className="panel"><p className="empty-state">Esta sessão de Discovery não é deste projeto.</p></section>
  </section>;
  if (error && !sessao) return <div role="alert" className="alert--error">{error}</div>;
  // O mesmo esqueleto de `ValorPage` — não um estado de carregamento novo.
  if (!sessao || !discovery) return <div className="animate-pulse space-y-6"><div className="h-10 w-64 rounded-xl bg-slate-200" /><div className="h-56 rounded-2xl bg-white" /></div>;

  const bloco = blocos.find(item => item.id === blocoAtivo);
  const doBloco = respostas[blocoAtivo] ?? {};
  const jaEstruturada = observacoes.length > 0;

  return <section className="space-y-7">
    <a href={`/projetos/${projectId}`} className="back-link"><ArrowLeft className="size-4" />Projeto · {discovery.project_name}</a>
    <header className="page-head">
      {/* "DISCOVERY SESSION" é o termo canônico, em inglês, com a copy em volta em pt-BR — a
          mesma regra do mapa de linguagem §1 que a decisão A do DAP de Engagements registrou. */}
      <p className="eyebrow">DISCOVERY SESSION</p>
      <h1>Sessão de {dataEHora(sessao.happened_at)}</h1>
      <p>{sessao.participants || "Sem participantes registrados."}{discovery.scope ? ` · ${discovery.scope}` : ""}</p>
    </header>

    {error && <p role="alert" className="alert--error">{error}</p>}

    {/* Semanticamente é um conjunto de abas — cada chip mostra um painel —, daí o padrão
        WAI-ARIA de tabs: `tablist`/`tab`/`tabpanel`, roving `tabIndex` (só o chip selecionado é
        tabulável, os demais ficam fora da ordem de Tab) e navegação por seta. */}
    <div className="filter-bar" role="tablist" aria-label="Blocos de perguntas da sessão" onKeyDown={navegarNaFaixa}>
      {blocos.map((item, indice) => <button
        key={item.id}
        ref={elemento => { chipRefs.current[indice] = elemento; }}
        id={`bloco-tab-${item.id}`}
        type="button"
        role="tab"
        aria-selected={item.id === blocoAtivo}
        aria-controls={PAINEL_ID}
        tabIndex={item.id === blocoAtivo ? 0 : -1}
        className={`filter-chip ${item.id === blocoAtivo ? "filter-chip--on" : ""}`}
        onClick={() => trocarBloco(item.id, true)}
      >{item.id.toUpperCase()} · {item.short_label}</button>)}
    </div>

    {bloco ? <section className="panel" id={PAINEL_ID} role="tabpanel" aria-labelledby={`bloco-tab-${blocoAtivo}`}>
      <div className="panel-heading">
        <h2>Bloco {bloco.id.toUpperCase()} — {bloco.label}</h2>
        {/* O indicador é **discreto e no cabeçalho**, no lugar onde o carimbo do último
            salvamento já ficava. Ele nunca desabilita nada abaixo. */}
        {estado.tipo === "salvando" && <span className={`state ${VARIANTE_DO_SALVAMENTO.salvando}`}>Salvando…</span>}
        {estado.tipo === "falhou" && <span className={`state ${VARIANTE_DO_SALVAMENTO.falhou}`}>Não foi possível salvar</span>}
        {estado.tipo !== "salvando" && estado.tipo !== "falhou" && ultimoSalvamento
          && <span className={`state ${VARIANTE_DO_SALVAMENTO.salvo}`}>Salvo às {hora(ultimoSalvamento)}</span>}
      </div>

      {/* O estado que sustenta a decisão D2. Ele diz **qual foi a última versão salva** — sem essa
          hora, "não foi possível salvar" não responde a única pergunta que importa numa reunião:
          o que já está seguro. */}
      {estado.tipo === "falhou" && <p role="alert" className="alert--error mb-4">
        {estado.mensagem}{" "}
        {ultimoSalvamento
          ? <strong>A última versão salva é a das {hora(ultimoSalvamento)}.</strong>
          : <strong>Nenhuma versão deste bloco chegou a ser salva.</strong>}
        {" "}O que você digitou continua aqui e a tela segue tentando.
      </p>}

      {bloco.note && <p className="mb-4 text-sm text-muted">{bloco.note}</p>}

      <div className="grid gap-4">
        {bloco.questions.map((pergunta, indice) => <label className="form-label" key={pergunta.id}>
          {pergunta.text}
          <textarea
            ref={indice === 0 ? primeiroCampoRef : undefined}
            className="field min-h-20"
            value={doBloco[pergunta.id] ?? ""}
            onChange={event => anotar(bloco.id, pergunta.id, event.target.value)}
          />
        </label>)}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-line pt-4">
        <p className="min-w-0 flex-1 text-xs text-muted">
          Tudo o que sai daqui entra como evidência declarada, nunca como Baseline. Estruturar em
          processos e achados é ato à parte, depois da sessão.
        </p>
        {/* O botão manual **volta quando o automático falha**, e some quando ele funciona: o
            autosave não elimina o botão, ele o esconde enquanto está dando conta. */}
        {estado.tipo === "falhou"
          ? <button type="button" className="btn" onClick={() => void salvarAgora()}>Tentar salvar agora</button>
          : <span className="text-xs text-muted">Salvo automaticamente</span>}
      </div>
    </section> : <p className="empty-state">Nenhum bloco de perguntas disponível.</p>}

    <section className="panel">
      <div className="panel-heading">
        <h2>Estruturar em processos e achados</h2>
        {jaEstruturada && <span className="state state--active">Estruturada</span>}
      </div>
      {jaEstruturada ? <div className="grid gap-3">
        <p className="text-sm text-ink">
          <strong>{plural(observacoes.length, "processo", "processos")} · {plural(sessao.structured_finding_count, "achado", "achados")}</strong>
        </p>
        <p className="text-xs text-muted">Todos como hipótese, aguardando revisão.</p>
        <div className="row-meta">
          {observacoes.map(observacao => <span className="state state--off" key={observacao.id}>{observacao.process_name}</span>)}
        </div>
        <a className="btn btn--secondary w-fit" href={`/contas/${observacoes[0].account}`}>Ver os processos</a>
      </div> : <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-0 flex-1 text-sm text-muted">
          Todo achado nasce como hipótese e é revisado depois. A extração lê o que foi anotado
          aqui — faça-a quando a sessão terminar.
        </p>
        <button type="button" className="btn" onClick={estruturar} disabled={estruturando}>
          <Workflow className="size-4" />{estruturando ? "Estruturando…" : "Estruturar"}
        </button>
      </div>}
    </section>

    
  </section>;
}
