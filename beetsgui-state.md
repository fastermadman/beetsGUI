# BeetsGUI — State

Sidst opdateret: 2026-06-13 · Valdefar

## Status
Klar til community-post. Næsten færdig — mangler kun app-ikon (.icns).

## Repo
https://github.com/fastermadman/BeetsGUI

## Filer
- `beetsgui.html` — single-file GUI
- `server.py` — Flask server, port 8080
- `fonts/` — Gnomon-Simple.ttf, Gnomon-Foreground.ttf, DraftingMono-Regular.ttf, DraftingMono-Bold.ttf

## Launcher
Script Editor .app navngivet **"BeetsGUI Start"** (VIGTIGT: ikke "BeetsGUI" — forårsager restart-loop via open_app_when_ready)
```applescript
do shell script "lsof -ti:8080 | xargs kill -9 2>/dev/null; true"
delay 0.3
do shell script "nohup /Users/valdefar/.local/pipx/venvs/beets/bin/python /Users/valdefar/ValdiVault/30_Resources/DR.WARTEMAL/beetsGUIv2/server.py > /tmp/beetsgui.log 2>&1 &"
```

## Typografi
- **Gnomon Simple + Foreground** (lagdelt, TOTD-akse 0–1000, 6:00=0, 18:00=1000, DIST=400/450)
- **Jost** (Google Fonts CDN) — UI
- **Drafting Mono** — monospace

## Udestående
- [ ] Commit alle ucommittede ændringer (beetsGUI, grid-fix, EN default, EN/DA toggle, USB mirror, FLAC, splash, scrollbar)
- [ ] App-ikon: `*` i Gnomon → screenshot 1024×1024 → sips + iconutil → .icns → apply på "BeetsGUI Start"
- [ ] Community-post: discourse.beets.io (Tools) + r/beets
