---
status: approved
approved: 2026-11-17
language: da
issues: [151, 152, 159, 160, 161, 156, 158, 146, 155, 145, 137]
source-artifact: https://claude.ai/artifact/3v6kRwkqb4L4FZjJsXCEZP
---

> **Source of truth** for the track-facts data model. If code and this document disagree,
> either the code is wrong or this document must be changed in the same PR.
> Indeks: [README.md](README.md). Dette dokument er på dansk (bruger-sprog); kode, UI og issues er på engelsk.
>
> **Godkendte beslutninger (2026-11-17):**
> 1. `segue.db` er kilden til sandheden; `sg_*`-felter i beets er kun en projektion (kopi af det vindende svar). Justerer beslutningen i #151.
> 2. BPM og key gemmes som `sg_bpm` / `sg_key` ved siden af beets' egne `bpm` / `initial_key`.
> 3. Fil-tags rangerer i tier `analysis` og slår vores egen motor, indtil #137 har tal.
>
> **Issues:** #159 (foundation), #160 (`sg_set`), #161 (History fase 1); omskrevet: #152, #156, #158, #146, #155, #145, #151; kommentar på #137.
> **Figurer:** den indlejrede figur ("hvad bor hvor") findes kun i original-dokumentet (se `source-artifact`); teksten under den beskriver den.

# Track facts: samlet datamodel

2026-11-17 · @FasterMadman

Appens egen database bliver sandheden om alt, hvad Segue ved om et nummer, og beets får en kopi af det aktuelle svar som `sg_*`-felter, så hvert filter stadig går gennem den ene scope-vej. Det er et designforslag: ingen kode og ingen issues er ændret.

## Hvad bor hvor

Der er fem lagre, og hvert har én ejer. Reglen er: det dyrebare (dine svar, historik, ændringslog) bor i appens database, det beets skal kunne filtrere på bliver kopieret ind i beets, og alt der kan regnes igen bor for sig.

<!-- embedded figure omitted: hvad bor hvor · tre kilder, fire databaser, én scope-vej -->

Læs den oppefra: tre slags kilder skriver til `segue.db`, enten direkte eller gennem deres egen database. Derfra går der to veje til scope, og begge ender som en beets-query.

| Lager | Sted | Ejer | Indhold | Hvis det går tabt |
|---|---|---|---|---|
| `library.db` | beets' mappe | beets | Filer, tags, album. Plus `sg_*`-felter som kopi af det aktuelle svar | Genopbygges af import, `sg_*` genskrives fra `segue.db` |
| `segue.db` | appens data-mappe (`SUPPORT_DIR`) | appen | Track-identitet, alle fakta pr. kilde, cues, ændringshistorik, gemte sæt, collections | Vinderne kan genskabes fra `sg_*` i beets, resten er væk. Den skal med i backup |
| `playhistory.db` | appens data-mappe | History-modulet | Sessions, plays, venues, som i Traktor-designet | Genskabes ved at scanne Traktor-filerne igen. Gig/øvning og spillesteder du har tastet er væk |
| `analysis.db` | appens data-mappe | analyse-jobbet | Beatgrid, sektioner, waveform, embeddings pr. lydfil | Koster kun regnetid at lave igen |
| Cache | `~/Library/Caches/beetsGUI` | appen | Thumbnails, waveform-billeder, Atlas-layout | Laves igen på sekunder |

Appen skriver aldrig i `library.db` uden om beets' eget API, og beets kender ikke de tre andre filer.

## Track-identitet

Hvert nummer får et fast id i `segue.db` (`track_uid`), fordi ingen af de nøgler vi har i dag holder alene. beets' item-id skifter, hvis et nummer fjernes og importeres igen. Stien skifter, når beets flytter filen. Traktor-historikken kender kun kunstner og titel.

