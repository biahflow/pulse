import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { DiscoverySessionPage } from "./DiscoverySessionPage";

/**
 * A Discovery Session (FDD 055; DAP `dap-discovery-session-e-business-case-r2`, C1 · D2 · E1 · H3).
 *
 * O que estes testes protegem é o **autosave**, e dentro dele o estado de falha: o desenho inteiro
 * de D2 se apoia em a tela **dizer** quando não conseguiu salvar, com a hora da última versão que
 * chegou ao servidor, e devolver o botão manual. Um `catch` silencioso passaria por todos os
 * outros testes desta suíte e reproduziria exatamente o defeito que a decisão existe para evitar.
 */

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  listDiscoveryQuestions: vi.fn(),
  getDiscoverySession: vi.fn(),
  listProcessObservations: vi.fn(),
  saveDiscoverySessionBlock: vi.fn(),
  structureDiscoverySession: vi.fn(),
}));
vi.mock("../api", () => mocks);

const BLOCOS = [
  {
    id: "a", label: "Contexto executivo", short_label: "Contexto executivo", note: "",
    questions: [{ id: "o-que-mais-incomoda", text: "Quando você olha o resultado do mês, o que mais te incomoda?" }],
  },
  {
    id: "b", label: "Follow the work (com quem executa)", short_label: "Follow the work",
    note: "",
    questions: [{ id: "casos-por-mes", text: "Quantos casos desse tipo passam por aqui num mês?" }],
  },
];

const DISCOVERY = {
  id: 7, project: 1, project_name: "Discovery Sprint — Home Care XPTO",
  scope: "Faturamento e repasse", status: "running", status_display: "Em andamento",
  started_at: "2026-09-01", completed_at: null, owner: 1,
  created_at: "2026-09-01T09:00:00Z", updated_at: "2026-09-01T09:00:00Z",
};

