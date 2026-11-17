---
status: reference
language: da
issues: [161, 158]
adopted-by: track-facts-data-model.md
original: /mnt/project-files/traktor/spillehistorik-design.md (projektfiler, 2026-10-11)
---

> **Status:** designforslag fra 2026-10-11. Datamodellen i [track-facts-data-model.md](track-facts-data-model.md) beholder `playhistory.db` "som i Traktor-designet" og er godkendt; selve Traktor-dokumentet har ikke fået en separat godkendelse. #161 bygger videre på det. Felt-navne i `EXTENDEDDATA` skal stadig verificeres på rigtige filer.
> Indeks: [README.md](README.md).

# Spillehistorik fra Traktor: designforslag

*2026-10-11T23:30 · til FasterMadman*

## Kort fortalt

"Mest spillede numre" findes allerede i beetsGUI (fanen **Recover**, bygget i #42). Den læser alle dine gamle `collection.nml`, `history_*.nml` og `.m3u`, slår numre sammen på tværs af snapshots og viser en rangeret liste over hvad du mangler i biblioteket.

Det der mangler for din drøm, er **tidspunkterne**. Hver linje i en Traktor-historikfil er én afspilning med dato, klokkeslæt, deck og varighed, men koden bruger i dag kun filnavnet ("nummeret optræder i N historikfiler") og smider resten væk. Det er det første og vigtigste der skal bygges, og det kræver ingen AI.

## Hvad dataene faktisk indeholder

| Kilde | Hvad den ved | Hvad den ikke ved |
|---|---|---|
| `collection.nml` (`INFO/@PLAYCOUNT`, `LAST_PLAYED`) | Samlet antal afspilninger pr. nummer, seneste dato | Hvornår de andre afspilninger var. Tæller også hjemmeøvning og preview |
| `History/history_ÅÅÅÅyMMmDDd_HHhMMmSSs.nml` | Én fil pr. Traktor-session. Hver afspilning har `EXTENDEDDATA`: `STARTDATE`, `STARTTIME` (sekunder efter midnat), `DECK`, `DURATION`, `PLAYEDPUBLIC` (hørt på master, ikke kun i hovedtelefoner) | Hvor du var. Om det var en gig eller øvning |
| `.m3u` | At nummeret var med i en playliste | Alt andet |

Dvs. **hvad** og **hvornår (inkl. tid på dagen og rækkefølge)** ligger allerede i dine filer. **Hvor** findes ikke i Traktor overhovedet. Det skal komme et andet sted fra.

*(Feltnavnene i `EXTENDEDDATA` er fra Traktors kendte format; jeg verificerer dem mod dine egne filer før noget bygges på dem, ligesom #42 gjorde med `PLAYCOUNT`.)*

## Datamodel

En selvstændig SQLite-database, `playhistory.db`, ved siden af `traktor_recovery.json`. Ingen beets-afhængighed i selve modellen.

- **tracks**: én række pr. nummer, samme nøgle som #42 bruger i dag (`track_key`: artist+titel normaliseret, title-only-rækker markeret). Kobles til beets-biblioteket, men ejes ikke af det.
- **sessions**: én række pr. historikfil. Start, slut, antal numre, hvilken Traktor-installation, og `kind` = gig / øvning / ukendt. Plus valgfri `venue_id`.
- **plays**: én række pr. afspilning. `session_id`, `track_id`, starttidspunkt, varighed, deck, `played_public`, position i sessionen.
- **venues**: navn, by, evt. koordinater. Udfyldes af dig, ikke gættet.

Rækkefølgen i `plays` giver gratis noget du ikke bad om, men nok vil elske: **hvad spiller jeg typisk efter X**.

## Hvad du kan spørge om, når det er bygget

- Mest spillede numre, men kun til gigs, kun i en periode, kun efter kl. 02.
- Warm-up-numre vs. peak-time-numre (tid på aftenen, position i sættet).
- Alt spillet på et bestemt spillested eller en bestemt dato.
- Numre du spillede meget i 2018 og aldrig siden.
- Overgange: hvilke par af numre går igen.

## Gig eller øvning, og hvor

Traktor skriver en historikfil for hver session, også når du øver hjemme. Forslag:

1. **Automatisk gæt** pr. session: lang session (>1½ time), sen aften/nat, mange numre med `PLAYEDPUBLIC` → sandsynlig gig. Kort session om eftermiddagen → øvning.
2. **Du retter** gættet med ét klik i en sessionsliste, og skriver spillested på. Det er nogle hundrede sessioner højst, og et spillested du har skrevet én gang, bliver foreslået igen.
3. **Kalenderforslag** (fase 2): importér en `.ics` fra Kalender, og sessioner der overlapper en begivenhed får dens navn/sted foreslået.

Det er her "AI" kunne komme ind, men først senere og kun som forslag: læse ustrukturerede kilder (gamle Facebook-events, mails med bookinger, Resident Advisor-sider, flyers) og foreslå "6/4-2019 var det nok Culture Box". Kernen skal virke uden.

## Indeni beetsGUI eller et separat værktøj?

**Anbefaling: indeni beetsGUI nu, men bygget så det kan løftes ud.**

- Indeni, fordi det allerede er der Traktor-scanningen, job-motoren, mappe-vælgeren og koblingen til dit bibliotek bor, og fordi beetsGUI er ved at blive en rigtig Mac-app. En "Historik"-fane er den mindste vej til noget du kan bruge.
- Adskilt, fordi spillehistorik ikke handler om at rydde op i musikfiler. Derfor eget modul (`playhistory.py`), egen database, ingen import af beets i datalaget. Hvis det senere vokser til Rekordbox/Serato, setlister fra Mixcloud, kalender og AI-forslag, kan modulet og databasen flyttes til sin egen app uden omskrivning.

Tegnet på at det er tid til at flytte det ud: når mere end halvdelen af det du laver i det, ikke har noget med dit beets-bibliotek at gøre.

## Faser

**Fase 1 (lille, høj værdi):** læs `EXTENDEDDATA` fra historikfilerne ind i `playhistory.db`. Den eksisterende "mest spillede"-liste får en ny kolonne/sortering: *bekræftede afspilninger* (talt fra historik, med dato) ved siden af det estimerede `PLAYCOUNT`, og et periodefilter. Plus en simpel sessionsliste (dato, start/slut, antal numre). Samme regler som #42: originaler læses kun, iCloud-placeholders springes over med besked, genkørsel tilføjer i stedet for at bygge forfra.

**Fase 2:** gig/øvning-gæt og -rettelse, spillesteder, `.ics`-import, filter på tid på aftenen og spillested, "spillet efter X".

**Fase 3 (hvis det giver mening):** andre kilder (Rekordbox, Serato, tracklister), AI-forslag til spillested ud fra ustrukturerede kilder, evt. udflytning til egen app.

## Ærlige begrænsninger

- Historik findes kun for de perioder hvor historikfilerne overlevede dine tre tab. Collection-`PLAYCOUNT` dækker mere, men uden datoer, så begge tal vises side om side og aldrig lægges sammen.
- Klokkeslæt er Mac'ens lokale tid på spilledagen. Gigs i udlandet kan være forskudt en time eller to.
- Numre uden kunstner (en tredjedel af dine gamle entries) arver #42's to-lags-regel: de vises, men markeres.
