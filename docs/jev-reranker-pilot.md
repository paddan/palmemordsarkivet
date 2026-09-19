# Jev som reranker: pilot 2026-09-19

Jev gav bättre källurval i detta lilla test, med nästan samma uppmätta svarstid som nuvarande BGE. Resultatet motiverar fortsatt utvärdering, men räcker inte för ett automatiskt byte av standardmodell. Produktionskod, inställningar och index har inte ändrats.

> **Efter piloten:** Ett experimentellt Jev-val har lagts till i RAG-lägets
> sökinställningar. BGE är fortfarande standard. Pilotens resultat och påståendet
> ovan om oförändrad produktionskod beskriver själva mättillfället. Kandidatlistans
> storlek höjdes däremot från 20 till 50 efter en separat mätning — se
> *Uppföljning 2026-09-19* nedan.
>
> Vid manuell användning i RAG-läget har BGE hittills gett bättre urval än Jev.
> Det är en användarerfarenhet från enstaka frågor, inte en mätning, och den
> motsäger därför inte pilotens siffror. Jev-läget behålls som opt-in för att
> kunna testas vidare; BGE förblir standard tills en större granskad jämförelse
> visar något annat.

## Upplägg

- 10 svenska frågor, 20 frysta kandidater per fråga och 6 slutliga utdrag.
- Samma vektorsökning som RAG-läget (`ask.search`, `intfloat/multilingual-e5-large`). Ingen fuzzy-sökning eller facettfiltrering. MCP:s hybridsökning testades inte.
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

Rekommendation: behåll BGE som standard tills vidare. Nästa steg är ett valbart Jev-läge (infört i RAG-läget 2026-09-19) och en större, mänskligt granskad frågesamling med särskilt fokus på korrekt talare, tidpunkt och kandidattäckning.

## Uppföljning 2026-09-19: kandidatlistans storlek

Pilotens slutsats att en reranker inte kan återställa material som första söksteget
missat ledde till en separat mätning på samma tio frågor och samma frysta kandidater.
Bara listans storlek ändrades: `top_k` 20 → 50. Rerankrarna var desamma (lokal BGE
respektive `typesafe/jev-1.13-20260917`), och frågan var om det som ligger på
vektorplats 21–50 är värt att hämta hem.

| Konfiguration | Belägg i topp 6 (av 60) | Korrigerat |
|---|---:|---:|
| Vektorsökning | 27 (45,0 %) | 27 |
| BGE, 20 kandidater | 38 (63,3 %) | 38 |
| BGE, 50 kandidater | 44 (73,3 %) | ~41 |
| Jev, 20 kandidater | 46 (76,7 %) | 46 |
| Jev, 50 kandidater | 55 (91,7 %) | ~51 |

Jev med 50 kandidater var bättre än Jev med 20 på fem frågor och sämre på ingen;
BGE med 50 var bättre på fem och sämre på en. Mårten Palme-frågan, där varken BGE
eller Jev hade ett enda belägg bland de 20, fick 3–4 belägg med 50 kandidater.
Det var alltså listan och inte rerankern som saknade material.

De 23 respektive 24 passager som blev nya i topp 6 bedömdes blint av två nya
bedömare. Båda kalibrerades mot pilotens etiketter på samma 23 stratifierade
passager: exakt samstämmighet 78,3 % respektive 73,9 %, linjärt viktad kappa 0,946
respektive 0,935, medelförskjutning +0,04 respektive 0,00, och ingen av dem
nedgraderade något av pilotens nio belägg. I båda fallen kom 18 % av deras belägg
från passager piloten kallat 0 eller 1, vilket är korrigeringsfaktorn bakom kolumnen
ovan. Deras höga beläggandel på det nya materialet förklaras av urvalet: de nya
passagerna är de en reranker valt till en topp 6, inte ett slumpurval ur alla 20.

Två följder:

