# Lecture Digest — Context Compiler (V0)

Compila gravações de tela (`recording.mp4`) e transcrições de aula (`transcript.txt` / `.srt` / `.vtt` / `.json`) em um **pacote de contexto de aula estruturado e legível por agentes de IA**, sem intermediários, OCR desnecessário ou bancos vetoriais pesados.

---

## 🎯 Arquitetura V0

```text
screen recording + transcript
            ↓
     preparação local
       (recorte + escala)
            ↓
    lecture-context/
            ↓
     agente abre pasta
            ↓
   nota no vault + wiki
```

### Estrutura do Pacote Gerado

```text
lectures/2026-08-25-nome-da-disciplina/
├── README.md           # Guia de alinhamento e instruções para o agente multimodal
├── transcript.md       # Transcrição normalizada com cabeçalhos ## HH:MM:SS
├── frames/
│   ├── index.csv       # Mapeamento rápido (timestamp, file)
│   ├── 00-04-37.jpg    # Recortados na tela compartilhada, com o timestamp real da aula
│   ├── 00-04-52.jpg
│   └── ...
└── (nada mais — o vault é o destino)
```

---

## 🚀 Como Usar

### Pré-requisitos
- Python 3.10+
- `ffmpeg` instalado no PATH (`brew install ffmpeg`)
- Dependências Python: `pillow`, `imagehash`, `numpy` (gerenciadas via `uv` ou `pip`)

```bash
uv sync
```

---

### Opção 1: Pipeline Completo em 1 Comando

```bash
uv run prepare_lecture.py recording.mp4 -t transcript.txt \
  --offsets 00:04:37 \
  -o lectures/2026-08-25-algoritmos \
  --title "Algoritmos e Estruturas de Dados - Aula 01"
```

Flags que controlam o tamanho do pacote:

| Flag | Padrão | O que faz |
|---|---|---|
| `--max-edge` | `1568` | Maior aresta do frame final, em pixels. `0` desliga a redução. |
| `--frame-quality` | `82` | Qualidade JPEG do frame final (1–95). |
| `--crop-box` | — | `left,top,right,bottom`: pula a detecção e recorta nessa caixa. |
| `--no-crop` | — | Mantém o frame inteiro; só reduz a escala. |
| `--keep-raw` | — | Preserva os frames em resolução original, que por padrão são apagados assim que o recorte termina. |

---

### Opção 2: Executar Passo a Passo Modular

#### 1. Normalizar Transcrição
Converte `.txt`, `.srt`, `.vtt` ou Whisper `.json` para `transcript.md`:
```bash
uv run 01_normalize_transcript.py transcript.txt -o lectures/minha-aula/transcript.md
```

#### 2. Extrair Frames Brutos da Gravação (1 a cada 5s)
Extrai frames brutos usando `ffmpeg`:
```bash
uv run 02_extract_frames.py recording.mp4 -o lectures/minha-aula/frames/raw --interval 5
```

#### 3. Recortar a Tela Compartilhada e Reduzir a Resolução
A aula é gravada dentro de uma chamada, então a maior parte de cada frame é moldura: barra do navegador, ladrilhos de participantes, controles do Meet, tarja preta. Quase nada disso fica realmente parado — o relógio anda, a barra de controles aparece e some, as webcams mexem. A pergunta útil não é *o que muda*, e sim **o que se reescreve o tempo todo**.

O passo amostra os frames de cada gravação, mede quanto cada pixel muda **de uma amostra para a seguinte** e fica com a região que muda tão forte quanto as células mais agitadas do frame — a tela compartilhada, que redesenha linhas inteiras de texto enquanto um ladrilho desloca um rosto por alguns níveis. Depois reduz a escala até a maior aresta caber em `--max-edge`.

```bash
uv run 03_crop_frames.py lectures/minha-aula/frames/raw \
  -o lectures/minha-aula/frames/cropped \
  --max-edge 1568 \
  -q 82
```

