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
.venv/bin/python scripts/install.py           # installera pipeline/webgränssnitt (brew, Python-paket, tessdata)
.venv/bin/python scripts/install.py --no-surya  # snabbare install utan Surya-OCR
.venv/bin/python scripts/install.py --dev     # valfritt: installera pytest/ruff/mypy för utveckling
```

`scripts/install.py` sköter pipeline- och webgränssnittsberoendena via Homebrew och pip — se
[Vad install.py gör](teknisk-referens.md#vad-installpy-gör) för detaljer.
Kör `.venv/bin/python scripts/test.py` för pytest; lägg till `--static` när du vill köra ruff och mypy.

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
.venv/bin/python scripts/run_pipeline.py      # kör alla steg i ett (download → OCR → ingest)
.venv/bin/python scripts/web.py               # Starta webgränssnittet och ställ frågor
```

Eller steg för steg:

```bash
.venv/bin/python scripts/download.py          # 1. Ladda ner alla PDF:er (3 762 st, tar några timmar)
.venv/bin/python scripts/download_wpu.py      # 1b. (valfritt) Ladda ner WPU PDF:er (7 155 st)
.venv/bin/python scripts/ocr.py               # 2. OCR → text (Tesseract + Surya-fallback/svåra sidor, tar flera timmar)
.venv/bin/python scripts/ingest.py            # 3. Bygg vektorindex i LanceDB (kan ta flera timmar)
.venv/bin/python scripts/web.py               # 4. Starta webgränssnittet och ställ frågor
```

Varje steg är idempotent — avbryt och fortsätt när som helst.
`scripts/run_pipeline.py` kör därför OCR och ingest även när nedladdningssteget inte
hittar nya filer, så väntande arbete från en tidigare avbruten körning slutförs.

## 4. Ställ frågor

```bash
.venv/bin/python scripts/web.py   # Starta Streamlit-webgränssnittet
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

I Utredning-flikens sidofält finns två extra sökfilter för RAG-läget
(de döljs när **Utredningsläge (MCP)** är aktivt):
**facetter** (begränsa träffarna till dokument som nämner en viss person/plats/
organisation ur kunskapsgrafen) och **OCR-tolerant fuzzy-sökning** (fångar
söktermer som OCR:en felstavat, t.ex. *Engstrcm* för *Engström* — första
körningen bygger ett index och tar en stund). Med likhetströskeln (sliden under
toggeln) avgör du hur tolerant matchningen är: sänk den mot ~0.6 för korta namn
med ett OCR-fel som *Palme*→*Paine*, höj den för mindre brus. Två fristående
flikar hjälper dig dessutom
att gräva: **Maskeringar** listar svärtad text i en tabell och visar detaljer
för klickad dokumentrad, och
**Jämförelse** ställer flera källor mot varandra och lyfter fram motstridiga
uppgifter. Referenserna i jämförelsesvaret är klickbara och öppnar matchande
PDF i en ny webbläsarflik, på refererad sida när sidnumret är känt.

### Karta

Karta-fliken finns i Streamlit-appen och visar observationer runt mordkvällen på
en folium-karta. Varje observation syns som en markör (färg per person); slå på
**Animera tidslinje** för att spela upp dem i tidsordning. Nya observationer ska
ha person, plats/koordinat och källhänvisning (`Nr` + `sida`); tid behövs för att
observationen ska komma med i tidslinjen. Källan väljs genom att **söka** fram
dokumentet (nr eller titel) och välja ur listan — du behöver inte kunna numret
utantill. Du kan **flytta** en observation direkt
på kartan: klicka på dess markör för att välja den, slå på **Flytta-läge** och
klicka där den ska ligga — och sätt klockslag i snabbfältet intill kartan. Kartan
seedar platskatalogen från `data/karta/platser.json`; `data/karta/rorelser.json`
börjar tom tills verifierade observationer läggs till.

För att leta fram kandidater ur OCR-texten utan att publicera dem direkt:

```bash
.venv/bin/python scripts/extract_map_observations.py --dry-run --limit 20
.venv/bin/python scripts/extract_map_observations.py --limit 20
```

Öppna sedan Karta-fliken och granska förslagen under **Granska extraherade
kartförslag**. En kandidat måste ha person, koordinat, tid och källa innan den
kan godkännas till tidslinjen.

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

`scripts/extract_entities.py` skickar OCR-texten sida för sida till en LLM för att hitta
personer, platser, organisationer och relationer. Det kan därför ta tid och
kosta API-tokens. Skriptet använder normalt samma standardprofil som
webgränssnittet och `scripts/llm_correct.py`. Namngivna profiler hanteras i
**Admin → Inställningar → LLM-inställningar**; där sparas endast namnet på
eventuell API-nyckelvariabel, aldrig själva nyckeln.

Kör `.venv/bin/python scripts/llm_config.py` utan argument i terminalen för en **interaktiv meny** där
du väljer backend och modell ur samma lista som Admin (Claude / OpenAI /
DeepSeek / Ollama / OpenAI-kompatibel). Vill du hellre sätta värdena direkt går
det med flaggor:

```bash
.venv/bin/python scripts/llm_config.py                                  # interaktiv meny: välj backend + modell

# Eller sätt direkt (sparas och används av framtida körningar och webgränssnittet)
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...  # eller ANTHROPIC_API_KEY
.venv/bin/python scripts/llm_config.py --provider claude --model claude-haiku-4-5-20251001

# Alternativt OpenAI
export OPENAI_API_KEY=sk-...
.venv/bin/python scripts/llm_config.py --provider openai --model gpt-4o-mini
```

En dyr modell som Opus kan bli kostsam för hela arkivet. Välj därför gärna en
billigare modell uttryckligen innan extraktionen.

Du kan också skriva över det sparade valet för bara en körning:

```bash
.venv/bin/python scripts/extract_entities.py --limit 20 \
  --provider claude --model claude-haiku-4-5-20251001
```

### 3. Extrahera entiteter och relationer

Extraktionen skriver resultatet till `generated/db/state.db` och kräver inte att
Neo4j är igång. Börja med en kostnadsfri dry-run och en begränsad provkörning:

```bash
.venv/bin/python scripts/extract_entities.py --dry-run    # visa hur många sidor som återstår, utan LLM-anrop
.venv/bin/python scripts/extract_entities.py --limit 20   # provkör på 20 dokument
.venv/bin/python scripts/extract_entities.py              # extrahera resten av arkivet
```

### 4. Starta, använd och stoppa Neo4j

```bash
.venv/bin/python scripts/neo4j.py          # starta Neo4j; skapar container och lösenord första gången
.venv/bin/python scripts/neo4j.py status   # kontrollera om Neo4j kör
.venv/bin/python scripts/load_graph.py     # ladda extraherade entiteter/relationer från state.db
.venv/bin/python scripts/web.py            # öppna "Graf"-sidan i sidofältet

.venv/bin/python scripts/neo4j.py stop     # stoppa Neo4j
.venv/bin/python scripts/neo4j.py          # starta samma container igen senare
```

Full beskrivning (schema, kostnad, namnnormalisering) finns under
[Kunskapsgraf](teknisk-referens.md#kunskapsgraf-neo4j).
