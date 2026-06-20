# Karta seed-data

`platser.json` är en platskatalog för snabbval i kartfliken.

`rorelser.json` är en lista med observationer. Lägg bara in observationer som har
verifierad källa i arkivet. Varje observation ska ha:

- `person`: visningsnamn
- `place_name`: platsnamn som visas i popupen
- `lat`, `lon`: koordinater i WGS84
- `time`: `HH:MM` under mordkvällen
- `uncertainty`: fri text, till exempel `ca` eller `±10 min`
- `nr`: arkivnummer
- `sida`: sida i källan
- `note`: kort neutral notering

Exempel:

```json
{
  "person": "Olof Palme",
  "place_name": "Grand",
  "lat": 59.34057,
  "lon": 18.06024,
  "time": "21:15",
  "uncertainty": "ca",
  "nr": "2055",
  "sida": 1,
  "note": "Exempelrad; ersätt med verifierad källrad innan den seedas."
}
```

Seedade observationer börjar tomma för att kartan inte ska påstå positioner utan
granskad källhänvisning. Lägg till riktiga observationer via UI:t eller genom en
granskad ändring av `rorelser.json`.
