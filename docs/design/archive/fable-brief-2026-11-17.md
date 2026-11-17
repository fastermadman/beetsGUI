> **Archive / snapshot, 2026-11-17.** Status and brief written for a design session; it records the state *then* (e.g. "phase 1 is 0 of 12 done"). Do not treat it as current. Current designs: [../README.md](../README.md).

# beetsGUI / Segue: status and brief for a Fable 5.2 session
Written 2026-11-17 from the repo, project memory and project files. Read-only prep; no issues, PRs or code were changed. Verify anything you build on against the repo.

## 1. Where we are (short)
- Goal: turn beetsGUI (Flask `server.py` + single-file `beetsgui.html`, beets as engine) into a polished Mac app (working name **Segue**) that non-technical people can use. Mac first, UI in English, not for sale (so CC BY-NC-SA Essentia models are fine). Code keeps saying beetsGUI until the naming decision (#76; Segway trademark risk noted, no final search).
- **Merged on master:** everything up to 2026-09-22: scope pattern, filter rows + action bar, one activity surface, library track table, AcoustID/dedup, USB mirror as a convert job, beets 2.14 fixes, Preferences config save (#132/#133), note-box triage (#123), a11y/contrast passes. master = d2d238b.
- **Nothing has landed since 2026-09-22.** Phase 1 (#150) is 0 of 12 done; enrichment (#151) is 0 of 7. The only open PR is **#136** (desktop app, pywebview + py2app alias build), still **draft and never run on a Mac** (that is #138). It sits on branch `claude/project-thread-cw9tqh`, and master has not moved.
- Planning is essentially complete: vision doc, 9+ mockup boards, issues #138-#158 each with a Session table (model, effort, plan/auto). The next thing that creates value is building, starting with #138.
- Repo rules (CLAUDE.md): CI pins beets 2.14.1 + Python 3.12; never touch `~/.config/beets` or `/Volumes`; all list/action endpoints take a `scope`; no `shell=True`; no build step. CLAUDE.md says "12 suites", PR #136 says 18; trim in #135.

## 2. Open work, by track
**Phase 1 (#150), build order:** #138 verify desktop app on Mac + land #136 (Sonnet/low) -> #139 split `beetsgui.html` into `ui/`, pure move (Sonnet/medium) -> #140 app shell, sidebar, router, ES modules (Opus plans, Sonnet builds) -> #142 Add music view; #141 "Import never blocks / Needs you" (Opus/high, server only, can start now); #143 album grid data + thumbnails (Sonnet, server only, can start now); #144 Library grid/artists/album page; #145 one action panel (Opus builds); #146 smart collections; #147 first-run mockups; #148 first-run guide; #149 acceptance with a real person; #39 watched folders.
**Enrichment (#151):** #152 track facts (`sg_*` flexattrs + source ranking) -> #153 Listen through -> #154 New in -> #155 Mixes; #156 intro/outro detection (waits on #137); #157 set ETA; #158 "how you mix" (waits on History phase 1).
**Analysis:** #137 benchmark key/BPM engines against Traktor `collection.nml` and Mixed In Key tags, parked until the user is at the real collection. Auto-cue prototype exists in `/mnt/project-files/segue-cues-prototype/` (librosa, 5 tracks, no ground truth yet; plan is to score it inside #137 against Traktor cues).
**History/Traktor:** design in `/mnt/project-files/traktor/spillehistorik-design.md` (`playhistory.db`, plays/sessions/venues, EXTENDEDDATA fields to verify on real files). No issue/build yet; #158 depends on it.
**Older backlog that phase 1 supersedes:** #124/#125/#127 by #145, #119 by #144, #121 by #143, #126 by #142. Standalone: #129 tooltips, #135 CI timeout, #122, #120, #89, #80, #76, #59, #26/#25 (Traktor/Rekordbox research).

## 3. Decisions already made (do not re-litigate)
- Library before Health. Rekordbox first, then Traktor/Serato/Denon through one adapter layer. Rust not now (revisit only if #137 shows a bottleneck).
- Split: `ui/` folder served by Flask, no build step. Stage A = classic-script move with zero changed lines (provable by diff); stage B = new views as ES modules (`ui/views/<place>.js`, `mount`/`unmount`, `data-action` delegation), legacy retired view by view.
- App state (parked imports, collections, watched folders, thumbnails) lives in the app's own folders (`SUPPORT_DIR`, `~/Library/Caches/beetsGUI`), never in beets' config/library.
- Answers stored as beets flexattrs `sg_<axis>` + `sg_<axis>_src`; source rank You > History > Analysis > Model > Guess, lower never overwrites higher. Raw analysis (sections, embeddings, waveforms) in app cache.
- Every filter goes through the one scope path, including collections, Ask and the search field.
- No swipe deletes a file; only Health removes, with confirm and undo. Everything works without AI; Essentia models only add vocal/mood/"sounds like".
- Set builder: Atlas (sound map) + Orbit (next-track ring of 8 by mix-score: sound + tempo + key + energy + history, weight sliders); locks (compatible key, tempo +-3 %, no vocal after vocal, played out) + direction (Lift/Hold/Breathe/Detour); 9 energy-curve templates; naming undecided.
- Cues only on phrase boundaries (4/8/12/16/24/32 bars); templates Mix points, Structure, Mixed, Custom.
- Workflow: use each issue's `model:`/`effort:` labels and plan-mode line; stop at the Effort switch marker and ask. Issue format: Session table, Why, Tasks, Goal metrics, Depends on.

## 4. Where a stronger model adds the most
Not the mechanical phase 1 build. Sonnet handles #138, #139, #143, #146, #148, #149 and most of #140/#142/#144 once planned. Fable earns its cost on decisions that are expensive to reverse and on the genuinely hard problems:
1. **One data model for "track facts".** Unify `sg_*` flexattrs, source ranking, History/Traktor plays (`playhistory.db`), analysis cache (BPM/key/sections/embeddings/cues) and Traktor cue points. Decide what lives in beets vs the app DB, how provenance, undo/change history and re-analysis work, and how scope queries reach app-DB facts (today scope only speaks beets queries). Touches #152, #156, #158, #137, History, Health/undo. Highest leverage; wrong here means a migration later.
2. **Stage B architecture (#140).** Router, module contract, state/event model, how legacy and new views coexist, how Atlas (canvas/WebGL, 10k+ points) and Listen through (gesture/keyboard card flow) fit a no-build-step, vanilla ES-module setup. Plan only; Sonnet builds.
3. **Phrase and structure analysis (#156, cues prototype).** Constant-tempo grid fails on live/variable-tempo material; mix-in/out picking is heuristic with no ground truth yet. Needs a scoring harness against Traktor cues and an honest accuracy bar before it goes in the product.
4. **Mix-score and Atlas/Orbit.** Define the score, its weights and learning from play history (#158), the UMAP/embedding pipeline and its performance on a real collection, and the set ETA overlap model (#157).
5. **Import "never blocks" (#141) and the action panel preview/apply hand-off (#145).** These are the places where beets internals and undo semantics are subtle; the issues already say Opus builds.
6. **Standalone bundle** (beets + plugins + ffmpeg inside a signed .app): packaging, updates, notarisation. Later, not now.

Suggested use: one Fable session for item 1 as a written design (doc + issue edits, no code), then one for item 2 (#140 plan). Items 3-4 wait for #137 numbers on the real collection. Do not start Fable on features before #138/#139 land.

## 5. Open questions for the user
- Verify #136 on the Mac (#138): when? It gates the whole spine.
- Final name (#76) and trademark search before any public release.
- Whether the cue prototype is scored inside #137 (proposed, not confirmed).
- "Take it with you" mockup screen is still missing.