const sessao = (overrides: Record<string, unknown> = {}) => ({
  id: 3, discovery: 7, meeting: null, happened_at: "2026-09-05T17:00:00Z",
  participants: "Ana Meireles, Paulo Rangel", source_artifact: null, transcript: "",
  notes: {}, structured_finding_count: 0,
  created_at: "2026-09-05T17:00:00Z", updated_at: "2026-09-05T17:38:00Z",
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.api.mockResolvedValue(DISCOVERY);
  mocks.listDiscoveryQuestions.mockResolvedValue(BLOCOS);
  mocks.getDiscoverySession.mockResolvedValue(sessao());
  mocks.listProcessObservations.mockResolvedValue([]);
  mocks.saveDiscoverySessionBlock.mockImplementation(() => Promise.resolve(sessao({ updated_at: "2026-09-05T17:45:00Z" })));
  mocks.structureDiscoverySession.mockResolvedValue({ processes: [] });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function abrir() {
  render(<DiscoverySessionPage projectId={1} sessionId={3} />);
  await screen.findByRole("heading", { level: 1, name: /Sessão de/ });
}

/**
 * O relógio falso entra **depois** da carga: `findBy*` espera por timer, e ligá-lo antes trava a
 * abertura da tela em vez de exercitar o autosave.
 *
 * Os testes de autosave usam `fireEvent` e não `userEvent`, e a escolha é deliberada: o
 * `userEvent` agenda os próprios `setTimeout` entre teclas e, sob relógio falso, quem os avança é
 * o teste — o que se exercitaria seria a mecânica da digitação, não o debounce de 2 s, que é o
 * assunto. `fireEvent` é síncrono e não toca em timer nenhum.
 */
const relogioFalso = () => vi.useFakeTimers();

/** Avança o relógio **dentro de `act`**: o que corre no fim do intervalo é um `setState`. */
const avancar = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

/** Uma anotação no campo, como a pessoa a deixa depois de digitar. */
const anotar = (campo: HTMLElement, texto: string) =>
  fireEvent.change(campo, { target: { value: texto } });

// A metade estrutural — nenhuma pergunta escrita no arquivo da tela — está em
// `src/test/tela-da-sessao-de-discovery.test.ts`, que lê a fonte com `node:fs`.
test("as perguntas e os rótulos dos blocos vêm do backend", async () => {
  await abrir();

  // A faixa de blocos é a que o servidor mandou, com a forma curta do rótulo.
  expect(screen.getByRole("tab", { name: "A · Contexto executivo" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "B · Follow the work" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Bloco A — Contexto executivo" })).toBeInTheDocument();
  expect(screen.getByLabelText(/o que mais te incomoda/)).toBeInTheDocument();
});

test("a resposta é gravada sob o id da pergunta, e o campo nunca bloqueia", async () => {
  await abrir();
  relogioFalso();

  const campo = screen.getByLabelText(/o que mais te incomoda/);
  anotar(campo, "O fechamento atrasa.");
  // Antes dos 2 s nada foi enviado — o gatilho é a pausa, não a tecla.
  expect(mocks.saveDiscoverySessionBlock).not.toHaveBeenCalled();
  // E o campo segue editável enquanto isso: travar a digitação é pior que o risco do autosave.
  expect(campo).not.toBeDisabled();

  await avancar(2000);

  expect(mocks.saveDiscoverySessionBlock).toHaveBeenCalledWith(
    3, "a", { "o-que-mais-incomoda": "O fechamento atrasa." },
  );
  expect(campo).not.toBeDisabled();
});

test("trocar de bloco salva o anterior antes, sem esperar os 2 s", async () => {
  await abrir();
  relogioFalso();

  anotar(screen.getByLabelText(/o que mais te incomoda/), "Atrasa.");
  fireEvent.click(screen.getByRole("tab", { name: "B · Follow the work" }));

  expect(mocks.saveDiscoverySessionBlock).toHaveBeenCalledWith(3, "a", { "o-que-mais-incomoda": "Atrasa." });
  expect(screen.getByRole("heading", { name: "Bloco B — Follow the work (com quem executa)" })).toBeInTheDocument();
});

test("quando falha, a tela diz a última versão salva, devolve o botão e não descarta o texto", async () => {
  await abrir();
  relogioFalso();

  // Primeiro salvamento dá certo: é dele que sai a hora que o alerta vai citar.
  anotar(screen.getByLabelText(/o que mais te incomoda/), "Atrasa.");
  await avancar(2000);
  const hora = screen.getByText(/^Salvo às /);
  const carimbo = hora.textContent!.replace("Salvo às ", "");

  mocks.saveDiscoverySessionBlock.mockRejectedValue(
    Object.assign(new Error("Sem conexão com o servidor."), { status: 0 }),
  );
  anotar(screen.getByLabelText(/o que mais te incomoda/), "Atrasa. Muito.");
  await avancar(2000);

  const alerta = screen.getByRole("alert");
  expect(alerta).toHaveTextContent("Sem conexão com o servidor.");
  expect(alerta).toHaveTextContent(`A última versão salva é a das ${carimbo}.`);
  expect(alerta).toHaveTextContent("O que você digitou continua aqui e a tela segue tentando.");
  // O selo troca para o de falha, e o botão manual **volta**.
  expect(screen.getByText("Não foi possível salvar")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Tentar salvar agora" })).toBeInTheDocument();
  // E o texto continua no campo, que é o que nenhum caminho de falha pode descartar.
  expect(screen.getByLabelText(/o que mais te incomoda/)).toHaveValue("Atrasa. Muito.");
});

test("a falha não é silenciosa: a tela continua tentando, e diz que está tentando", async () => {
  await abrir();
  relogioFalso();
  mocks.saveDiscoverySessionBlock.mockRejectedValue(new Error("Sem conexão com o servidor."));

  anotar(screen.getByLabelText(/o que mais te incomoda/), "Atrasa.");
  await avancar(2000);
  expect(mocks.saveDiscoverySessionBlock).toHaveBeenCalledTimes(1);

  await avancar(2000);

  expect(mocks.saveDiscoverySessionBlock).toHaveBeenCalledTimes(2);
  // A retentativa **não** apaga o alerta: um reenvio invisível é o defeito que D2 fecha.
  expect(screen.getByRole("alert")).toHaveTextContent("Sem conexão com o servidor.");
});

test("o alerta não pisca enquanto a retentativa está em voo", async () => {
  // O teste acima observa o alerta **depois** da retentativa falhar. O intervalo que falta é o em
  // que ela está no ar: voltar o selo para "salvando" ali apaga o alerta, e com uma requisição que
  // leva segundos para expirar ele acende e apaga a cada ciclo. Numa reunião isso lê como "ora
  // está salvo, ora não" — que é o oposto do que a decisão D2 comprou.
  await abrir();
  relogioFalso();
  mocks.saveDiscoverySessionBlock.mockRejectedValueOnce(new Error("Sem conexão com o servidor."));

  anotar(screen.getByLabelText(/o que mais te incomoda/), "Atrasa.");
  await avancar(2000);
  expect(screen.getByRole("alert")).toBeInTheDocument();

  // A retentativa fica pendente — nem resolve, nem rejeita.
  mocks.saveDiscoverySessionBlock.mockReturnValueOnce(new Promise(() => {}));
  await avancar(2000);

  expect(mocks.saveDiscoverySessionBlock).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("alert")).toHaveTextContent("Sem conexão com o servidor.");
});

test("o botão manual reenvia o mesmo bloco e o alerta some quando dá certo", async () => {
  await abrir();
  relogioFalso();
  mocks.saveDiscoverySessionBlock.mockRejectedValueOnce(new Error("Sem conexão com o servidor."));

  anotar(screen.getByLabelText(/o que mais te incomoda/), "Atrasa.");
  await avancar(2000);
  fireEvent.click(screen.getByRole("button", { name: "Tentar salvar agora" }));
  await avancar(0);

  expect(mocks.saveDiscoverySessionBlock).toHaveBeenLastCalledWith(3, "a", { "o-que-mais-incomoda": "Atrasa." });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Tentar salvar agora" })).not.toBeInTheDocument();
});

test("a sessão em branco não finge ter sido salva", async () => {
  await abrir();

  expect(screen.queryByText(/^Salvo às /)).not.toBeInTheDocument();
  expect(screen.getByText("Salvo automaticamente")).toBeInTheDocument();
});

test("a sessão que já tem anotação abre com o texto e o carimbo do último salvamento", async () => {
  mocks.getDiscoverySession.mockResolvedValue(
    sessao({ notes: { a: { "o-que-mais-incomoda": "O fechamento atrasa." } } }),
  );

  await abrir();

  expect(screen.getByLabelText(/o que mais te incomoda/)).toHaveValue("O fechamento atrasa.");
  expect(screen.getByText(/^Salvo às /)).toBeInTheDocument();
});

test("estruturar é o ato explícito, e a tela nunca grava achado", async () => {
  const user = userEvent.setup();
  await abrir();

  await user.click(screen.getByRole("button", { name: /Estruturar/ }));

  await waitFor(() => expect(mocks.structureDiscoverySession).toHaveBeenCalledWith(3));
  // Nenhuma chamada da tela toca o recurso de achado — nem por `api()` cru. A metade estrutural
  // (o arquivo não ter caminho para isso) fica em `src/test/tela-da-sessao-de-discovery.test.ts`,
  // que lê a fonte com `node:fs` e por isso mora fora do `tsconfig.app.json`.
  expect(mocks.api.mock.calls.every(([caminho]) => !String(caminho).includes("finding"))).toBe(true);
});

test("estruturar não acontece sobre um bloco que ficou para trás", async () => {
  await abrir();
  relogioFalso();
  mocks.saveDiscoverySessionBlock.mockRejectedValue(new Error("Sem conexão com o servidor."));

  anotar(screen.getByLabelText(/o que mais te incomoda/), "Atrasa.");
  await avancar(2000);
  fireEvent.click(screen.getByRole("button", { name: /Estruturar/ }));
  await avancar(0);

  // Extrair é ato de uma vez só — o segundo é 409. Fazê-lo com o bloco pendente congelaria o mapa
  // sem ele, e ninguém veria falta.
  expect(mocks.structureDiscoverySession).not.toHaveBeenCalled();
  expect(screen.getAllByRole("alert").some(
    aviso => aviso.textContent?.includes("O último bloco ainda não foi salvo"),
  )).toBe(true);
});

test("a sessão já estruturada mostra o selo, os processos e o caminho até eles", async () => {
  mocks.getDiscoverySession.mockResolvedValue(sessao({ structured_finding_count: 11 }));
  mocks.listProcessObservations.mockResolvedValue([
    { id: 1, discovery: 7, process: 21, process_name: "Conferência de repasse", account: 4, observed_at: "2026-09-05", observation_type: "initial", observation_type_display: "Primeira observação", source_session: 3, created_at: "", updated_at: "" },
    { id: 2, discovery: 7, process: 22, process_name: "Cadastro de prestador", account: 4, observed_at: "2026-09-05", observation_type: "initial", observation_type_display: "Primeira observação", source_session: 3, created_at: "", updated_at: "" },
  ]);

  await abrir();

  const painel = screen.getByRole("heading", { name: "Estruturar em processos e achados" }).closest("section")!;
  expect(within(painel).getByText("Estruturada")).toBeInTheDocument();
  expect(within(painel).getByText("2 processos · 11 achados")).toBeInTheDocument();
  expect(within(painel).getByText("Conferência de repasse")).toBeInTheDocument();
  expect(within(painel).getByRole("link", { name: "Ver os processos" })).toHaveAttribute("href", "/contas/4");
  // Já estruturada não oferece o botão de novo: o servidor recusa com 409, e a tela não convida.
  expect(within(painel).queryByRole("button", { name: /Estruturar/ })).not.toBeInTheDocument();
});

test("a cronometragem reservada não aparece na tela — ela vive no board", async () => {
  // O pacote de design comunica a fronteira ao **aprovador**, não ao operador: o pacote irmão diz
  // que o reservado aparece *no board*, e a tabela de procedência deste não lista nenhuma classe
  // nova. Um cartão anunciando o que virá ocupa, numa reunião de duas horas, a atenção que a tela
  // existe para poupar — e traria uma classe de CSS cujo único consumidor seria ele mesmo.
  await abrir();

  expect(screen.queryByText(/Cronometragem/i)).not.toBeInTheDocument();
  expect(document.querySelector(".panel--reserved")).toBeNull();
});

test("sessão de outro projeto não é mostrada sob o rastro deste", async () => {
  mocks.api.mockResolvedValue({ ...DISCOVERY, project: 99 });

  render(<DiscoverySessionPage projectId={1} sessionId={3} />);

  expect(await screen.findByText("Esta sessão de Discovery não é deste projeto.")).toBeInTheDocument();
});

/**
 * Ordem de foco no teclado — o que o DAP `dap-discovery-session-e-business-case-r2` registrou em
 * "Notas para quem implementa" como não especificado, e que "merece atenção" por esta ser a tela
 * mais usada por teclado do produto: alguém digita nela durante uma reunião de duas horas. O axe
 * não mede nada disto — 2.4.7 (foco visível) e a ordem de foco em si são verificação manual —, daí
 * estes testes de comportamento em vez de mais uma varredura.
 *
 * `userEvent`, não `fireEvent`: aqui o que se exercita é a mecânica real de tabulação e seta, ao
 * contrário dos testes de autosave acima, que usam `fireEvent` de propósito por causa do relógio
 * falso (`userEvent` agenda os próprios timers entre teclas e trava sob relógio falso). Nenhum
 * teste abaixo liga `relogioFalso()`.
 */

test("ativar um bloco por clique põe o foco no primeiro campo do bloco novo", async () => {
  // Antes desta entrega, o clique trocava o painel e deixava o foco preso no chip: quem conduz a
  // reunião tinha de tabular por toda a faixa até o primeiro campo — atrito que, ao vivo, custa a
  // atenção que a tela existe para poupar.
  const user = userEvent.setup();
  await abrir();

  await user.click(screen.getByRole("tab", { name: "B · Follow the work" }));

  expect(screen.getByLabelText(/Quantos casos desse tipo passam por aqui num mês/)).toHaveFocus();
});

test("tabular pela faixa não troca de bloco nem move o foco para o campo", async () => {
  // Só passar o foco por cima do chip (sem clicar, sem Enter, sem Espaço) não é ativação — o
  // bloco tem de continuar o mesmo, e o campo não pode receber foco nenhum por causa disso.
  const user = userEvent.setup();
  await abrir();

  await user.tab(); // "Voltar para o projeto", único elemento tabulável antes da faixa.
  await user.tab(); // o único chip tabulável da faixa — o que já está selecionado ("A").

  expect(screen.getByRole("tab", { name: "A · Contexto executivo" })).toHaveFocus();
  expect(screen.getByRole("heading", { name: "Bloco A — Contexto executivo" })).toBeInTheDocument();
  expect(screen.getByLabelText(/o que mais te incomoda/)).not.toHaveFocus();
});

test("só o chip selecionado é tabulável — os outros ficam fora da ordem de Tab", async () => {
  // Roving tabIndex: é o que faz um único Tab entrar e sair da faixa, em vez de percorrer os seis
  // chips até chegar ao primeiro campo.
  await abrir();

  expect(screen.getByRole("tab", { name: "A · Contexto executivo" })).toHaveAttribute("tabindex", "0");
  expect(screen.getByRole("tab", { name: "B · Follow the work" })).toHaveAttribute("tabindex", "-1");
});

test("seta para a direita move a seleção para o próximo chip e mantém o foco nele", async () => {
  // A ativação por seta troca o bloco (seleção automática de tablist), mas o foco tem de ficar no
  // chip — descê-lo ao campo, como na ativação por clique, tornaria a própria navegação por seta
  // impossível de continuar.
  const user = userEvent.setup();
  await abrir();
  await user.tab();
  await user.tab(); // foco no chip "A"

  await user.keyboard("{ArrowRight}");

  const chipB = screen.getByRole("tab", { name: "B · Follow the work" });
  expect(chipB).toHaveFocus();
  expect(chipB).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("heading", { name: "Bloco B — Follow the work (com quem executa)" })).toBeInTheDocument();
  expect(screen.getByLabelText(/Quantos casos desse tipo passam por aqui num mês/)).not.toHaveFocus();
});

test("seta para a esquerda no primeiro chip volta para o último (volta nas pontas)", async () => {
  const user = userEvent.setup();
  await abrir();
  await user.tab();
  await user.tab(); // foco no chip "A", o primeiro

  await user.keyboard("{ArrowLeft}");

  const chipB = screen.getByRole("tab", { name: "B · Follow the work" });
  expect(chipB).toHaveFocus();
  expect(chipB).toHaveAttribute("aria-selected", "true");
});

test("Home e End vão ao primeiro e ao último chip da faixa", async () => {
  const user = userEvent.setup();
  await abrir();
  await user.tab();
  await user.tab(); // foco no chip "A"

  const chipA = screen.getByRole("tab", { name: "A · Contexto executivo" });
  const chipB = screen.getByRole("tab", { name: "B · Follow the work" });

  await user.keyboard("{End}");
  expect(chipB).toHaveFocus();
  expect(chipB).toHaveAttribute("aria-selected", "true");

  await user.keyboard("{Home}");
  expect(chipA).toHaveFocus();
  expect(chipA).toHaveAttribute("aria-selected", "true");
});
