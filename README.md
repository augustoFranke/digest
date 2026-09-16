# Digest

Compila transcrições, vídeos e slides em um pacote de contexto para agentes de IA. A fala fica em `transcript.md`; frames e páginas de slides recuperam o contexto visual de expressões como “essa linha aqui”. O programa prepara as fontes; estudar o pacote ou escrever notas é uma tarefa separada, conforme o pedido do usuário.

Há **um único comando**, `uv run digest.py`, com `--transcript`, `--transcribe`, `--audio` e `--slides`. Os vídeos são argumentos posicionais. Coloque vídeos e áudios na ordem cronológica.

## Instalação

Python 3.12+ e [uv](https://docs.astral.sh/uv/). Use o lockfile do projeto:

```bash
uv sync
```

Para qualquer modo com vídeo, instale FFmpeg (inclui `ffprobe`):

```bash
brew install ffmpeg
```

Para **gerar** uma transcrição a partir do áudio, instale também o extra opcional:

```bash
uv sync --extra audio
```

Os exemplos com `--transcribe` usam `uv run --extra audio` para manter esse extra instalado. Os demais modos não carregam o reconhecedor de voz.

Slides PDF usam `pypdf`, já incluído. Para renderizar imagens das páginas, instale `brew install poppler`; sem ele, PDF e texto continuam disponíveis. Para PowerPoint, instale `brew install --cask libreoffice`.

## Organização dos pacotes

Use `lectures/` para aulas e materiais acadêmicos e `work/` para reuniões e materiais profissionais. Em `-o`, informe o diretório e a pasta do pacote; por exemplo, `work/2026-09-09-meet`. O compilador aceita esses caminhos sem configuração adicional.

Somente `lectures/` é acessível por `~/Learning/lectures`. `work/` fica separado dentro de Digest e não entra em Learning nem no vault. Aulas e reuniões de trabalho seguem o pedido específico, como gerar apenas uma transcrição.

## Escolha do modo

### 1. Transcrição pronta + vídeo (com ou sem áudio)

Use quando já houver um `.txt` timestampado, como a exportação do Wispr Flow. O áudio do vídeo é ignorado; a fonte verbal é exatamente a transcrição fornecida.

```bash
uv run digest.py recording.mov --transcript transcript.txt \
  --offsets 00:04:37 \
  -o lectures/2026-09-10-algoritmos \
  --title "Algoritmos — Busca binária"
```

`--offsets 00:04:37` significa que o início do vídeo corresponde a `00:04:37` da transcrição. Não há frames para o período anterior. São aceitos vídeos locais que FFmpeg consiga ler, como MOV, MP4, MKV e WebM; a presença das trilhas é verificada, em vez de depender apenas da extensão.

### 2. Vídeo com áudio, sem transcrição pronta

`--transcribe` extrai a primeira trilha de áudio e gera a transcrição localmente. Os frames e a fala usam a mesma linha do tempo.

```bash
uv run --extra audio digest.py aula.mp4 --transcribe \
  --language pt \
  -o lectures/2026-09-10-algoritmos
```

O padrão é português (`pt`). O reconhecimento usa sempre o modelo `small` do faster-whisper, em CPU com quantização int8; não há como trocá-lo pela linha de comando. `--language auto` detecta o idioma por gravação.

A primeira execução baixa os pesos do `small` (~480 MB) para o cache do Hugging Face; o áudio é processado na máquina e não é enviado a uma API. Execuções posteriores reutilizam o cache e dispensam rede. Veja a [documentação oficial do faster-whisper](https://github.com/SYSTRAN/faster-whisper#usage).

O reconhecedor é uma dependência opcional para manter o fluxo com transcrição pronta leve. Usa CPU também em Macs Apple Silicon. Não há diarização nem tradução. A transcrição automática pode errar termos técnicos; confira-os nos frames e slides.

Vídeo sem trilha de áudio, falha de decodificação ou ausência de fala reconhecida resulta em erro, sem publicar um pacote incompleto. Um WAV mono de 16 kHz é temporário e removido ao encerrar o processamento. Pausas e atraso inicial da trilha de áudio são preservados na linha do tempo.

### 3. Áudios sem vídeo

Use `--audio` com `--transcribe` para arquivos que o FFmpeg consiga decodificar, incluindo OGG, M4A, MP3 e WAV. A saída é um pacote apenas com transcrição, sem `frames/`:

```bash
uv run --extra audio digest.py --audio mensagem-1.ogg mensagem-2.ogg \
  --transcribe --language pt -o work/2026-09-09-mensagens
```

Com vários arquivos, forneça um offset para cada início. Eles não podem sobrepor:

```bash
uv run --extra audio digest.py --audio parte-1.ogg parte-2.ogg \
  --transcribe --offsets 00:00:00 00:01:30 -o work/2026-09-09-mensagens
```

### 4. Apenas transcrição pronta

Use quando não houver vídeo:

```bash
uv run digest.py --transcript transcript.txt \
  -o lectures/2026-09-10-algoritmos
```

A saída contém `README.md` e `transcript.md`, sem `frames/`. O README avisa que não há evidência visual temporal.

Para obter esse mesmo tipo de pacote a partir de um vídeo com áudio, acrescente `--no-frames`:

```bash
uv run --extra audio digest.py aula.mp4 --transcribe --no-frames \
  -o lectures/2026-09-10-algoritmos
```

### 5. Slides antes da gravação ou junto das outras fontes

Ingerir material antecipadamente não exige transcrição nem vídeo:

```bash
uv run digest.py --slides slides.pdf -o lectures/2026-09-10-algoritmos
# Também aceita: --slides slides.pptx
```

Depois, finalize no **mesmo diretório**, usando qualquer um dos modos acima. O PDF já ingerido será associado à transcrição. Ou faça tudo em uma execução:

```bash
uv run --extra audio digest.py aula.mp4 --transcribe --slides slides.pptx \
  -o lectures/2026-09-10-algoritmos
```

O PowerPoint é convertido para PDF pelo LibreOffice headless, com perfil temporário próprio e timeout de 5 minutos; `materials/slides.pptx` preserva o original. A conversão permite que busca, renderização e associação usem sempre páginas PDF. Animações e notas do apresentador não são representadas no PDF. Se a conversão falhar, exporte o deck para PDF e forneça esse arquivo. Outros formatos de slides, como `.key`, `.odp` e `.ppt`, não são aceitos.

O LibreOffice permite conversão sem diálogo interativo de acesso a arquivos. A renderização pode diferir da do PowerPoint; confira a versão preservada quando houver dúvida visual.

`materials/slide-links.md` contém candidatos por sobreposição lexical, nunca prova de que uma página estava na tela naquele segundo. Verifique os candidatos no PDF ou nas imagens de páginas. Slides sem texto extraível são marcados; não há OCR.

## Alinhamento e múltiplos vídeos

Cada frame e trecho reconhecido recebe `tempo no vídeo + offset`. O offset é um início absoluto na linha do tempo do pacote, não uma duração nem um deslocamento acumulativo.

Um vídeo sozinho assume offset zero se a flag for omitida. Com dois ou mais, forneça **exatamente um offset por vídeo**:

```bash
uv run digest.py parte-1.mov parte-2.mov --transcript transcript.txt \
  --offsets 00:04:37 00:42:10 -o lectures/minha-aula

uv run --extra audio digest.py parte-1.mp4 parte-2.mp4 --transcribe \
  --offsets 00:00:00 00:42:10 -o lectures/minha-aula-com-audio
```

São aceitos segundos inteiros, `MM:SS` ou `HH:MM:SS`, não negativos e em ordem cronológica. Não há inferência de intervalo entre gravações. No modo de áudio, segmentos que se sobrepõem são recusados para evitar fala duplicada ou fora de ordem; corrija os offsets ou forneça uma transcrição única. Com transcrição pronta, sobreposições visuais são permitidas e recebem nomes de frame sem colisão.

Wispr pode manter uma timeline contínua com lacunas de pausa; os marcadores Paused/Resumed são preservados. Timestamps que voltam no tempo são recusados. Texto sem timestamps é colocado em `00:00:00` e não ganha alinhamento automático; para cruzar fala e frames, forneça texto timestampado. A precisão do pacote é de segundos, e os frames são amostras aproximadas.

## Recorte e tamanho do pacote

O padrão extrai um frame a cada 5 segundos, verifica em cada frame se há uma apresentação do Google Meet, reduz sua maior aresta a 1568 pixels e deduplica por pHash. O hash compara o conteúdo após o recorte. O primeiro frame é preservado, assim como mudanças de distância ≥ 6 ou intervalos ≥ 30 segundos.

O recorte automático exige Tesseract no PATH (`brew install tesseract`). O OCR local lê apenas o cabeçalho da interface: procura o endereço `meet.google.com` e o indicador `Presenting` ou `Apresentando`. Em seguida, usa as faixas escuras da interface para delimitar uma tela compartilhada grande e horizontal. Não usa movimento para escolher a região: webcams podem se mover e slides podem ficar parados. Cada frame é verificado de novo, inclusive depois de trocar de aba ou mudar o layout.

A detecção é conservadora e foi validada em amostras do layout desktop com participantes à direita, com webcams ligadas ou desligadas. Outros vídeos, chamadas sem apresentação identificável, limites ambíguos, falhas de OCR e ausência de Tesseract mantêm o frame inteiro. Endereço oculto, outros idiomas, tema/layout diferente ou telas muito escuras podem impedir o reconhecimento. O programa informa falhas de OCR e quantos frames recortou. O OCR acrescenta trabalho por frame; `--no-crop` o desativa. Use `--crop-box` para um recorte manual. Nenhum desses modos modifica ou comprime o vídeo original; redimensionamento e JPEG continuam aplicáveis aos frames mesmo sem recorte.

```bash
uv run digest.py demo.mp4 -t examples/wispr-transcript.txt \
  --no-crop --max-edge 1280 --frame-quality 82 --interval 5 \
  -o lectures/demo
```

Controles disponíveis em `uv run digest.py --help`:

- `--crop-box left,top,right,bottom`: caixa explícita em pixels da gravação; substitui a detecção.
- `--no-crop`: mantém o frame inteiro; incompatível com `--crop-box`.
- `--max-edge 1568`: limite da maior aresta; `0` mantém a resolução. Não amplia imagens menores.
- `--frame-quality 82`: qualidade JPEG final, de 1 a 95.
- `--interval 5`: segundos entre amostras, maior que zero.
- `--phash-threshold 6`: distância mínima de pHash, de 0 a 64.
- `--max-interval 30`: tempo máximo entre amostras mantidas, limitado à grade de extração.
- `--quality 2`: qualidade JPEG bruta do FFmpeg, de 1 a 31 (menor é melhor).
- `--keep-raw`: preserva intermediários em `frames/raw/` e `frames/cropped/`; por padrão são removidos.

## Saída e execução por agentes

```text
lectures/<data>-<topico>/    # acadêmico; profissional: work/<data>-<topico>/
├── README.md            # fontes, modo, alinhamento e instruções
├── transcript.md        # cabeçalhos ## HH:MM:SS; ausente no modo só slides
├── frames/              # opcional
│   ├── index.csv        # timestamp,file
│   └── HH-MM-SS.jpg
└── materials/           # opcional
    ├── README.md
    ├── slides.pdf
    ├── slides.pptx      # somente se a entrada foi PowerPoint
    ├── slides.md
    ├── slides-index.csv
    ├── pages/           # imagens quando Poppler está disponível
    └── slide-links.md   # após a transcrição
```

Para um agente preparar um pacote: identifique as fontes; se houver transcrição ou vídeo, escolha uma das flags verbais (`--transcript` **ou** `--transcribe`); acrescente `--slides` se houver material; informe os offsets; escolha `-o` explicitamente. Para um pacote só de slides, use `--slides` sem nenhuma flag verbal. `--transcript` e `--transcribe` são mutuamente exclusivos. Vídeos sem uma dessas flags são recusados. Flags de modelo e idioma exigem `--transcribe`.

Diretórios com apenas slides podem ser finalizados ou ter o deck substituído. Um pacote com `transcript.md` ou `frames/` já existente não é sobrescrito: use outro diretório para recompilar. Entradas são lidas sem modificação; etapas de processamento usam área temporária e os arquivos gerados são copiados para o destino depois de concluídas. Erros de gravação na cópia final ainda podem deixar saída parcial.

Para estudar, abra o agente no diretório do pacote e peça que leia `README.md`, `transcript.md` (quando presente) e os frames ou slides pertinentes. O pacote é material de referência; escrever notas ou atualizar um vault depende do pedido do usuário. O compilador não escreve no vault.

## Migração para 0.2

O projeto passa a se chamar **digest**. A interface muda para um único `digest.py`, sem wrappers dos comandos antigos:

- `prepare_lecture.py videos -t texto --offsets ...` → `digest.py videos -t texto --offsets ...`.
- `prepare_transcript.py texto.txt` → `digest.py --transcript texto.txt`.
- `ingest_slides.py deck.pdf` → `digest.py --slides deck.pdf`.
- Os quatro scripts numerados deixam de ser comandos independentes. Normalização fica em `transcript.py`; extração, recorte e deduplicação ficam em `frames.py`.

Todos os modos exigem `-o`. O layout dos pacotes existentes e o diretório `lectures/` permanecem compatíveis. Neste ambiente, o projeto fica em `~/Developer/digest` e `~/Learning/lectures` aponta para `~/Developer/digest/lectures`. Atualize invocações externas dos scripts removidos conforme os exemplos acima.

## Privacidade

`lectures/`, `work/` e extensões comuns de mídia são ignorados pelo Git porque as fontes podem conter falas, nomes, avatares, chats e material de terceiros. Outros diretórios de saída precisam de cuidado próprio. Revise qualquer pacote antes de compartilhar; o recorte pode manter o frame inteiro. A privacidade ao estudar depende também do agente que abrir o pacote.

Use o texto fictício em [`examples/wispr-transcript.txt`](examples/wispr-transcript.txt) para demonstrações, junto de vídeos criados por você.

## Desenvolvimento e verificação

São quatro arquivos de implementação: `digest.py` coordena CLI e pacote; `transcript.py` normaliza texto e transcreve áudio; `frames.py` processa imagens; `slide_materials.py` ingere decks e associa páginas.

```bash
uv run python -m unittest discover tests
```

A suíte gera vídeos sintéticos com FFmpeg e valida modos, offsets, extração real de áudio, atraso de trilha, frames, recorte, slides e falhas sem publicação. O reconhecedor é substituído nos testes determinísticos; eles não baixam modelos nem medem qualidade de ASR. Valide reconhecimento real com um vídeo de fala conhecido e `--transcribe --language en` (ou o idioma correspondente).
