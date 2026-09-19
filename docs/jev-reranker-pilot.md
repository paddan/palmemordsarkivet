# Jev som reranker: pilot 2026-09-19

Jev gav bättre källurval i detta lilla test, med nästan samma uppmätta svarstid som nuvarande BGE. Resultatet motiverar fortsatt utvärdering, men räcker inte för ett automatiskt byte av standardmodell. Produktionskod, inställningar och index har inte ändrats.

> **Efter piloten:** Ett experimentellt Jev-val har lagts till i RAG-flikens
> sökinställningar. BGE är fortfarande standard. Pilotens resultat och påståendet
> ovan om oförändrad produktionskod beskriver själva mättillfället.
>
> Vid manuell användning i RAG-fliken har BGE hittills gett bättre urval än Jev.
> Det är en användarerfarenhet från enstaka frågor, inte en mätning, och den
> motsäger därför inte pilotens siffror. Jev-läget behålls som opt-in för att
> kunna testas vidare; BGE förblir standard tills en större granskad jämförelse
> visar något annat.

## Upplägg

- 10 svenska frågor, 20 frysta kandidater per fråga och 6 slutliga utdrag.
- Samma vektorsökning som RAG-fliken (`ask.search`, `intfloat/multilingual-e5-large`). Ingen fuzzy-sökning eller facettfiltrering. MCP:s hybridsökning testades inte.
- Samma textutdrag gavs till lokal `BAAI/bge-reranker-v2-m3` och OpenRouter `typesafe/jev-1.13`. Jev svarade med `typesafe/jev-1.13-20260917` via `/api/alpha/decisions`.
- Jev bedömde varje fråga–utdrag-par med en fast Noul-fråga; högst fyra anrop samtidigt. Instruktionerna var på engelska, arkivfrågor och utdrag på svenska. Inga dokumenttitlar gavs till någondera rerankern.
- Tre subagenter gjorde blinda relevansbedömningar utan att se modellernas poäng eller ordning. 0 = fel/irrelevant, 1 = relevant bakgrund, 2 = konkret belägg som besvarar minst en del av den exakta frågan. Sammanfattningar och motstridiga vittnesmål kunde få 2; sanningshalten i vittnesmålen bedömdes inte.
- Frågorna om Mårten Palme och Anna Hage granskades en andra gång av en annan agent, fortfarande utan modellrankningar. Inga etiketter ändrades. Bedömningarna är preliminära agentbedömningar, inte ett mänskligt validerat facit.

## Resultat

| Mått | Vektorsökning | BGE | Jev |
|---|---:|---:|---:|
| Direkt relevanta utdrag bland topp 6 | 45.0% | 63.3% | 76.7% |
| nDCG@6 (högre är bättre) | 0.671 | 0.854 | 0.910 |
| Relevanta källsidor bland topp 6, medel | 2.7 | 3.7 | 4.5 |

BGE valde 38 av 60 direkt relevanta utdrag; Jev 46 av 60. Medeltid för enbart reranking: BGE 2.07 s/fråga, Jev 2.00 s/fråga. BGE laddades på 0.35 s och fick ett separat uppvärmningsanrop före mätningen. En körning per fråga: skillnaden i tid är för liten för att hävda en säker hastighetsvinst.

De 200 Jev-anropen använde 133 211 indatatoken och rapporterade totalt 0,005594862 USD. Det separata syntetiska anslutningstestet kostade 0,000014196 USD. Total rapporterad API-kostnad inklusive detta test: 0,005609058 USD. Lokal BGE har ingen API-avgift; lokal energi/hårdvara värderades inte.

## Per fråga

