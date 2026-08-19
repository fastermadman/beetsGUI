# beetsGUI — UI/UX design pass

Research + audit + phased plan for [#89](https://github.com/FasterMadman/beetsGUI/issues/89).
Written 2026-08-19. **Plan only — no code changed in this pass.**

Scope note: #89 is the third leg alongside [#36](../../issues/36) (don't expose beets'
command surface) and [#87](../../issues/87) (multi-step flow structure). This document
stays on *visual and interaction polish* and defers IA/flow verdicts to those two, but
they overlap and the sequencing section says where.

---

## Method

Audited the running app, not the source. Server booted against a **throwaway `BEETSDIR`**
in a temp dir with three generated albums (two imported, one left unimported so Scan had
a real result). The real library on `/Volumes` was never touched — `/status` confirmed
`config_path`, `library_db` and `directory` all resolved into the sandbox and
`volumes: []`.

Every tab walked at 375 / 768 / 1280 px, light and dark, plus the Preferences dialog and
a live scan. Numbers below are measured in-page, not estimated. The measurement snippet
is in [Appendix A](#appendix-a--re-running-the-baseline) so these can be re-run after each
phase.

Comparable studied: **MusicBrainz Picard** — the closest thing that exists to this app
(a GUI over the same tagging/matching problem). Its
[main screen docs](https://picard-docs.musicbrainz.org/en/v2.13/getting_started/screen_main.html)
describe three things beetsGUI lacks: a two-pane *unmatched → matched* spatial split, a
metadata pane that is explicitly a three-column **tag name | original value | new value**
diff, and a cover-art pane showing new vs. existing art. Broader "2026 UI trend" searching
was mostly SEO filler and produced nothing worth citing; where a claim below is design
judgement rather than a sourced pattern, it says so.

---

## Overall assessment

This is not a prototype wearing a prototype's clothes. It has a real design system —
self-hosted OFL type with a deliberate three-role split (Gnomon/Jost/Drafting Mono),
a full token set, an explicit light/dark/system control, `<details>` for native
disclosure, and one screen — the import decision panel — that is genuinely well designed.

The problem is not that it looks cheap. It is that **it asks the user to hold too much at
once, and where a control isn't self-explanatory it adds a sentence instead of fixing the
control.** That reads as beta-grade regardless of how good the typography is.

The strongest evidence for this isn't in the measurements: the person who built the app
reports finding it overwhelming. When the author is overwhelmed, cognitive load is a
defect, not a matter of taste.

---

## Goal metrics

The design-technologist framing #89 asked for: instrument first, then design against
numbers, so "does it feel modern now?" has an answer that isn't a vibe check.

| # | Metric | How it's measured | Baseline | Target |
|---|---|---|---|---|
| M1 | Interactive controls visible on load, Inbox | count of `button/input/select/textarea/summary` visible, collapsed sections excluded | **30** | ≤ 12 |
| M2 | Primary-styled buttons per screen | `.btn-primary` visible | **3** (Inbox), **3** (Export) | 1 |
| M3 | Share of on-screen words that are explanatory notes | words in `.alert` ÷ words in panel | **57%** (Export) | < 15% |
| M4 | Note boxes in the app | `.alert` count in source | **50** | < 20 |
| M5 | Sampled text pairs failing WCAG AA (4.5:1) | computed contrast, both themes | **6 of 16** | 0 |
| M6 | Async surfaces with a live region | `aria-live` count | **0** | all job/scan/import status |
| M7 | Buttons with press feedback | `:active` rules | **0 of 117** | all |
| M8 | Same setting exposed in two places | manual | **2 pairs** | 0 |
| M9 | Inline `style=` bypassing tokens | source count | **111** | < 30 |
| M10 | Album artwork shown anywhere | `<img>` excluding app icon | **0** | library rows + decision panel |
| M11 | Persistent orientation anchors while scrolling | elements with `position:sticky/fixed` inside `.content` | **0** | section headers pinned |
| M12 | Layout shift when scan results appear | landmark `getBoundingClientRect().top` before vs. after | **71px** (1 result), up to **~430px** | 0 |

Per-tab baseline, for reference:

| Tab | Controls | Primary btns | Collapsed sections | Note boxes | % words explaining |
|---|---|---|---|---|---|
| Inbox | 30 | 3 | 3 | 1 | 8% |
| Library | 27 | 1 | 6 | 0 | 0% |
| Export | 11 | 3 | 0 | 3 | **57%** |
| Recover | 7 | 1 | 1 | 0 | 0% |

The pattern is the finding: **Recover is the calmest tab and it is the one with seven
controls and one primary action.** Inbox and Export are the two that feel bad. Nothing
about Recover's styling is different — only its load.

---

## The thesis

**The app is organised around beets' verbs rather than the user's goals, and it
compensates with prose.**

Five observations, all measured:

1. **Inbox is two unrelated jobs stacked on one page.** "Scan for new music" and the
   import configuration each have their own path input, their own gold CTA, and no
   stated relationship — yet the scan's whole purpose is to feed the import. 30 controls,
   19 of them binary toggles, 3 competing primary buttons.

2. **Where a control isn't obvious, a note was added instead of redesigning it.** 50 note
   boxes; Export is 57% explanation by word count, including a five-step numbered
   Traktor procedure the user must perform *by hand in another app*. #89's own origin
   story — unlabeled checkboxes fixed by adding a label in #77 — is this reflex.

3. **The same setting exists twice, because the page mirrors beets' flags rather than a
   mental model.** Copy/Move/Keep-in-place is "File handling" on Inbox and "Import
   strategy" in Preferences. The two `SAMPLES / STEMS / ABLETON / SPLICE / ONE SHOTS`
   checkbox rows on Inbox look identical and mean different things (scan-result filter
   vs. real import exclusion) — the app resolves this with a note, one row apart.

4. **There is no fixed reference anywhere in the scrolling area.** Zero elements inside
   `.content` are `position: sticky` or `fixed` — every section title scrolls away, so
   past the first screen nothing on screen says which section you are in or what else
   exists. The page is a continuous ribbon with no map, and it *moves*: scan results are
   injected mid-page, pushing everything below them down by 71px with a single result and
   by up to ~430px once the block hits its `max-height` cap. The import controls you were
   about to use are somewhere else by the time the scan finishes.

5. **The one screen built around a user's decision instead of a beets flag is the best UI
   in the app.** The import decision panel has inline old-vs-new field diffing
   (`fieldDiff()`), a similarity score, confidence-driven progressive disclosure, and it
   highlights precisely the fields that separate two near-tied candidates
   (`.candidate-decider`). That is Picard-grade, and it was built here.

**So the work is not a reskin.** It is: give every screen one job, and propagate the
decision panel's design language outward to the rest of the app.

---

## PHASE 1 — Critical

Things that actively hurt usability or accessibility.

- **One long scroll, no fixed reference, and the content moves under you** → Nothing in
  the scrolling area is pinned (M11 = 0), so orientation depends entirely on remembering
  how far you scrolled — and that memory is invalidated the moment a scan injects results
  above your position (M12). → Two native fixes, no framework needed: `position: sticky`
  on `.section-title` so the current section stays named at the top of the viewport, and
  `overflow-anchor` / scrolling the results into view rather than letting them shove the
  page down. Longer term, sections become destinations you navigate to rather than
  distance you scroll past — that's [#36](../../issues/36)'s sidebar territory. → This is
  the orientation half of "overwhelming": load is *how much* is on screen, orientation is
  *not knowing where you are in it*. Fixing density without fixing orientation only gets
  half of it.

- **Inbox — two workflows, three primary buttons, 30 controls** → Make Scan → Import one
  sequential flow with one active primary action at a time; the import configuration
  should not be visible until there is something to import. Collapse "File handling",
  "Import mode" and "Exclude folders" into a summarised, editable line ("Copy · Ask about
  every album · 6 exclusions") that expands on demand → M1 30→≤12, M2 3→1. *This is the
  point where #89 and [#87](../../issues/87) touch; the sequencing note below covers it.*

- **Duplicate checkbox rows with different meanings** → The scan-filter row and the
  exclude-folders row must not be visually identical one screen apart. Give the scan row
  result-scoped framing ("Hide from these results") and the import row permanent framing,
  or fold the scan filter into the results header where it acts. → the exact defect #89
  was opened over, still present in a second form.

- **Copy/Move/Keep exists in two places** → One source of truth. Keep it in the import
  flow (that's where the decision is made) and have Preferences link to it, or vice
  versa — but not both. → M8 2→0. Two controls for one setting is how a user learns not
  to trust either.

- **Light theme never redefines `--accent`** → In light mode the **active** tab is
  2.04:1 — the gold was tuned for a near-black background and reused unchanged on cream.
  ~~Inactive tabs measured 5.04:1~~ **Correction, found while fixing #104:** that 5.04:1
  reading was a transition-timing artifact — `.tab-btn` carries a 0.1s `color` transition,
  and reading `getComputedStyle` synchronously right after a theme switch caught it
  mid-flight on the *previous* theme's color. The real figure is **3.21:1**, which also
  fails AA — it's `--text3`, not `--accent`, and used in ~20 other de-emphasized-text
  spots, so raising it is a separate, broader call than this one token pair. Fixed here:
  active-tab legibility and the hierarchy inversion (2.04→5.65, now correctly the most
  legible item). Not fixed here, flagged separately: `--text3`'s own AA failure. → M5.

- **Scan result rows hide the only useful part of the path** → Rows put the full absolute
  path in one `text-overflow: ellipsis` line with **no `title` attribute**, so
  `…/incoming/Burial - Untrue` truncates to `/private/tmp/claude-501/-Users-valdefa…`.
  The album name — the entire reason you're looking at the row — is invisible and
  unhoverable. → Show the leaf folder as the row title with the parent path secondary,
  and add `title` for the full path.

- **Zero `aria-live` regions** → Every meaningful surface in this app is asynchronous
  (SSE import stream, job progress, scan results, artwork/convert jobs) and none of it is
  announced. → M6. An import can run for a long time with no accessible signal that
  anything is happening.

- **Focus is effectively invisible** → 2 `:focus` rules and 0 `:focus-visible` across 117
  buttons. → Single tokenised focus ring applied globally. Keyboard operation of this app
  is currently guesswork.

**Why first:** every item here is either a correctness bug (inverted contrast hierarchy,
hidden path, duplicated setting, content displaced under the cursor) or an access barrier,
not a matter of taste. Orientation and load lead because between them they *are* the
"overwhelming" the author reports — and orientation is the cheaper half to fix.

---

## PHASE 2 — Refinement

Below professional standard, but not actively broken.

- **ALL-CAPS applied to content, not just labels** → 12 `text-transform:uppercase` rules.
  Caps on a section title is a typographic device; caps on
  `AUTO-ACCEPT CONFIDENT MATCHES, SKIP THE REST (RECOMMENDED FOR LARGE IMPORTS)` is a
  sentence shouted at 11px, and caps measurably slow reading of running text. → Keep caps
  for section titles and field labels only; radio/checkbox option text and help text go
  sentence case.

- **Library has 21 buttons and no subject** → Its primary-styled button is "Export",
  which is not what the Library tab is for, and six collapsed maintenance sections sit
  under the list. → Lead with the music: rows get artwork, actions attach to selection,
  maintenance operations move behind one "Maintenance" affordance. *Overlaps
  [#36](../../issues/36) — coordinate rather than duplicating.*

- **No album artwork anywhere** → The only `<img>` in 3,285 lines is the app icon. This is
  a music library manager; Picard devotes a whole pane to new-vs-existing cover art. →
  Artwork in library rows and in the decision panel (where "is this the right release?"
  is precisely the question being asked). → M10. *Needs a server endpoint to serve art —
  functional change, flagging for the build agent rather than assuming it.*

- **Notes doing the work controls should do** → 50 note boxes; Export at 57% explanatory
  words. → For each, decide: encode the rule in the control, or move it to the existing
  `ℹ` tooltip pattern, or keep it if it genuinely warns of data movement. The Traktor
  five-step procedure is the extreme case and probably belongs in README, not in the tab.
  → M3, M4.

- **111 inline `style=` attributes** → A token system exists and is bypassed 111 times,
  which is why vertical rhythm drifts between sections. → Move to classes; introduce the
  spacing scale below. → M9.

**Why second:** these are the difference between "works" and "looks considered", but none
of them block a user or lock anyone out. They also get cheaper once Phase 1 has settled
the layout.

---

## PHASE 3 — Polish

Functional already; not yet premium.

- **No press feedback on any button** → 0 `:active` rules across 117 buttons. →
  `transform: scale(0.97)` with a ~160ms ease-out. Single cheapest change in this
  document for how responsive the app feels.

- **18 `:hover` rules ungated on a touch target** → The app ships
  `apple-mobile-web-app-capable` and is intended as a Safari Web App, so on iPad/iPhone
  hover states stick after tap. → Wrap in `@media (hover: hover) and (pointer: fine)`.

- **Two infinite animations, no reduced-motion escape** → `job-pulse` (1.6s, infinite)
  runs continuously for the entire duration of any job, and `dropPulse` while dragging;
  `prefers-reduced-motion` appears 0 times. → Guard both; keep opacity cues, drop the
  loop.

- **No easing vocabulary** → All nine transitions use the browser default curve. → One
  `--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` token applied to entrances; keep the
  existing 100–150ms durations, they're already correctly snappy.

- **No loading or skeleton states** → One "loading" string in the whole file; lists appear
  fully-formed or not at all. → Skeleton rows for library/scan results, and
  `@starting-style` entry on decision cards and scan rows so results don't pop in.

- **Empty states are bare strings** → Recover's "Nothing scanned yet — hit Scan above."
  is the app's *best* empty state and it's still one line of grey text. → Give the
  primary empty states a heading, one sentence, and the action inline.

**Why third and what it adds up to:** none of this changes what the app can do — it
changes whether it feels like it's responding to you. Phase 1 makes it usable, Phase 2
makes it look deliberate, Phase 3 is where it stops reading as beta.

---

## Design system updates required

The design system is the `:root` block in `beetsgui.html` — there is no `DESIGN_SYSTEM.md`
and, for a single-file no-build app, there shouldn't be. These are token additions to that
block.

- **Per-theme accent.** `--accent`/`--accent2` are currently defined once (for dark) and
  reused in light. Add light-theme values with ≥4.5:1 against `--bg`. Suggest
  `--accent:#7a5c1e` / `--accent2:#5d4614` for light as a starting point — must be
  re-measured, not trusted.
- **A spacing scale.** None exists; spacing is ad-hoc rem values plus 111 inline styles.
  Add `--space-1…6` (0.25/0.5/0.75/1/1.5/2.5rem) and use them.
- **A focus ring.** `--focus:0 0 0 2px var(--bg), 0 0 0 4px var(--accent)` applied via one
  global `:focus-visible` rule.
- **One easing token.** `--ease-out: cubic-bezier(0.23, 1, 0.32, 1)`.
- **Primary-button foreground.** Current pairing is 3.71:1 (light) / 3.79:1 (dark) against
  `--accent2` — both fail AA. Either darken the accent or set an explicit high-contrast
  foreground token.

---

## Implementation notes for the build agent

Only the unambiguous, self-contained ones are listed. Phase 1's layout items are design
work, not find-and-replace, and shouldn't be reduced to a diff here.

- `beetsgui.html` `:root` — add the five token groups above.
- `beetsgui.html` — add `html[data-theme="light"]` and the `@media (prefers-color-scheme:light)`
  block: `--accent` and `--accent2` overrides. Both blocks, or the OS-preference path
  keeps the broken values.
- `.btn`, `.btn-primary`, `.tab-btn`, `.prefs-btn`, `.prefs-close`, `.job-panel-dismiss` —
  add `transform 160ms var(--ease-out)` to the existing transition list and a
  `:active { transform: scale(0.97); }` rule.
- Global — one rule: `:focus-visible { outline:none; box-shadow: var(--focus); }`. Remove
  the two ad-hoc `:focus` rules it supersedes.
- All 18 `:hover` rules — wrap in `@media (hover: hover) and (pointer: fine)`.
- `.job-pulse`, `.drop-target.drag-over::after` — wrap the animation declarations in
  `@media (prefers-reduced-motion: no-preference)`.
- Scan result row (`renderScanResults`, `.lib-row-title`) — add
  `title="<full path>"`; render leaf folder name as the row title, parent path as
  `.lib-row-meta` beneath.
- Job/import status containers (`#import-decision`, job panel result wrap, scan results) —
  `aria-live="polite"`, and `aria-live="assertive"` on the error/lost-connection surface
  added in `c659a9e`.
- `.section-title` — `position: sticky; top: 0; z-index: 5;` plus a `background: var(--bg)`
  (currently transparent, so it would render over content while pinned). `details.section
  summary.section-title` inherits this; verify the disclosure marker still aligns.
- `.content` — `overflow-anchor: auto` (browser default, but currently defeated by the
  results block being inserted rather than revealed); alternatively call
  `scrollIntoView({block:'nearest'})` on `#scan-unimported-results` after render so the
  results come to the user instead of displacing them.
- Radio/checkbox option labels — remove `text-transform:uppercase` from `.check-item` /
  `.radio-item`; keep it on `.section-title` and `label`.

**Testing:** `test_smoke.py` is the only suite that exercises real DOM/JS. Per CLAUDE.md,
UI behaviour changes extend that suite rather than the API-level tests. Contrast and
control-count regressions are cheap to assert there directly — the Appendix A snippet runs
unchanged in Playwright.

---

## Sequencing against #36 and #87

These three issues collide in exactly one place: the Inbox rework.

- **Do Phase 1's non-layout items now** — contrast, focus, `aria-live`, scan-row
  truncation, duplicate settings. They are independent of any IA decision and stay correct
  whatever #36 and #87 conclude.
- **Do not land the Inbox Scan→Import restructure ahead of [#87](../../issues/87).** #87
  owns whether a flat page is the right shape for multi-step flows; M1/M2 would be
  re-solved twice otherwise.
- **The Library rework belongs with [#36](../../issues/36)**, not here — "21 buttons, no
  subject" and "stop exposing beets' command surface" are the same problem seen from two
  sides.
- **Phase 3 is independent of all of it** and can land any time.

---

## Deliberately not decided

- **No new visual direction.** The type and colour choices are good and specific; nothing
  here proposes replacing them. Every colour change listed is a contrast fix with a
  measured reason.
- **No framework, no build step.** CLAUDE.md is explicit that there isn't one, and nothing
  in this plan needs one.
- **Artwork requires a server endpoint** to serve images out of the library. That's a
  functional change and is flagged, not assumed.

---

## Appendix A — re-running the baseline

Paste in the browser console with the app open, or run via Playwright in `test_smoke.py`.
Returns M1–M4 per tab.

```js
{
const vis=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
 return s.display!=='none'&&s.visibility!=='hidden'&&(r.width>0||r.height>0)
 &&!e.closest('details:not([open])')&&!e.closest('.tab-panel:not(.active)')
 &&!e.closest('dialog:not([open])')};
const out={};
document.querySelectorAll('.tab-btn').forEach(b=>{
 b.click();
 const p=document.querySelector('.tab-panel.active');
 const all=[...p.querySelectorAll('button,input,select,textarea,summary,[onclick]')].filter(vis);
 const notes=[...p.querySelectorAll('.alert')].filter(vis);
 const words=p.innerText.trim().split(/\s+/).length;
 const noteWords=notes.reduce((n,e)=>n+e.innerText.trim().split(/\s+/).length,0);
 out[b.textContent.trim()]={controls:all.length,
  primary:[...p.querySelectorAll('.btn-primary')].filter(vis).length,
  collapsed:[...p.querySelectorAll('details')].filter(d=>!d.open).length,
  notes:notes.length, pctWordsExplaining:Math.round(noteWords/words*100)};
});
console.table(out);}
```

Contrast (M5) — swap `data-theme` and compare `getComputedStyle` colour against the
nearest opaque ancestor background; the full snippet used for this audit is in the #89
thread.