O padrão de `--max-edge` é **1568 px** porque é a resolução acima da qual modelos de visão reduzem a imagem por conta própria — guardar mais que isso ocupa disco sem entregar detalhe nenhum ao agente. Em tela Retina isso sozinho corta a maior parte do peso.

A detecção é feita **por gravação**, já que cada uma pode ter um layout diferente. Se ela errar, `--box left,top,right,bottom` recorta na marra e `--no-crop` mantém o frame inteiro. Quando a detecção não confia no resultado (poucos frames, frames parados, região implausível), ela mantém o frame inteiro e diz isso na saída.

**Diferença entre amostras consecutivas, não desvio-padrão sobre a gravação toda.** Os dois são altos na tela compartilhada, mas só o desvio-padrão é alto em algo que ficou em dois estados ao longo de uma hora. Basta o professor entrar em tela cheia uma vez aos 45 minutos para *todo* pixel do frame — tarja preta inclusive — passar a ter desvio-padrão alto, e aí a região detectada engole a chamada inteira. Foi exatamente o que aconteceu na aula de 25/08. A diferença consecutiva cobra de cada célula a **frequência** com que ela muda, e esse evento único se dilui entre as 23 transições amostradas.

Medido na aula de 25/08, nos parâmetros de produção: tela compartilhada ~106 níveis de mudança entre amostras, ladrilhos de participantes ~29, tarja preta ~22, pico ~147. O corte fica em 30% do pico (~44), que é o vão entre o primeiro valor e todos os outros.

**A detecção precisa da gravação inteira.** Em um recorte de poucos minutos a separação se desfaz, porque a página fica parada e as webcams não. Quando a região detectada não concentra pelo menos 60% da mudança do frame, a detecção assume que travou em algo que se mexe *ao lado* da aula — a grade de ladrilhos, provavelmente — e mantém o frame inteiro.

**A caixa é uma só por gravação, então ela é a união.** Se o professor mudar o layout da chamada no meio da aula — ladrilhos da lateral para o topo, painel de chat abrindo —, a região que carrega a aula ocupa posições diferentes ao longo do tempo, e a caixa cobre todas elas. O resultado fica grande, e está certo: apertar a caixa passaria a cortar conteúdo em parte da aula.

O passo avisa quando o resultado cobre mais de 70% do frame, porque as duas situações acima produzem essa assinatura e pedem respostas opostas. Antes de forçar `--box`, olhe um frame do começo e um do fim. Em qualquer dos casos a falha é para o lado seguro: sobra moldura, nunca falta conteúdo.

Medido nas aulas reais deste repositório: **80% a 83% menos disco**, sem perder legibilidade do código na tela.

#### 4. Deduplicar por Perceptual Hash e Renomear pelo Offset
Deduplica frames redundantes (pHash distance >= 6 ou intervalo >= 30s) e renomeia pelo timestamp da aula. Roda **depois** do recorte, de propósito: assim o hash compara conteúdo de aula, e não o relógio do Meet ou o ladrilho de quem mexeu na webcam.
```bash
uv run 04_dedupe_and_rename.py lectures/minha-aula/frames/cropped \
  -o lectures/minha-aula/frames \
  --offsets 00:04:37 \
  --interval 5 \
  --phash-threshold 6 \
  --max-interval 30 \
  --clean-raw
```

---

## 🤖 Workflow com o Agente de IA

Abra o agente multimodal (Antigravity, Claude, Codex, etc.) no diretório da aula:

```bash
cd lectures/2026-08-25-algoritmos
```

E faça seus pedidos diretamente:

> *"Read README.md and transcript.md. Study the lecture and inspect the relevant frames in frames/ when visual context is necessary."*

O destino da nota é o vault, não este diretório — ver `AGENTS.md`.

> *"At 37:20 the professor discusses an edge case with 'essa linha aqui'. Explain what they mean, inspecting nearby frames."*

---

## 🧪 Testes

Execute a suíte de testes com vídeo sintético e transcrições:
```bash
uv run python3 -m unittest discover tests
```