| Fråga | BGE relevanta /6 | Jev relevanta /6 | BGE nDCG@6 | Jev nDCG@6 |
|---|---:|---:|---:|---:|
| Vad uppgav Lisbeth Palme om gärningsmannens klädsel? | 5 | 6 | 0.928 | 1.000 |
| Vad uppgav Lisbeth Palme om gärningsmannens ansikte? | 4 | 5 | 0.827 | 0.928 |
| Vilken flyktväg beskrev Lars Jeppsson? | 5 | 6 | 0.899 | 1.000 |
| Vad uppgav Yvonne Nieminen om mannen hon mötte på David Bagares gata? | 2 | 5 | 0.662 | 0.928 |
| Vilka tider uppgav Stig Engström för att lämna Skandia? | 6 | 5 | 1.000 | 0.928 |
| Vad såg Mårten Palme utanför biografen Grand efter föreställningen? | 0 | 0 | 0.658 | 0.506 |
| Vad berättade Anders Björkman om personerna framför honom på Sveavägen? | 6 | 6 | 1.000 | 1.000 |
| Vad berättade Anna Hage om återupplivningsförsöken på mordplatsen? | 2 | 1 | 0.939 | 0.808 |
| Vad uppgav vittnen om skottens antal och tidsavstånd? | 6 | 6 | 1.000 | 1.000 |
| Vilka uppgifter finns om walkie-talkie-observationer på Sveavägen under mordkvällen? | 2 | 6 | 0.622 | 1.000 |

Jev förbättrade nDCG@6 för fem frågor, försämrade tre och var lika på två.

## Vad skillnaden innebär

- Yvonne Nieminen: Jev lyfte konkreta iakttagelser och jämförelser framför flera utdrag med utredares slutsatser om mannens identitet.
- Walkie-talkies: Jev lyfte specifika observationer med plats och tid framför flera övergripande diskussioner.
- Stig Engström: Jev tog med en vakts tidsuppskattning på bekostnad av ett utdrag ur Engströms förhör, trots att frågan gällde hans egna uppgifter.
- Anna Hage: Jev missade ett direkt utdrag ur hennes berättelse och föredrog bland annat en annan persons beskrivning av insatsen.
- Mårten Palme: inget av de 20 sökutdragen innehöll en direkt relevant redogörelse efter föreställningen enligt blindbedömningen. En annan reranker kan inte återställa material som första söksteget missat.

## Begränsningar och rekommendation

Detta är ett utforskande pilotprov med tio egenvalda frågor, inga statistiskt säkerställda förbättringar. Utdragen innehåller överlapp och samma handling kan förekomma från båda arkivkällorna; antalet relevanta källsidor räknar fil och sida, inte självständiga vittnesmål. Jev-poäng kan ha lika värden; sorteringen behåller då ursprunglig sökordning. nDCG ger viss kredit åt bakgrund (etikett 1), medan precisionen endast räknar direkta belägg (etikett 2). Recall i summary.json gäller endast de 20 kandidaterna, inte hela arkivet; frågan utan direkta belägg utelämnas ur recall-medelvärdet.

Slutliga LLM-svar genererades inte. Testet mäter källurval, inte svarens faktiska korrekthet, totala RAG-svarstid eller slutmodellens tokenbesparing. Jev kräver nätverk och ett separat besluts-API som för närvarande ligger under alpha.

Rekommendation: behåll BGE som standard tills vidare. Nästa steg är ett valbart Jev-läge (infört i RAG-fliken 2026-09-19) och en större, mänskligt granskad frågesamling med särskilt fokus på korrekt talare, tidpunkt och kandidattäckning.

## Reproducerbart underlag

Underlaget ligger lokalt i `generated/experiments/jev-2026-09-19/` (gitignorerad katalog,
alltså inte med i repot). Detta dokument är kopian som versionshanteras.

- `candidates.json`: frysta sökträffar med exakt källfil, sida, text och stabilt passage-id.
- `blind-*.json`, `labels-*.json`: slumpad presentationsordning respektive bedömningar och motiveringar.
- `bge.json`, `jev.json`: fulla rangordningar, tider, Jev-modellversion och leverantörens kostnadsuppgifter.
- `summary.json`: sammanställda mått per fråga och totalt.
- `prepare.py`, `run_jev.py`, `evaluate.py`: tillfälliga experimentskript, inte produktionsentrypoints. `prepare.py` återskapar och skriver över sökunderlaget; `run_jev.py` gör nya externa anrop och kostar krediter. `evaluate.py` räknar om resultaten lokalt utan API-anrop.

SHA-256 för fryst candidates.json: `a3875b1df605657ef021bf20303d0bbe3414f9643476572a298aa5144a3f09fa`.
