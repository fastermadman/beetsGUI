# beetsGUI

![beetsGUI logo](assets/icon.png)

A local web GUI for [beets](https://beets.io) — runs as a standalone macOS app via Safari Web App.

No Electron. No Docker. Just a small Flask server and a single HTML file.

![beetsGUI screenshot](assets/screenshot.png)

## Features

- **Import** — configure and run `beet import` with all options
- **Library** — search and export your collection
- **Clean up** — manage duplicates, cover art, metadata
- **Convert** — find WAV/AIFF files; auto-convert lossless → ALAC on import (via ffmpeg)
- **Find** — fast file search with `fd`
- **Unimported** — scan a folder for music not yet in beets
- **Settings** — edit `config.yaml` directly in the app, generate config from UI
- **Services** — configure Discogs, MusicBrainz, Beatport4
- Dark + light mode (follows macOS system preference)

## Requirements

- macOS (Ventura or later recommended for Safari Web App)
- [beets](https://beets.io) — `pipx install beets`
- Flask — already included in beets' pipx environment:
  ```bash
  pipx inject beets flask
  ```
- ffmpeg (optional, for lossless → ALAC conversion):
  ```bash
  brew install ffmpeg
  ```
- [fd](https://github.com/sharkdp/fd) (required for the **Find** and **Unimported** tabs):
  ```bash
  brew install fd
  ```

## Setup

### 1. Clone

```bash
git clone https://github.com/YOUR_USERNAME/beetsgui.git
cd beetsgui
```

### 2. Start the server

```bash
# If Flask is in beets' pipx environment:
~/.local/pipx/venvs/beets/bin/python server.py

# Or if Flask is in your system Python:
python3 server.py
```

Open http://localhost:1312 in Safari.

### 3. Create a Safari Web App (macOS standalone)

With http://localhost:1312 open in Safari:
**File → Add to Dock → name it "beetsGUI"**

The server will now open the standalone app automatically on next launch.

### 4. Create a launcher (Automator)

Open **Automator → New → Application → Run Shell Script**:

```bash
/path/to/python server.py
# Example with pipx beets:
# ~/.local/pipx/venvs/beets/bin/python /path/to/beetsgui/server.py
```

Save as `beetsGUI Launcher.app`, drag to Dock. One click starts everything.

## Import

The importer runs **inside** the server through the beets Python API — no
`beet import` subprocess, so match decisions are made in the app instead of
in Terminal. The Import tab is a decision queue: candidate cards for
album/track matches, a side-by-side compare for duplicates, and full keyboard
control (`1`-`9` picks a candidate, `Enter` applies, `S` skips, `A` keeps
tags as-is; duplicates use `K`/`S`/`M`/`R` for keep/skip/merge/replace). One
import runs at a time; closing the tab mid-import is safe — reopening the app
rejoins whatever is still running.

Duplicates are ranked by quality before you're ever asked: a lossy copy of an
album already in the library is skipped automatically, a lossless copy
recommends replacing the existing one, and equal-quality duplicates still ask
with no recommendation either way. See `quality_rank()` in `importsession.py`.

The underlying endpoints also work with `curl` alone, for scripting or
debugging:

```bash
# Start: mode is interactive | fast | quiet | timid
# handling is copy | move | keep; incremental and singleton are booleans
curl -s -X POST localhost:1312/import/start -H 'Content-Type: application/json' \
     -d '{"path":"~/Downloads/some album","mode":"interactive","handling":"copy"}'

# Or a curated set of folders (e.g. picked in Unimported, or an fd search) —
# beets groups them itself, same as `beet import path1 path2`
curl -s -X POST localhost:1312/import/start -H 'Content-Type: application/json' \
     -d '{"paths":["~/Downloads/album1","~/Downloads/album2"],"mode":"interactive"}'
```

```bash
# Watch: status lines, decisions and a final done event, as SSE
curl -sN localhost:1312/import/<id>/events
```

```bash
# Answer a decision. choice is apply|skip|asis|tracks|albums for a match,
# skip|keep|remove|merge for a duplicate, or resume|restart to resume.
curl -s -X POST localhost:1312/import/<id>/decide -H 'Content-Type: application/json' \
     -d '{"decision_id":"<from the event>","choice":"apply","candidate":0}'
```

`POST /import/<id>/abort` cancels cleanly, and `GET /import/current` returns the
running import plus its waiting decision, so a reloaded page can rejoin.
An unanswered decision times out after 15 minutes (`BEETSGUI_DECISION_TIMEOUT`)
and aborts the import rather than blocking the server forever.

Test it end to end (needs ffmpeg; runs against a throwaway library):

```bash
~/.local/pipx/venvs/beets/bin/python test_importsession.py
```

## DJ workflow notes

- Lossless files (WAV, AIFF, FLAC) convert to **ALAC 24-bit** on import — 32-bit float is handled automatically
- MP3 and AAC are never re-encoded
- Designed for Traktor / Lexicon / Rekordbox workflows

## Beets plugins supported in Settings UI

Metadata sources: `musicbrainz` `chroma` `beatport4` `discogs` `deezer` `spotify` `tidal`

Enrichment: `fetchart` `embedart` `lastgenre` `fromfilename` `bpsync` `autobpm` `keyfinder` `replaygain` `lyrics`

Maintenance: `duplicates` `missing` `mbsync` `importfeeds` `dirfields` `scrub` `smartplaylist` `unimported`

## Contributing

Pull requests welcome. This started as a personal tool for a DJ/electronic music collection — if you have a different workflow, open an issue.

## About

Built by DR. WARTEMAL — if you'd like to hear what this tool is for, find my music at [soundcloud.com/drwartemal](https://soundcloud.com/drwartemal).

## License

AGPLv3 — see [LICENSE](LICENSE).
