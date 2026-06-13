# BeetsGUI — Log

---

## 2026-06-13 · Valdefar

### Gjort
- Bilingual DA/EN gennemtjekket og fikset (alle hardcodede strenge → t())
- Font-system: Jost (UI) + Gnomon Simple/Foreground (logo, TOTD/DIST-akser) + Drafting Mono (mono)
- Gnomon to-lags CSS Grid overlay (fundet korrekte aksetags i gnomon.designspace på GitHub)
- Splash screen: Gnomon `*` snurrer som loader, poller /status, fader ud ved ok
- beetsGUI (lowercase b), fed vægt (700), EN default, EN/DA toggle med accent-fremhævning
- USB Mirror sektion i Convert: beet convert + /playlists endpoint + playlist-filter
- FLAC/ALAC-dropdown til import-konvertering
- mp3_320 format altid inkluderet i genereret config.yaml
- Horizontal scrollbar fix (overflow-x: hidden)
- Script Editor launcher fix: rename til "BeetsGUI Start" løste restart-loop
- Logo-mark valgt: `*` i Gnomon (skalerer, indestructible type*'s eget mærke)

### Valgt
- CSS Grid til Gnomon-lag frem for absolut positionering
- Script Editor frem for Automator (ingen stderr-fejl)
- `*` som ikon-mark frem for beetsGUI-tekst (for langt til kvadratisk ikon)
- TOTD-akse: 0=6:00, 1000=18:00 (fundet i gnomon.designspace)

### Fravalgt
- Automator → stderr-fejl selv med 2>/dev/null
- BeetsGUI/BEETSGUI → beetsGUI
- Absolut positionering → CSS Grid
- bG, BG som initials → `*`

### Næste
1. `git add . && git commit -m "..." && git push` (alle ucommittede ændringer)
2. App-ikon: screenshot `*` i Gnomon 1024×1024 → `sips` + `iconutil` → .icns
3. Community-post discourse.beets.io + r/beets
