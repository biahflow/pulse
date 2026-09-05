"""O conteúdo de um `Document` é PDF? **Uma** regra, **uma** expressão (ADR 0010).

O produto responde essa pergunta em três momentos que não se parecem: o carimbo
`Document.content_is_pdf` no upload (`DocumentSerializer.create`), o aviso que a tela de
assinatura precisa dar **antes** do clique (`esign.lacuna_de_posicionamento`) e a leitura das
âncoras na hora do envio real (`esign._itens_da_ultima_pagina`). Os três comparavam o mesmo
`b"%PDF"` por conta própria — o defeito que fez `dinheiro.py` nascer, aqui na escala de uma linha:
três lugares argumentando a mesma coisa, e nada ficando vermelho no dia em que um deles mudar de
ideia (ficar mais estrito exigindo `%PDF-1.`, ou mais frouxo tolerando um BOM na frente). Os dois
primeiros decidem o que a tela mostra e o terceiro decide onde a assinatura cai; divergirem entre si
é a tela prometer uma coisa e o envio fazer outra.

**Recebe bytes, e não o arquivo nem o `Document`.** Cada chamador lê da fonte que tem — o upload em
memória, o arquivo local do storage, o conteúdo já baixado do Drive —, e centralizar a *leitura*
junto da *decisão* acoplaria três contextos de I/O diferentes num lugar só. O que mora aqui é a
decisão, e só ela.

**Não mora em `esign.py`, de propósito.** O farejamento é sobre o documento, não sobre a
assinatura: pô-lo lá faria o `DocumentSerializer` importar do módulo de assinatura para carimbar um
upload, e o carimbo existe mesmo numa instalação em que a assinatura eletrônica nunca seja ligada.

**Responde `bool`, nunca `None`.** `Document.content_is_pdf` é `null=True` e o `null` ali significa
"não medido" — jamais `False` (a regra do não-apurado). Quem tem os bytes em mãos sabe a resposta;
quem decide que não há o que medir é o chamador, e continua sendo ele.

Importa-se o símbolo (`from .document import conteudo_e_pdf`) e não o módulo: `document` é o nome da
variável local em quase toda função que mexe com um documento — inclusive na própria
`DocumentSerializer.create` —, e um `from . import document` viraria `UnboundLocalError` na primeira
delas. É a mesma colisão que `serializers.py` já resolve com `process as process_module`, na versão
em que o apelido não é necessário.
"""

from __future__ import annotations

# O reconhecimento é pelo **conteúdo** e não pela extensão (ADR 0065): `original_name` é digitado
# por gente, e um `.docx` batizado de `contrato.pdf` precisa cair aqui — e cai certo.
PREFIXO_DO_PDF = b"%PDF"


def conteudo_e_pdf(conteudo: bytes) -> bool:
    """O começo do arquivo diz que ele é um PDF?

    `startswith` e não igualdade, porque o chamador passa o pedaço que tinha para ler: cinco bytes
    no upload, o arquivo inteiro no envio da assinatura. Os dois têm de responder o mesmo, e é essa
    a razão de a decisão ser uma função sobre bytes em vez de um método sobre arquivo.
    """
    return conteudo.startswith(PREFIXO_DO_PDF)
