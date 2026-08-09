# CLAUDE.md — beetsGUI

Read this before making changes. It's the essence, not a re-hash — every
claim here traces to a docstring/comment at the cited location; read that
for the full reasoning, not this file.

## What this is

Local web app for [beets](https://beets.io) as a Safari Web App on macOS.
`server.py` (Flask, port 1612) + `beetsgui.html` (single file, no build
step, no npm). Never assume a build/bundle step exists — there isn't one.

## Local beets version must match CI's pin (#47)

CI (`.github/workflows/test.yml`) pins `beets==2.13.1` deliberately — "a red
run here should mean this PR broke something, not beets shipped a new
release." Check your local install matches before trusting any local test
result: `pipx runpip beets show beets`. This bit hard once already: an
entire session's worth of local "all green" runs were against beets 2.11.0,
and merging on that basis shipped a real bug (#12/config-path, below) that
only exists on 2.13+. If a local run and CI disagree, assume the version
mismatch first, not a flaky test. To reinstall the pinned version:
`pipx runpip beets install beets==2.13.1`.

## Non-negotiable safety rule

**Never point a test, a script, or an experiment at `~/.config/beets` or
`/Volumes`.** The user has a real, live beets library on external drives.
Every test uses a throwaway `BEETSDIR` in a temp dir — copy the pattern in
`test_importsession.py`'s `start_server()`/`stop_server()`, don't invent a
new one. If you need to inspect real files (e.g. to check tags), reading is
fine; writing/importing/deleting through them is not, without asking.

## Request-boundary security model (#12, #28)

Two `before_request` guards in `server.py`: Host check (`_reject_foreign_host`,
blocks DNS rebinding) and Origin check (`_reject_foreign_origin`, blocks a
cross-site page's POST — Host alone doesn't catch this, a cross-site request
still carries `Host: localhost:<port>`). Both are needed; three separate
audit passes (#11, #27, #38) treated the Host check plus a "JSON needs a
preflight" assumption as sufficient, which a `text/plain` body (CORS-safelisted,
no preflight) defeats. If you add a mutating endpoint, it inherits both
guards automatically — don't add your own CSRF handling per-route.

No `subprocess` call anywhere uses `shell=True` — every one takes an argv
list. Keep it that way; string-built shell commands were the root cause the
old `/run` endpoint got deleted for (see `docs/audit-2026-08-security-review.md`).

## Scope pattern (#63/#64)

Every list/action endpoint takes `{"scope": {"query": "..."}}` or
`{"scope": {"ids": [...]}}`, never a bare hand-typed query — `libops.scope_query()`
turns either into the query string `libops.matching_ids()`/`split_query()`
consume. The Library list and every action share this same resolution path
on purpose (see `libops.matching_ids()` docstring) — don't add a second way
to filter that could disagree with it.

## Query building lives in two places that must agree

- Server: `libops.split_query()` — beets' `shlex.split()`, falls back to a
  plain whitespace split on an unbalanced quote (an apostrophe in a name
  like `O'Brien` is not an error here, #12).
- Client: `shellQuote()` in `beetsgui.html` — POSIX single-quotes filter
  values. This isn't just display: it's the actual query the app sends to
  `/library*`, and it's also what a user would paste into a real shell if
  they used the old Command box (deleted #12) or the current filter's raw
  query box. `test_filter.py` cross-checks both sides against real `bash`
  and real beets — extend it, don't hand-verify a quoting change.

## Paths in `library.db` can be relative (beets 2.11+)

`resolve_item_path()` in `server.py` handles both forms — since beets
2.11 (upstream #6460), paths under `directory:` are stored relative to it.
A relative path is normal, not corruption (#78) — don't "fix" a relative
path by treating it as a decoder gap or a broken import.

## Job model

`jobs.py`: one beets job at a time, globally — not one-per-type. beets'
own `config`/`Library` are process-global and not thread-safe for concurrent
mutation, so import/convert/artwork/sync/Traktor-scan all share one
single-flight registry. `GET /jobs/current` and `POST /jobs/<id>/abort` are
shared across every job type, not import-specific — the route names don't
say "import" despite older docs (including README, before this session)
sometimes calling them that.

## `get_config_path()`/`get_library_db_path()`/`get_library_directory()` cache only on success (#12)

beets >= 2.13 can run a one-time schema migration as a side effect of *any*
`beet` invocation against a library that hasn't been opened this way before
— including a brand-new one, so this fires on every fresh beetsGUI install's
first request. It prints `Created database backup at: ...` lines to
**stdout**, ahead of the path `beet config --path` actually returns. These
three functions are `@functools.lru_cache`d for the process's life (avoids
a ~700ms `beet` subprocess per request) — if you ever touch them, the
non-negotiable invariant is that a *failed* resolution must never be
cached, only a genuinely successful one, or one noisy first call
permanently misdirects every `/library`-family read to `~/.config/beets` —
the user's real library — for the rest of the server's life, silently
(`ok: true`, empty results, no error). See `test_config_path.py` for the
mocked reproduction; it doesn't depend on real migration timing.

## Traps that already bit once here

- Some endpoint URLs in `beetsgui.html` are built by string concatenation
  (e.g. `'/library/'+kind` in `runMaint()`), not written as a literal.
  `git log -S'/library/write'` or a plain grep for the literal route string
  will report "never referenced" even when a real button calls it — this
  produced a wrong dead-code finding in this session's own audit. Trace the
  actual call sites (read the onclick handler through to the fetch/postJSON
  call) before concluding something is unused.
- A local test suite that's all green proves nothing if it ran against the
  wrong beets version — see the pin note above. This shipped a real bug to
  `master` for about 20 minutes before CI caught it.

## Testing

12 suites, all `test_*.py` at repo root, each runnable standalone:
`~/.local/pipx/venvs/beets/bin/python test_<name>.py`. Most need `ffmpeg`
on PATH; check your beets version matches CI's pin first (see above).
`test_smoke.py` drives a real headless Chromium via Playwright
(`pipx inject beets playwright && playwright install chromium`) — it's the
only one that exercises `beetsgui.html`'s actual DOM/JS rather than the
HTTP API, and it has already caught a bug (a format preset referencing a
field that doesn't exist) that every API-level test missed. If you touch
UI behavior, extend this one, not just the API-level tests.

CI only runs `test_importsession.py`/`test_transcode.py`/`test_jobs.py`/
`test_playback.py` (`.github/workflows/test.yml`) — the other 8, including
`test_smoke.py`, are local-only. A change that only breaks one of those
won't go red in CI; run the full set locally before trusting a PR.

No `fd` or `xld` dependency — both were removed from what the app actually
calls; don't reintroduce a shell-out to either without a documented reason.
