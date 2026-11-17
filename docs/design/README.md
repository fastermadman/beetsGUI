# Design documents: index

These are the **source of truth** for decisions that were expensive to make and are expensive to reverse. Issues say *what to build*; these say *why it is shaped this way*. Read the relevant one before touching the area it covers.

**Rule:** if a PR changes behaviour a design describes, it updates the design in the same PR (the PR template asks). If a design turns out wrong, change the document and say so in the PR; don't quietly drift from it. A document's header carries `status`, the approval date, and the issues it governs; `scripts/check_repo.py` (run in CI) checks the headers exist and the links resolve.

## In git

| Document | Language | Status | Governs |
|---|---|---|---|
| [track-facts-data-model.md](track-facts-data-model.md) | da | approved 2026-11-17 | `segue.db` / `sg_*` / `sg_set` / change log / cues / history; #151, #152, #159, #160, #161, #156, #158 |
| [app-shell-140.md](app-shell-140.md) | en | approved 2026-11-17 | App shell, router, view contract, legacy bridge, native drop; #140 (PRs 1-4), later views |
| [play-history-traktor.md](play-history-traktor.md) | da | reference (adopted by the data model) | `playhistory.db` from Traktor sessions; #161, #158 |
| [prototypes/auto-cues/](prototypes/auto-cues/README.md) | da | prototype, unapproved | Phrase-snapped mix in/out cues; to be scored in #137/#156 |
| [archive/fable-brief-2026-11-17.md](archive/fable-brief-2026-11-17.md) | en | snapshot, may be stale | Status brief written for a design session |

Language: the user writes Danish, so older design notes are Danish; the **app UI, code, issues and PRs are English**.

## Not in git (still only artifacts, listed so they are not forgotten)

Product vision and the mockup boards live as claude.ai artifacts, linked from [#150](https://github.com/fastermadman/beetsGUI/issues/150). They are visual; if a decision in them starts to drive code, copy the decision into a document here. The enrichment proposal (#151) and the auto-cues write-up exist as issue text and project memory.

## When code lands

- #159 adds `segue.db` with `PRAGMA user_version`: from then on a schema change needs a numbered migration **and** an update to the data-model document (add a check to `scripts/check_repo.py` that the latest migration number is mentioned there).
- #140 PR 1 adds `test_ui_core.py`: add its step to `.github/workflows/test.yml` (the guard fails if you forget).
