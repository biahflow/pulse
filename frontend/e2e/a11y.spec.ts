/**
 * Acessibilidade: varredura axe em toda a matriz de telas, nas três larguras (FDD 022).
 *
 * Roda no browser de verdade, não em jsdom, porque as duas falhas que mais doem aqui —
 * contraste de texto e indicador de foco — dependem de layout e de CSS aplicado, e jsdom não
 * calcula nem um nem outro. Um `jest-axe` no Vitest daria a sensação de cobertura sem ver
 * justamente o que estava quebrado.
 *
 * Roda nas três larguras porque a marcação **muda** com a largura: abaixo de `lg` o `<aside>`
 * some e o menu hambúrguer aparece, então o menu mobile só existe para o axe no viewport
 * mobile e tablet.
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "./fixtures";

import { abrir, ROUTES } from "./matrix";

/**
 * O axe **não** cobre isto, e é bom saber por quê: "foco visível" (WCAG 2.4.7) depende de
 * comparar o antes e o depois de um estado que a página não está exibindo, então a regra é de
 * verificação manual — não existe checagem automatizada dela no axe. Foi verificado: com o
 * `focus:outline-none` cru de volta no `index.css`, as 51 varreduras acima continuavam passando.
 *
 * Sem este teste, a correção de foco desta mesma entrega ficaria sem gate nenhum — e seria
 * desfeita no primeiro `focus:outline-none` que alguém colasse de volta.
 *
 * `Tab` e não `.focus()`: foco programático nem sempre casa com `:focus-visible` no Chromium,
 * que é justamente o seletor em teste. Precisa ser navegação por teclado de verdade.
 */
test("o foco de teclado fica visível nos botões", async ({ page }) => {
  await abrir(page, { path: "/", name: "Visão geral", role: "admin" });

  // O orçamento de tabulações precisa caber a navegação inteira antes do primeiro `<button>`: são
  // links até lá, e cada item novo no menu lateral consome um salto. Com 12 ele estourava assim
  // que "Serviços" entrou na lista.
  for (let salto = 0; salto < 24; salto++) {
    await page.keyboard.press("Tab");
    const foco = await page.evaluate(() => {
      const alvo = document.activeElement;
      if (!alvo || alvo.tagName !== "BUTTON") return null;
      const estilo = getComputedStyle(alvo);
      return {
        rotulo: alvo.getAttribute("aria-label") ?? alvo.textContent?.trim().slice(0, 40) ?? "",
        estilo: estilo.outlineStyle,
        largura: parseFloat(estilo.outlineWidth),
      };
    });
    if (!foco) continue;

    expect(foco.estilo, `botão "${foco.rotulo}" sem contorno de foco`).not.toBe("none");
    expect(foco.largura, `botão "${foco.rotulo}" com contorno de foco de largura zero`).toBeGreaterThan(0);
    return;
  }
  throw new Error("nenhum botão recebeu foco em 12 tabulações — o teste não chegou a verificar nada");
});

/**
 * Ordem de foco na Discovery Session (FDD 055; DAP `dap-discovery-session-e-business-case-r2`,
 * "Notas para quem implementa"): o pacote registrou a ordem de foco como não especificada e
 * "merece atenção" — é a tela mais usada por teclado do produto, com alguém digitando durante uma
 * reunião de duas horas. O axe não mede nada disto (é verificação manual, como o teste acima); o
 * vitest de `DiscoverySessionPage.test.tsx` cobre a mesma lógica em jsdom, e este é o teclado de
 * verdade, no molde do teste de foco visível: interação real, não `.focus()` isolado nem asserção
 * de estado sem passar pelo DOM.
 */
