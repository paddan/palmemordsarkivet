# palmemordsarkivet

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Skript för att ladda ner, OCR-tolka och söka i materialet på
[palmemordsarkivet.se](https://palmemordsarkivet.se) — ett publikt Google Sheet
med 3 762 PDF-filer (~35 000 sidor) som länkas via "Länk till kopia".

Pipelinen laddar ner arkivet, OCR-tolkar varje sida (Tesseract + Surya på svåra
sidor), indexerar texten i en lokal vektor-databas och ger dig ett
webgränssnitt där du kan ställa frågor om Palme-mordet — med källhänvisningar
tillbaka till original-PDF:erna. Som komplement kan materialet byggas upp som en
kunskapsgraf och utforskas visuellt.

## Web-gränssnitt

Efter nedladdning och OCR-scanning finns ett webgränssnitt (`./web.sh`) med
flikar för **Utredning** (frågor), **Källor** (bläddring i dokument),
**Utredningspärm** (sparade spår), **Graf** (relationer), **Sökverkstad**
(manuell sökgranskning).

### Utredning

#### RAG (standard)

En fast pipeline: frågan
embedas och matchas mot vektorindexet, de bästa utdragen rerankas, och de
6 mest relevanta skickas som kontext till AI som formulerar svaret med
källhänvisningar. Snabbt och förutsägbart — passar enkla faktafrågor där ett
söksteg räcker.

![Web-gränssnitt — RAG-läge](cross-encoder.png)

#### MCP (utredningsläge)

AI söker *autonomt* via
[Model Context Protocol](https://modelcontextprotocol.io). Istället för en
fast pipeline får Claude tillgång till verktyg (`search_archive`, `get_page`)
som den anropar hur många gånger den vill — provar olika söktermer, följer
upp intressanta träffar och läser hela sidor för mer kontext. Bättre täckning
på komplexa flerstegs-frågor, men långsammare (~1–3 min).

![Web-gränssnitt — MCP-läge](utredningsläge.png)

### Källor

Fliken **Källor** låter dig bläddra och söka i OCR-textdokumenten utan att först
ställa en AI-fråga. Varje träff visar en kort textförhandsvisning och samma
PDF/text-/bokmärkningsknappar som källkorten i Utredning och Utredningspärm.

### Utredningspärm

I fliken **Utredningspärm** visas fråga/svar-spår som sparats från både RAG- och
MCP-läget som kollapsade poster med kort rubrik; öppna en post för att läsa
svaret, källorna, modellvalet, grafentiteterna och eventuell egen anteckning.
Källorna visas som klickbara källkort med PDF/text-knappar, och sparade
grafentiteter har en länk som återskapar grafen i Graf-fliken. Källor i svarens
källista kan också bokmärkas separat så att du snabbt hittar tillbaka till
viktiga PDF:er och sidor. Hela pärmen kan laddas ner som Markdown eller JSON
för att dela anteckningar eller fortsätta arbetet utanför Streamlit.

### Sökverkstad

I **Sökverkstad** kan du köra manuella vektor- eller hybridsökningar, slå på och
av reranking, granska utdragen innan något AI-svar formuleras och bokmärka
källor direkt. Den är användbar när du vill testa söktermer, jämföra
retrieval-inställningar eller samla källor till en fråga innan du går vidare
till Utredning-fliken.

### Graf

En **Graf**-sida visualiserar kunskapsgrafen: sök en person, plats eller
organisation och utforska dess nätverk av relationer och källdokument som ett
interaktivt ego-nätverk — dubbelklicka en nod för att fälla ut dess grannskap
eller öppna ett källdokument. Samma graf dyker även upp automatiskt till varje
svar i frågeläget, centrerad kring de entiteter svaret handlar om.

![Web-gränssnitt — Graf](graf.png)

## Dokumentation

- **[Kom igång](docs/kom-igang.md)** — installation, API-nyckel, kör pipelinen
  och ställ din första fråga.
- **[Teknisk referens](docs/teknisk-referens.md)** — alla steg och flaggor i
  detalj: nedladdning, OCR (Tesseract/Surya), indexering, RAG/MCP, state-databasen,
  valfri installation av kunskapsgrafen, LLM-konfiguration, filöversikt och
  tester.

## Licens

Koden är licensierad under [MIT](LICENSE). Materialet i arkivet ägs av sina
respektive upphovsmän och berörs inte av denna licens.
