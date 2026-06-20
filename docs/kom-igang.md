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
./install.sh           # installera pipeline/webgränssnitt (brew, Python-paket, tessdata)
./install.sh --no-surya  # snabbare install utan Surya-OCR
```

`install.sh` sköter pipeline- och webgränssnittsberoendena via Homebrew och pip — se
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

Fliken **Utredning** har två lägen: **RAG** (snabbt, deterministiskt — bra för
faktafrågor) och **MCP/utredningsläge** (autonomt, bättre täckning på komplexa
frågor). Toggla i sidofältet. Detaljer i
[Teknisk referens](teknisk-referens.md#4-ställ-frågor).

När du hittar något intressant kan du spara svaret i fliken **Utredningspärm**.
Sparade spår visas som kollapsade poster med kort rubrik; öppna posten för att
se frågan, svaret, källkort med PDF/text-knappar, modellvalet och eventuella
grafentiteter. Grafentiteterna kan öppnas som en länk som återskapar grafen i
Graf-fliken. Du kan också bokmärka enskilda källor från källistan för att
snabbt komma tillbaka till samma PDF eller sida senare, och skriva fria
**anteckningar** på en källa via ✏️-rutan på källkorten (samlas på en egen flik
i Utredningspärmen).

I Utredning-flikens sidofält finns två extra sökfilter för RAG-läget:
**facetter** (begränsa träffarna till dokument som nämner en viss person/plats/
organisation ur kunskapsgrafen) och **OCR-tolerant fuzzy-sökning** (fångar
söktermer som OCR:en felstavat, t.ex. *Engstrcm* för *Engström* — första
körningen bygger ett index och tar en stund). Med likhetströskeln (sliden under
toggeln) avgör du hur tolerant matchningen är: sänk den mot ~0.6 för korta namn
med ett OCR-fel som *Palme*→*Paine*, höj den för mindre brus. Två fristående
flikar hjälper dig dessutom
att gräva: **Maskeringar** visar var arkivet svärtat över text, och
**Jämförelse** ställer flera källor mot varandra och lyfter fram motstridiga
uppgifter.

## Kunskapsgraf (valfritt)

Utöver vektorsökningen kan arkivet byggas som en kunskapsgraf i Neo4j, med en
interaktiv grafvy i webgränssnittet.

### 1. Installera grafberoenden

Installera först grafens valfria beroenden. Podman-maskinen behöver minst 4 GiB
minne för Neo4j:

```bash
brew install podman
podman machine init --memory 4096
.venv/bin/pip install -e '.[graph]'
```

### 2. Välj LLM för extraktionen

`extract_entities.sh` skickar OCR-texten sida för sida till en LLM för att hitta
personer, platser, organisationer och relationer. Det kan därför ta tid och
kosta API-tokens. Skriptet använder normalt samma sparade LLM-val som
webgränssnittet och `llm_correct.sh`.

Kör `./llm_config.sh` utan argument i terminalen för en **interaktiv meny** där
du väljer backend och modell ur samma lista som webgränssnittet (Claude / OpenAI /
DeepSeek / Ollama / OpenAI-kompatibel). Vill du hellre sätta värdena direkt går
det med flaggor:

```bash
./llm_config.sh                                  # interaktiv meny: välj backend + modell

# Eller sätt direkt (sparas och används av framtida körningar och webgränssnittet)
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...  # eller ANTHROPIC_API_KEY
./llm_config.sh --provider claude --model claude-haiku-4-5-20251001

# Alternativt OpenAI
export OPENAI_API_KEY=sk-...
./llm_config.sh --provider openai --model gpt-4o-mini
```

En dyr modell som Opus kan bli kostsam för hela arkivet. Välj därför gärna en
billigare modell uttryckligen innan extraktionen.

Du kan också skriva över det sparade valet för bara en körning:

```bash
./extract_entities.sh --limit 20 \
  --provider claude --model claude-haiku-4-5-20251001
```

### 3. Extrahera entiteter och relationer

Extraktionen skriver resultatet till `generated/db/state.db` och kräver inte att
Neo4j är igång. Börja med en kostnadsfri dry-run och en begränsad provkörning:

```bash
./extract_entities.sh --dry-run    # visa hur många sidor som återstår, utan LLM-anrop
./extract_entities.sh --limit 20   # provkör på 20 dokument
./extract_entities.sh              # extrahera resten av arkivet
```

### 4. Starta, använd och stoppa Neo4j

```bash
./neo4j.sh          # starta Neo4j; skapar container och lösenord första gången
./neo4j.sh status   # kontrollera om Neo4j kör
./load_graph.sh     # ladda extraherade entiteter/relationer från state.db
./web.sh            # öppna "Graf"-sidan i sidofältet

./neo4j.sh stop     # stoppa Neo4j
./neo4j.sh          # starta samma container igen senare
```

Full beskrivning (schema, kostnad, namnnormalisering) finns under
[Kunskapsgraf](teknisk-referens.md#kunskapsgraf-neo4j).