test("a faixa de blocos da Discovery Session ativa por clique e navega por seta sem perder o foco do chip", async ({ page }) => {
  await abrir(page, { path: "/projetos/1/sessoes/3", name: "Discovery Session", role: "admin" });

  // Ativação deliberada (clique) desce o foco ao primeiro campo do bloco novo — antes desta
  // entrega, ficava preso no chip, e tabular a faixa inteira até o campo era o atrito que o DAP
  // sinalizou.
  const chipC = page.getByRole("tab", { name: /C · Sistemas e dados/ });
  await chipC.click();
  await expect(page.getByLabel(/Quais sistemas participam desse processo/)).toBeFocused();

  // Seta troca de bloco (seleção automática de tablist), mas o foco tem de ficar no chip — descê-
  // lo ao campo, como na ativação por clique, tornaria a própria navegação por seta impossível de
  // continuar.
  await chipC.focus();
  await page.keyboard.press("ArrowRight");
  const chipD = page.getByRole("tab", { name: /D · Sponsor e acesso/ });
  await expect(chipD).toBeFocused();
  await expect(chipD).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel(/bate o martelo/)).not.toBeFocused();

  // `Home` volta ao primeiro chip da faixa.
  await page.keyboard.press("Home");
  const chipA = page.getByRole("tab", { name: /A · Contexto executivo/ });
  await expect(chipA).toBeFocused();
  await expect(chipA).toHaveAttribute("aria-selected", "true");
});

for (const screen of ROUTES) {
  test(`${screen.name} não tem violação de acessibilidade`, async ({ page }) => {
    await abrir(page, screen);

    // A matriz já passou meses "cobrindo" esta pílula sem nunca renderizá-la: `serie` é
    // 1-based e o fixture comparava o índice com zero. Esta asserção torna a medição não-vazia.
    // Como esta spec roda nos projetos mobile, tablet e desktop, as três larguras provam que a
    // decisão de uma fase concluída chegou ao DOM antes de o axe medir seu contraste.
    if (screen.path === "/projetos/1") {
      const decisaoDaFase = page.locator(
        'span.state[title="Seguimos monitorando a acurácia do OCR."]',
      );
      await expect(decisaoDaFase).toHaveText("CONDITIONAL GO");
      await expect(decisaoDaFase).toBeVisible();
    }

    // O Business Case (FDD 053, DAP `dap-discovery-session-e-business-case-r2`) mora dentro do
    // acordeão da oportunidade, que abre fechado: sem o clique, o `.alert--warn` do custo não
    // apurado (decisão F1) e os dois botões de decisão nunca chegam ao DOM, e o axe mediria só a
    // metade da tela que a decisão A1 acrescentou.
    if (screen.path === "/contas/1/priorizacao") {
      await page.getByLabel("Abrir detalhe: Padronizar o checklist de documentação exigida para faturamento TISS").click();
      await expect(page.getByRole("alert").filter({ hasText: "Nenhum processo desta oportunidade tem custo sustentado por fato." })).toBeVisible();
    }

    // A rodada de assinatura (DAP `dap-assinatura-com-papeis-r1`) só existe dentro de um diálogo,
    // e o `.alert--warn` do aviso E1 é **cor nova** — sem abrir o modal aqui, a única superfície
    // âmbar em forma de alerta do produto nunca passaria pela medição de contraste. O primeiro
    // documento da fixture é o que vem com `signature_positioning_gap`, para o aviso renderizar.
    if (screen.path === "/documentos") {
      await page.getByRole("button", { name: /para assinatura$/ }).first().click();
      const rodada = page.getByRole("dialog", { name: "Enviar para assinatura" });
      await expect(rodada).toBeVisible();
      await expect(rodada.getByText(/última página do relatório/)).toBeVisible();
      // Os contatos da conta chegam depois do clique: sem esperar, o axe mede o modal antes de o
      // seletor de contato existir.
      await expect(rodada.getByLabel("Contato da conta")).toBeVisible();
    }

    const { violations } = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();

    // Mensagem com regra, impacto e alvo: "1 violação" sem o seletor obriga quem quebrou a
    // reproduzir à mão para descobrir onde.
    expect(
      violations.map(v => `${v.id} (${v.impact}) — ${v.nodes.map(n => n.target.join(" ")).join(", ")}`),
    ).toEqual([]);
  });
}
