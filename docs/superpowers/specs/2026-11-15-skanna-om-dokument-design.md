# Skanna om hela dokumentet — design (kort)

**Varför:** I Sidgranskning går det bara att skanna om en sida i taget. När en sida
ser dålig ut är oftast hela dokumentet dåligt läst, och då vill man skanna om alla
sidor med samma motor i ett steg.

**Mål:** en knapp *Skanna om hela dokumentet* i Sidgranskning, och samma val i
CLI/Admin, som skannar om varje sida i dokumentet med vald motor (och profil för
`llm-vision`).

**Icke-mål:** ingen ny OCR-väg, ingen ändring av hur en enskild sida skannas om.

**Anti-mål:** sidor som var bra får inte försämras tyst; ingen omskanning av
raderade dubbletter; `llm-vision` får inte skicka ett helt dokument utan att
användaren sett hur många sidor det gäller.

## Approaches

1. **`rescan-pages` med `--pdf-stem`** (valt av ägaren). Alla sidor i dokumentet
   väljs oavsett poängtröskel och tidigare motor; varje sida behåller den bättre
   läsningen (strikt högre jämförelsepoäng), den andra sparas i historiken.
   Dokumentet OCR:as i en körning.
2. `rescan-page` med "alla sidor": ersätter varje sida ovillkorligt. Förkastad:
   en sämre motor kan försämra bra sidor utan att något stoppar det.

## Beslutat

- Ny parameter `pdf_stem` (`--pdf-stem`) i `rescan-pages`, i registret (CLI och
  Admin genereras därifrån). Med den: sidorna 1…N enligt PDF:ens sidantal;
  `--threshold`, `--source` och regeln "redan läst med motorn" gäller inte;
  `--max-pages` och `--dry-run` gäller som vanligt.
- En raderad dubblett eller saknad PDF ger `OperationFailed` före skrivning.
- Sidgranskning: knappen bredvid *Skanna om sidan*, med samma motor-, profil- och
  omindexeringsval. Med `llm-vision` måste användaren kryssa i en bekräftelse som
  anger antalet sidor innan knappen går att trycka.
- Pågående jobb och slutstatus visas som för en enskild sida.

## Lämnat till byggaren

Etiketter, hjälptexter och debugloggning enligt AGENTS.md.
