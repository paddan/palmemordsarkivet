# Kom igång

[← Översikt](../README.md) · [Teknisk referens →](teknisk-referens.md)

Den här sidan tar dig från noll till att ställa frågor. För alla flaggor och
detaljer om varje steg, se [Teknisk referens](teknisk-referens.md).

## Krav

- macOS (testat på Darwin 25), Python 3.11+
- [Homebrew](https://brew.sh)
- Minst en stödd LLM-backend: Claude, OpenAI, DeepSeek eller en
  OpenAI-kompatibel lokal tjänst

## 1. Installera

```bash
./install.sh           # installera pipeline/webui (brew, Python-paket, tessdata)
./install.sh --no-surya  # snabbare install utan Surya-OCR
```

`install.sh` sköter pipeline- och webui-beroendena via Homebrew och pip — se
[Vad install.sh gör](teknisk-referens.md#vad-installsh-gör) för detaljer.

## 2. Sätt en API-nyckel

Konfigurera minst en stödd LLM-backend. För molntjänster sätter du dess
API-nyckel:

```bash
# Anthropic (Claude) — Pro/Max-abonnemang (rekommenderas):
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
# eller API-credits:
export ANTHROPIC_API_KEY=sk-ant-...

# OpenAI (GPT-5 / GPT-4o):
export OPENAI_API_KEY=sk-...

# DeepSeek (V4 / Reasoner):
export DEEPSEEK_API_KEY=sk-...
```

OAuth-token genereras med `claude setup-token` (engångsåtgärd). Webgränssnittet
sparar valt backend i `generated/llm_config.json` mellan sessioner — se
[LLM-konfiguration](teknisk-referens.md#llm-konfiguration-generatedllm_configjson).

## 3. Kör pipelinen

Allt i ett kommando:

```bash
./run_pipeline.sh      # kör alla steg i ett (download → OCR → ingest)
./web.sh               # Starta webgränssnittet och ställ frågor
```

Eller steg för steg:

```bash
./download.sh          # 1. Ladda ner alla PDF:er (3 762 st, tar några timmar)
./download_wpu.sh      # 1b. (valfritt) Ladda ner WPU PDF:er (7 155 st)
./ocr.sh               # 2. OCR → text (Tesseract + Surya på svåra sidor, tar flera timmar)
./ingest.sh            # 3. Bygg vektorindex i LanceDB (kan ta flera timmar)
./web.sh               # 4. Starta webgränssnittet och ställ frågor
```

Varje steg är idempotent — avbryt och fortsätt när som helst.
`run_pipeline.sh` kör därför OCR och ingest även när nedladdningssteget inte
hittar nya filer, så väntande arbete från en tidigare avbruten körning slutförs.

## 4. Ställ frågor

```bash
./web.sh   # Starta Streamlit-webgränssnittet
```

Två lägen: **RAG** (snabbt, deterministiskt — bra för faktafrågor) och
**MCP/utredningsläge** (autonomt, bättre täckning på komplexa frågor). Toggla i
sidofältet. Detaljer i [Teknisk referens](teknisk-referens.md#4-ställ-frågor).

## Kunskapsgraf (valfritt)

Utöver vektorsökningen kan arkivet byggas som en kunskapsgraf i Neo4j, med en
interaktiv grafvy i webgränssnittet:

Installera först grafens valfria beroenden. Podman-maskinen behöver minst 4 GiB
minne för Neo4j:

```bash
brew install podman
podman machine init --memory 4096
.venv/bin/pip install -e '.[graph]'
```

```bash
./extract_entities.sh --limit 20   # extrahera entiteter/relationer (provkörning)
./neo4j.sh                         # starta Neo4j via podman
./load_graph.sh                    # ladda grafen
./web.sh                           # öppna "Graf"-sidan i sidofältet
```

Full beskrivning (schema, kostnad, namnnormalisering) finns under
[Kunskapsgraf](teknisk-referens.md#kunskapsgraf-neo4j).