- **`top_k` höjt till 50** i `src/Utredning.py` (`rag_top_k`). Kandidaterna kostar
  inga tokens — `top_n` styr fortfarande vad som skickas till modellen. Lokal BGE
  rangordnade 50 kandidater på 1,6–2,1 s per fråga i den här körningen, på en maskin
  där cross-encodern kör på `mps:0`; samma 50 par tog 0,65 s när de kördes utan en
  samtidig vektorsökning, eftersom encodern och embedding-modellen delar GPU. Siffran
  är alltså maskinberoende och säger inget om CPU-only-hårdvara, där kostnaden skalar
  med antalet par. Jevs merkostnad är däremot oberoende av maskin: 50 kandidater tog
  ~5,0 s per fråga mot 2,0 s för 20, eftersom anropen är nätverksbundna (fyra
  samtidiga, 0,0014 USD per fråga).
  MCP-verktyget `search_archive` har kvar sin egen standard på 20
  (`TOP_K_DEFAULT` i `src/rag/mcp_server.py`). Mätningen gällde RAG-vägen, och i
  MCP-läget gör modellen flera sökningar per fråga, så den vägen ska mätas separat
  innan den ändras.
- **`top_n` lämnad på 6.** Pilotens etiketter visar hur bra materialet är per
  rangplats: för Jev är plats 1–6 belägg i 76,7 % av fallen, plats 7–10 i 50,0 %,
  plats 11–15 i 36,0 % och plats 16–20 i 12,0 %. Tio vore alltså det enda försvarbara
  steget om fler utdrag ska skickas, och det fördubblar utdragsdelen av kontexten
  (~990 → ~1 740 token per fråga). Ingen mätning visar att slutsvaren blir bättre av
  det, så standarden står kvar.

Begränsningar: tio egenvalda frågor, en körning per fråga, agentbedömningar och inte
mänskligt validerat facit — samma förbehåll som piloten. 33 av Jev@50:s 55 belägg
vilar på en annan bedömare än pilotens. Slutsvar genererades fortfarande inte, så
mätningen gäller källurval.

## Reproducerbart underlag

Underlaget ligger lokalt i `generated/experiments/jev-2026-09-19/` (gitignorerad katalog,
alltså inte med i repot). Detta dokument är kopian som versionshanteras.

- `candidates.json`: frysta sökträffar med exakt källfil, sida, text och stabilt passage-id.
- `blind-*.json`, `labels-*.json`: slumpad presentationsordning respektive bedömningar och motiveringar.
- `bge.json`, `jev.json`: fulla rangordningar, tider, Jev-modellversion och leverantörens kostnadsuppgifter.
- `summary.json`: sammanställda mått per fråga och totalt.
- `prepare.py`, `run_jev.py`, `evaluate.py`: tillfälliga experimentskript, inte produktionsentrypoints. `prepare.py` återskapar och skriver över sökunderlaget; `run_jev.py` gör nya externa anrop och kostar krediter. `evaluate.py` räknar om resultaten lokalt utan API-anrop.
- `recall.py`, `topk50.py`, `compare50.py`, `compare_all.py`, `jev50.py`: uppföljningens skript. `recall.py` och `compare_all.py` räknar bara om befintliga etiketter lokalt; `topk50.py` gör en ny lokal sökning och rangordning (inget API); `jev50.py` gör 500 externa anrop och kostade 0,0141 USD.
- `topk50.json`, `jev50.json`, `jev50-rapport.json`, `topk50-nytt-att-bedoma.json`, `jev50-nytt-att-bedoma.json`, `kalibrering-att-bedoma.json`, `labels-topk50-nytt.json`, `labels-jev50-nytt.json`, `labels-kalibrering.json`, `labels-kalibrering-c.json`: uppföljningens underlag och bedömningar.

SHA-256 för fryst candidates.json: `a3875b1df605657ef021bf20303d0bbe3414f9643476572a298aa5144a3f09fa`.