`tracks`-tabellen holder for hvert `track_uid`: beets item-id, sti, `audio_sig`, og `track_key` (samme normaliserede kunstner+titel som #42 bruger). `track_uid` skrives også i beets som `sg_uid`, så koblingen overlever, at en af de to databaser forsvinder.

`audio_sig` er en hash af selve lydstrømmen, ikke af filen. Den ændrer sig ikke, når tags skrives om, og derfor kan analysen genbruges efter en tag-rettelse. Filens størrelse og ændringstid bruges kun til at afgøre, om hashen skal regnes igen.

Når appen starter, og efter hver import, kører en afstemning, der kobler beets-items til `track_uid` i denne rækkefølge:

1. `sg_uid` på itemet
2. samme `audio_sig`
3. samme sti
4. samme `track_key`, kun når der er både kunstner og titel, og kun som forslag for title-only-rækker

Et nummer der er fjernet fra beets, beholder sin række og sine fakta i `segue.db`. Kommer filen tilbage, får den sine svar igen.

## Fakta, kilder og rangorden

`segue.db` gemmer ét svar pr. nummer, akse og kilde, og beets får kun vinderen. Det er ændringen i forhold til #152, som i dag kun har `sg_*`-felterne: et felt i beets kan holde ét svar, så når du fortryder dit eget, er analysens svar væk.

**Tabellen `facts`**: `track_uid`, `axis`, `tier`, `producer`, `value`, `confidence`, `updated_at`. Primærnøgle er nummer + akse + tier.

- `tier` er den besluttede rangorden: `you` > `history` > `analysis` > `model` > `guess`. Vinderen er den højeste tier, der har et svar.
- `producer` siger præcist hvem: `listen-through`, `traktor-cue`, `file-tag`, `essentia-2.1/edma`, `cues-v3`. Samme tier, nyere producer-version erstatter den gamle. Sådan virker gen-analyse uden at røre dine svar.
- "Spring over" i Listen through gemmer ingenting. "Ryd mit svar" sletter `you`-rækken, og den næste tier vinder igen.

**Projektion til beets.** Når vinderen skifter, skrives `sg_<akse>` og `sg_<akse>_src` på itemet med `item.store()`. Ingen fil-tags røres (besluttet: kun via Take it with you). Appen registrerer felternes typer hos beets, når den åbner biblioteket, så `sg_energy:3..5` og `sg_bpm:120..126` er rigtige talintervaller. Den registrerer også sin egen hurtige query for hvert felt, fordi beets' indbyggede er for langsom (se målingen sidst i dokumentet). Din `config.yaml` ændres ikke.

**Én skrivevej.** Alt går gennem én funktion, `facts.put(...)`. Den gemmer i `segue.db` med det samme, skriver en linje i ændringshistorikken, og lægger projektionen til beets i kø. Køen tømmes, når intet beets-job muterer biblioteket. Derfor kan du svare i Listen through, mens en import kører: svaret er gemt straks, filteret følger få sekunder efter. "What we know about it" læser fra `segue.db` og viser alle kilder, ikke kun vinderen.

| Akse | beets-felt | Type | Værdier | Kilder der kan svare |
|---|---|---|---|---|
| Set role | `sg_role` | enum | opener, warmup, build, peak, closer, afterhours | you, history (tid på natten), guess |
| Floor | `sg_floor` | enum | dance, listen, both | you, model, guess |
| Energy | `sg_energy` | heltal | 1–5 | you, analysis, model |
| Vocal | `sg_vocal` | enum | none, some, full | you, model |
| Mood | `sg_mood` | enum | dark, neutral, bright | you, model |
| Keep | `sg_keep` | enum | sets, listen, no, later | you |
| Mix in / Mix out | `sg_mixin`, `sg_mixout` | sekunder | afledt af cues, se Cues | you, history (Traktor-cue), analysis |
| BPM | `sg_bpm` | decimaltal |  | you, history (Traktor-grid), analysis (motor eller fil-tag) |
| Key | `sg_key` | tekst, Camelot | 1A–12B | you, history (Traktor), analysis (motor eller fil-tag) |
| Plays | `sg_plays`, `sg_plays_public`, `sg_last_played` | tal, dato | optalt fra `playhistory.db` | ingen rangorden, regnes om efter hver scanning |

BPM og key er nye i forhold til #152. De er med, fordi de har præcis samme problem: flere kilder, der er uenige, og et svar du vil kunne rette. Se beslutning 1.

## Scope: hvordan filtre når app-data

Scope forbliver en ren beets-query, og app-data når den på to måder. Der kommer ingen ny form for scope og ingen anden filtervej.

**1. Projektion, for alt der er én værdi pr. nummer.** Rolle, energi, BPM, antal afspilninger, sidst spillet. De ligger som `sg_*` i beets, så `sg_role:peak sg_plays:5..` er en helt almindelig query. Collections (#146), Ask og søgefeltet får dem gratis.

**2. `sg_set:<id>`, for alt der ikke er én værdi pr. nummer.** Spillet på et bestemt spillested, efter kl. 02 til gigs i 2019, numre i et Mix, de 50 der lyder mest som dette, et lasso på Atlas med 3.000 punkter. Appen registrerer ét navngivet query-felt hos beets. beets spørger `segue.db`, hvilke numre sættet indeholder, og får en id-liste tilbage. Det er samme mekanisme som beets' egen `playlist:`-query.

Et sæt i `segue.db` er enten en fast liste (et Mix, et lasso) eller en opskrift, der regnes ud når den bruges (et historik-filter, naboer til et nummer). Midlertidige sæt udløber af sig selv.

Det giver tre ting:

- `sg_set:42 genre:techno` kan kombineres med alt andet og gemmes som collection, fordi det bare er tekst.
- Grænsen på 500 id'er i `scope.ids` holder op med at være et problem for Atlas og Ask.
- Library-listen og hver handling rammer stadig præcis de samme numre, fordi begge går gennem `libops.matching_ids()`.

Klienten taler fortsat kun i beets item-id'er. `track_uid` forlader aldrig serveren.

## Analyse-cache og gen-analyse

Analysen får sin egen database og sin egen arbejdsproces, og den rører aldrig beets direkte. Den læser en lydfil, skriver rå resultater i `analysis.db`, og afleverer de få tal brugeren filtrerer på gennem `facts.put(...)` som alle andre.

**`analysis.db`** er nøglet på `audio_sig`, ikke på nummer eller sti. To kopier af samme lyd deler analyse, og en omdøbt fil skal ikke analyseres igen.

| Tabel | Indhold |
|---|---|
| `runs` | `audio_sig`, analysator, version, parametre, status, varighed, fejl. Viser hvad der er kørt, og hvad der mangler |
| `blobs` | `audio_sig`, slags, analysator, version, data. Slags: `beatgrid`, `sections`, `waveform`, `loudness`, `embedding:<model>` |

Embeddings gemmes som kompakte tal-blokke. Ved 10.000 numre fylder de 25–50 MB og læses ind som én matrix, når Atlas åbner. Atlas-layoutet (UMAP) er afledt og ligger i cachen.

**Gen-analyse.** Hver analysator har et versionsnummer. Når det stiger, er de gamle rækker forældede, og jobbet tager dem i baggrunden. Nye tal erstatter kun svar i samme tier (`analysis` eller `model`), så dine egne svar og Traktor-data står urørt. En hel analyse-omgang er én post i ændringshistorikken og kan fortrydes samlet.

**Uden for single-flight.** Fordi analysen ikke bruger beets' `Library`, behøver den ikke plads i `jobs.py`'s ene kø. Den kører som en separat proces med lav prioritet. Kun projektionen til beets venter på en ledig stund. Det løser #156's krav om, at en import starter inden for 10 sekunder, uden at dele analysen op i bidder.

På Mac bor `analysis.db` i appens data-mappe, ikke i `~/Library/Caches`. En første omgang over en stor samling tager timer, og oprydningsværktøjer tømmer Caches.

## Cues og beatgrid

Cues er rækker i `segue.db`, ikke felter i beets, fordi et nummer har mange af dem, og hver har position, rolle og ophav. `sg_mixin` og `sg_mixout` er kun de to tal, der bliver kopieret ud, så der kan filtreres og regnes ETA på dem.

**Tre lag pr. nummer**, så intet overskriver noget andet:

| Lag | Hvor det kommer fra | Kan redigeres | Tier for Mix in/out |
|---|---|---|---|
| `traktor` | Spejl af cues i `collection.nml`, opdateres ved scanning | Nej, kun i Traktor | history |
| `auto:<template>` | Analysen, én pr. skabelon (Mix points, Structure, Mixed, Custom) | Nej, laves om ved gen-analyse | analysis |
| `segue` | Dit arbejdssæt i appen. Starter som kopi af Traktor eller auto | Ja | you, så snart du flytter eller godkender en cue |

**Tabellen `cues`**: `track_uid`, lag, position i millisekunder, længde (loops), slags (cue, loop, grid, load, fade), rolle (`start`, `mix_in`, `mix_out`, `end`, `drop`, `breakdown`), takt-nummer, navn, hotcue-plads, confidence.

Position gemmes både som tid og som takt i forhold til et grid. Tiden er sandheden, takten gør det muligt at holde reglen om, at auto-cues kun står på frasegrænser (4/8/12/16/24/32 takter).

**Beatgrid** følger samme mønster som fakta: `grids` har én række pr. nummer og kilde. Traktors grid (anker + BPM, som du selv har rettet til) er tier history. Analysens grid er tier analysis og kan være en fuld beat-liste for numre med variabelt tempo. Auto-cues regnes på det vindende grid, så et grid du har rettet i Traktor også flytter Segues forslag.

Skrivning tilbage til Traktor hører under Take it with you og er ikke med her. Tabellen har det den skal bruge (hotcue-plads, slags, navn).

## Spillehistorik

`playhistory.db` bliver, som Traktor-designet beskriver den: egen fil, eget modul, ingen beets i datalaget, så den kan løftes ud senere. Det eneste nye er koblingen til resten.

- **Kobling**: `segue.db` får `history_links` (`track_uid`, historikkens track-id, metode, sikkerhed). Historik-modulet kender ikke `track_uid`, og `segue.db` ejer ikke plays. Title-only-rækker kobles kun som forslag, du bekræfter.
- **Optalte tal** (`sg_plays`, `sg_plays_public`, `sg_last_played`) regnes om efter hver scanning og projiceres til beets. Bekræftede afspilninger og Traktors `PLAYCOUNT` lægges aldrig sammen, som designet siger.
- **Afledte fakta** går gennem `facts.put(...)` med tier history: rolle ud fra tid på natten og plads i sessionen, Mix in fra Traktor-cue, BPM og key fra Traktor.
- **Alt pr. afspilning** (spillested, klokkeslæt, rækkefølge, "spillet efter X") bliver i `playhistory.db` og når scope som `sg_set`.

Rekordbox, Serato og Denon kommer ind gennem samme adapterlag og lander i de samme tabeller: plays i `playhistory.db`, cues i et lag pr. program, grid som en kilde mere.

## Undo og ændringshistorik

Der er én ændringslog i `segue.db`, og alt der ændrer noget ved et nummer, skriver i den: Listen through, action-panelet (#145), analysen, Health. Det er den "planned change history", som #152, #137 og visionen henviser til.

- `change_sets`: hvornår, hvem (`you`, et job, en import), en tekst brugeren kan læse ("Set role to Peak on 14 tracks"), og om den er fortrudt.
- `change_rows`: `track_uid`, hvad der blev ændret, værdi før og efter.

| Slags ændring | Før/efter gemmer | Fortryd gør |
|---|---|---|
| Fakta og cues | Den berørte række i `facts` eller `cues` | Sætter rækken tilbage og projicerer vinderen igen. Altid muligt |
| Metadata via beets (titel, genre, år) | Feltets værdi | Kører den omvendte ændring gennem samme `libops`-vej, inklusive tag-skrivning |
| Fjernelse (kun Health) | Hele itemets felter, og hvor filen blev lagt | Lægger filen tilbage fra papirkurven og itemet tilbage i beets. Muligt indtil papirkurven tømmes |

**Regler.** Fortryd gælder en hel `change_set`. En række fortrydes kun, hvis værdien stadig er den, ændringen satte. Er den ændret siden, springes den over, og du får at vide hvor mange. Z i Listen through fortryder din seneste `change_set` i den omgang. Fortryd er selv en `change_set`, så den kan fortrydes.

Import er ikke med: beets' import flytter filer og slår op hos MusicBrainz, og det kan ikke rulles tilbage række for række. "Fjern det jeg lige importerede" er en almindelig fjernelse.

Fil-niveauet (papirkurv på samme drev, så der ikke kopieres på tværs af diske) designes i Health-issuet. Loggen har pladsen til det nu, så Health ikke skal lave sin egen.

## Robusthed, sikkerhed og tests

- **To databaser kan ikke skrives atomart.** Rækkefølgen er altid `segue.db` først, beets bagefter, og hver ventende projektion er markeret. Går appen ned midt imellem, retter afstemningen ved næste start op. `segue.db` vinder altid, undtagen når den ikke kender nummeret: så læses `sg_*` fra beets ind som udgangspunkt.
- **Data-mappen kan flyttes.** Stien til `segue.db`, `playhistory.db` og `analysis.db` kommer ét sted fra og kan sættes med en miljøvariabel. Hver test peger den på en midlertidig mappe ved siden af sin `BEETSDIR`, så ingen test kan røre din rigtige app-data. Det er samme regel som for `~/.config/beets`.
- **Skema-version** i hver database (`PRAGMA user_version`) og små, nummererede migrationer. Før en migration tages en kopi af `segue.db`.
- **Navnet `sg_`** bliver stående, uanset hvad appen ender med at hedde (#76). Feltnavne er data, og et nyt præfiks ville være en migration uden gevinst.
- **Nye endpoints** følger de eksisterende regler: `{"scope": ...}` ind, Host- og Origin-vagterne arves, ingen `shell=True`.
- **De to JSON-filer ved siden af `library.db`** (`beetsgui_settings.json`, `traktor_recovery.json`) bliver, hvor de er. De kan flyttes senere og er ikke en del af dette.

## Byggerækkefølge og ændringer i issues

Fundamentet er rent server-arbejde og kan bygges nu, uafhængigt af #138 og #139. Intet herunder er oprettet eller ændret endnu.

| Trin | Issue | Indhold | Afhænger af |
|---|---|---|---|
| 1 | Nyt: App database foundation | Data-mappe med test-override, `segue.db` med migrationer, `tracks` + afstemning, `audio_sig`, `change_sets`/`change_rows` | Intet |
| 2 | #152, skrives om | `facts` + `facts.put`, projektion med kø, typer og hurtige `sg_*`-queries, endpoints. UI-tabellen "What we know about it" bliver sin egen del | Trin 1. UI-delen: #144 |
| 3 | Nyt: `sg_set` | Gemte og midlertidige sæt, det navngivne query-felt | Trin 1 |
| 4 | Nyt: History phase 1 | `playhistory.db` som i Traktor-designet, `history_links`, `sg_plays*` | Trin 1, 3 |
| 5 | #156, udvides | `analysis.db`, analyse som egen proces, `grids`, cue-laget `auto` | #137, trin 2 |
| 6 | #158, udvides | Cue-laget `traktor`, fakta i tier history | Trin 4, 5 |

Mindre rettelser i eksisterende issues:

- **#146** Collections gemmes som tabel i `segue.db` i stedet for en fil for sig.
- **#155** Mixes er et fast `sg_set` med rækkefølge.
- **#145** og Health bruger ændringsloggen fra trin 1 og laver ikke deres egen.
- **#137** får to målinger mere: forskydning mellem Traktors cue-tider og vores afkodning, og scoring af cue-prototypen (hvis du siger ja til det åbne spørgsmål fra oktober).
- **#151** får en linje om, at `sg_*` i beets er en kopi, og at `segue.db` er kilden.

Issues skrives i det aftalte format (Session-tabel, Why, Tasks, Goal metrics, Depends on). Trin 1 og 3 er velafgrænsede nok til Sonnet/medium med dette dokument som brief. Trin 2 beholder Opus-plan plus Sonnet-build.

## Beslutninger (alle tre godkendt 2026-11-17)

**1. `segue.db` som kilde, `sg_*` i beets som kopi.** Det justerer beslutningen fra #151, hvor felterne i beets var selve lageret. Felterne og filtrene er de samme, men sandheden flytter. Jeg anbefaler ja: uden det kan et fortrudt svar ikke falde tilbage på analysens, og du kan ikke svare, mens en import kører.

**2. BPM og key som `sg_bpm` og `sg_key`, ved siden af beets' egne felter.** beets' `bpm` og `initial_key` er et spejl af filens tags. Skriver vi analysens tal der, så enten ruller "Update from files" dem tilbage, eller også skal vi skrive i dine originale filer. Jeg anbefaler egne felter: brugerfladen viser ét BPM og én key (Segues bedste svar), og filens tag er bare én af kilderne. Tags skrives kun via Take it with you.

**3. Hvor fil-tags står i rangordenen.** Jeg anbefaler tier analysis, fordi et tag er en andens analyse. Indtil #137 har tal, vinder fil-tagget over vores egen motor, da dine tags kommer fra Mixed In Key. #137 afgør, om det skal vendes pr. felt.

Siger du ja til alle tre, skriver jeg issues for trin 1–3 og retter de nævnte, og går derefter videre til planen for #140.

## Målt og mangler at blive verificeret

**Målt i dag** på et testbibliotek med 40.000 numre, beets 2.14.1 og Python 3.12 (CI's versioner), i en midlertidig mappe:

| Filter | beets' indbyggede | Appens egen query-klasse |
|---|---|---|
| `sg_role:peak` (4.460 træf) | 4,6 s | 0,5 s |
| `sg_energy:3..4` (5.327 træf) | 4,9 s | 0,7 s |
| `sg_bpm:120..126` (1.379 træf) | 4,9 s | 0,17 s |
| `sg_role:peak sg_energy:4.. artist:a1` (203 træf) | 4,6 s | 0,05 s |

beets filtrerer flex-felter i Python efter at have læst hele biblioteket, så hvert `sg_*`-filter koster det samme som at hente alt. En query-klasse på 12 linjer, der spørger `item_attributes` direkte i SQL, bringer det under grænsen på 1 sekund fra #12. Selve opslaget tager 8 ms. Resten er beets, der bygger objekter, og det er den kendte flaskehals i `matching_ids()`. Maskinen her er langsommere end din Mac, så tallene er øvre grænser.

Samme test bekræftede tre ting, designet bygger på:

- Appen kan registrere felt-typer og navngivne queries i processen, uden at røre `config.yaml`.
- `sg_set` med 15.000 id'er virker og kan kombineres med andre led, både med og og med eller.
- At gemme et flex-felt på ét nummer tager 2 ms.

**Mangler**, og hører hjemme i de nævnte issues:

- Trin 1: hvad `audio_sig` koster pr. fil, og at den er uforandret efter en tag-skrivning for MP3, FLAC, AIFF og M4A
- Trin 2: om `item.store()` fra en request kan ramme et kørende import-job. Køen antager at det er usikkert
- #137: forskydning mellem Traktors cue-tider og vores afkodning af MP3 og M4A. Selv 25 ms er hørbart på en cue
- #158 og History: Traktors feltnavne for cues og `EXTENDEDDATA` på dine egne filer, og hvilken cue der er Mix in
- Trin 5: størrelse og indlæsningstid for embeddings på din rigtige samling
