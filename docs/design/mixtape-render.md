---
status: draft
language: en
issues: [175, 164, 173, 160, 156, 159, 169, 174]
supersedes: "#175 (scope and tasks; the issue is to be rewritten from this document once approved)"
---

> **Draft, not approved.** Design only: no code, no prototype, no issue has been changed. If the user approves it, it becomes the source of truth for the mixtape render; until then nothing here binds the build.
> Builds on [track-facts-data-model.md](track-facts-data-model.md) (cues, grids, `sg_set`, change log, `analysis.db`) and does not change it except where "Changes to the approved designs" says so. Index: [README.md](README.md).
> UI-framework independent on purpose (the platform is being ported from vanilla JS to Preact): everything below is a server-side engine plus JSON contracts. Section "What the UI needs" lists behaviour, not components.

# Mixtape render: set list, transitions, automation, master chain, bounce

2026-11-26 · @FasterMadman

## What this is

The user wants to build a set list, choose where each transition happens, **hear the finished mix without sitting through the tracks**, and finally **bounce** it with a mastering chain on the master bus. Where a DJ would ride faders live, the user draws or records **automation** per transition, like in Ableton, and the app renders offline.

Four pieces, in build order:

1. **Mix plan.** Ordered tracks plus one transition between each neighbouring pair: an out point on A, an in point on B, an overlap length in bars, and a tempo policy. All positions are in bars on the winning beat grid.
2. **Automation lanes** per transition: volume, EQ, filter, effect sends, drawn or recorded, with templates (bass swap, filter fade, echo out, ...).
3. **Offline engine.** Decode, tempo-match, apply automation, sum, run the master chain, write audio. The same code makes a 45-second transition preview and the full bounce.
4. **Master chain.** Built-in effects first; third-party VST3/AU plugins on the master bus (and optionally per deck) via [Spotify Pedalboard](https://github.com/spotify/pedalboard).

This replaces the scope of #175 (single-transition preview). A preview is simply a short bounce, so one engine serves both.

## Principles

- **One truth, two lengths.** Preview and bounce run the same plan through the same engine. What you hear in the preview is what the bounce contains, plugin for plugin. Where a faster approximation exists for live editing (see "Hearing it"), it is labelled as a draft.
- **Bars, not seconds.** Every position (anchors, overlap, automation breakpoints) is stored in bars/beats relative to a grid. A corrected grid or a changed tempo moves everything consistently. Seconds are derived at render time.
- **Never touch the library.** The engine reads audio files and writes only to the render cache and the user's chosen output folder. It does not write tags, does not import the bounce into beets unless the user asks, and never reads or writes `~/.config/beets` or `/Volumes` in tests.
- **Own process.** Rendering never runs inside beets' `Library` or the `jobs.py` single-flight (same reasoning as `analysis.db`'s separate process in the approved data model). A plugin that crashes takes down the render worker, not the app.
- **Your work is data.** Plan, lanes and chain state are rows in `segue.db`, written through `facts.put`-style functions and the change log, so they are undoable and part of the backup.

## Fit with the track-facts design

| Need | Where it already lives | What this design adds |
|---|---|---|
| Ordered set list | `sg_set` fixed list with order (#160; "Mixes" #155) | `transitions` rows keyed by set + position |
| Transition anchors | `cues` roles `mix_in` / `mix_out` per layer (`traktor`, `auto:<template>`, `segue`); the 16-cue template (#173) gives more candidates (drops, breakdowns) | Anchors stored **per transition**, as an offset from a cue or a bar number on the grid. Moving a transition does not move the track's cues |
| Beat grid | `grids` with winning source per track (tier rules) | Resolved at render time; the plan stores bars, not times |
| Loudness, sections, keys | `analysis.db` blobs `loudness`, `sections`; #165/#166 segments and key over time | Per-track gain trim from `loudness`; mix score (#169) shown on each transition |
| Undo | `change_sets` / `change_rows` | Lane edits and chain changes are rows there; one drawing gesture is one `change_set` |
| Track identity | `track_uid` never leaves the server | Client talks beets item ids and set ids only |

**Promoting a transition choice.** "Use as this track's Mix out" writes a `segue`-layer cue (tier `you`) through the normal path. It is an explicit action, because a transition point that works against one particular next track is not necessarily the track's general mix point.

### Schema sketch (goes into the data-model doc and a numbered migration when built)

All in `segue.db`; names are proposals.

| Table | Columns (essentials) |
|---|---|
| `mix_sets` | `set_id` (= the `sg_set`), `sample_rate`, `target_lufs`, `chain_id`, `output_dir`, `updated_at`. Optional row: a plain `sg_set` is a mixtape only if it has one |
| `transitions` | `set_id`, `position` (index of track A), `out_anchor` and `in_anchor` as (`ref_cue_id` or null, `bars_offset`, or absolute `bar`), `overlap_bars`, `tempo_mode`, `tempo_param`, `nudge_ms`, `template` (provenance only), `automation` (JSON, below), `updated_at` |
| `mix_track_gain` | `set_id`, `track_uid`, `trim_db` (default from `loudness`, user-adjustable) |
| `chains` | `chain_id`, `scope` (`master` or a deck bus), `devices` (JSON, ordered: `{kind: builtin|vst3|au, id, path, version, bypass, state_blob_id}`) |
| `blobs` (in a small table of its own) | `raw_state` of third-party plugins, content-hashed |
| `renders` | `set_id`, `kind` (`preview` or `bounce`), `range`, `plan_hash`, `path`, `duration`, `lufs`, `true_peak`, `status`, `created_at` |

`plan_hash` hashes everything that changes the audio: the ordered `audio_sig`s, resolved grid per track, anchors, tempo policy, all lanes, chain devices with `raw_state` hashes and plugin versions, sample rate. Same hash means a cached render is valid; any edit invalidates exactly the affected clips (see "Caching").

## The timeline and tempo matching

**Mix grid.** The mix has one master beat clock. Track N's source grid is mapped onto it with a **warp map** (source beat to mix beat). Everything else (automation, effect delay times, cue display) is expressed on the mix clock.

**Anchors.** A transition aligns A's out anchor with B's in anchor on the same mix bar and runs for `overlap_bars`. Both anchors must be on bar lines of their track's grid; the editor snaps to 4/8/16/32-bar phrases like auto cues do, but a free bar position is allowed (the user is in charge). `nudge_ms` exists for tracks whose grid is audibly off; it is the escape hatch, not the normal path.

**Tempo policy per transition** (`tempo_mode`):

| Mode | Behaviour | Default for |
|---|---|---|
| `hold_a` | B is stretched to A's tempo for the overlap, then ramps to B's native tempo over `tempo_param` bars (default 8) after A has left | BPM difference within tolerance |
| `ramp` | Tempo glides from A's to B's over the overlap | Large difference, club genres |
| `meet` | Both move to the midpoint during the overlap, then each returns to native | Optional |
| `native` | No stretching; overlap is a cut/echo/FX style transition | When BPM is incompatible (a "bridge" transition) |

- **Lock tolerance** (default 6 %, a setting): beyond it the plan marks the transition "tempo stretch large" and recommends `native` or half/double time. The mix score (#169) already carries a tempo term; the badge reads it.
- **Half/double time** is a per-transition toggle so 70 BPM against 140 BPM can match without a 2x stretch.
- **Variable-tempo tracks** use the full beat list from the `analysis` grid, not a constant BPM: the warp map is piecewise.
- **Stretch engine and key lock.** Stretching keeps pitch (key lock) by default. The candidate is Pedalboard's own Rubber Band based `time_stretch` (Rubber Band is GPL, compatible with the planned AGPLv3). **To verify in the spike:** quality at 3-6 %, CPU cost, that a pitch shift option exists for the "harmonic nudge" some DJs use.
- **Outside the overlap**, a track plays at its native tempo, except in `hold_a`'s return ramp. So a track B that is matched to A during the overlap and then returns to its native tempo makes the whole mix drift in tempo the way a human DJ's does, rather than being forced to one BPM for the whole tape. A global "everything at one BPM" mode is a possible later setting.

## Automation lanes

A lane is a list of breakpoints on the mix clock, relative to the transition start (beats, so tempo changes and grid corrections do not break it).

```json
{
  "lanes": {
    "A.volume": {"points": [[0, 1.0], [64, 0.0, "exp"]], "default": 1.0},
    "A.eq.low": {"points": [[32, 1.0], [32, 0.0, "step"]]},
    "B.filter": {"points": [[0, 0.0], [48, 1.0, "lin"]]},
    "B.send.delay": {"points": [[56, 0.0], [60, 0.4], [64, 0.0]]}
  }
}
```

Values are normalised 0-1 and mapped to real parameter ranges in the engine, so lanes are portable across effect implementations. Shapes between points: `lin`, `exp`/`log` (for volume and frequency), `step`, `smooth`.

**Lane targets** (per deck A and B, plus the transition-wide):

| Group | Targets |
|---|---|
| Level | `volume` (the crossfader/channel fader equivalent) |
| EQ | 3-band `low`, `mid`, `high` with kill at 0 (DJ-style isolator, not a mastering EQ) |
| Filter | one DJ filter per deck: 0.5 = open, below = low-pass sweep, above = high-pass sweep (single knob, like a hardware mixer) |
| Sends | `reverb`, `delay` / echo (time in beats, feedback fixed per preset), each a post-fader send into a shared effect bus |
| Transition-wide | optional master-bus sweep or a "riser" built-in; limited to built-ins |

**Templates** copy lanes into the transition (editable afterwards; `template` is only provenance, so changing a template later never rewrites a user's transitions):

| Template | What it does |
|---|---|
| Equal-power crossfade | Volume only, the baseline |
| **Bass swap** | A `low` and B `low` swap on a bar line (hard step) at a chosen bar, volumes overlap before and after |
| **Filter fade** | A filter sweeps closed while B's sweeps open, volume lanes follow |
| Echo out | Delay send on A rises, A volume cuts on the bar, tail rings over B's intro |
| Long blend | 32 bars, gentle EQ trade over the whole overlap |
| Cut on the one | Hard cut at a bar line, optional reverb throw |
| Mix in a drop | B enters on its `drop` cue (uses the 16-cue template when present) |

Templates are parametrised by `overlap_bars` and a "swap point", and generated by pure functions, so they can be unit-tested without audio.

**Recording like Ableton.** Two input modes, same storage:

1. **Draw**: add, move and delete breakpoints on a lane, with bar-snapped grid (shift for free). The primary mode and the only one needed for v1 of lanes.
2. **Record (touch/latch)**: a transition loop plays in a monitor (see next section), the user moves on-screen faders, and the app writes breakpoints with beat timestamps. A "simplify" pass thins the points. MIDI controller input is parked with #175's parking decision; recording goes through whatever input the UI offers, and the engine only sees breakpoints.

Automation is **evaluated per audio block** (for example 256 samples) with a short smoothing ramp on level, EQ and filter parameters to avoid zipper noise. This is the point where tempo and automation meet: lanes are evaluated on the mix clock after the warp map, so a lane drawn at bar 8 stays at bar 8 whatever the stretch.

## Hearing it

This is the hardest part and the place where the design gives up some things honestly.

| Mode | How | Latency to hear an edit | Fidelity |
|---|---|---|---|
| **Block-streamed render (the default)** | Server renders the region in blocks (about 8 bars) ahead of the playhead with the real engine and chain; the client plays what has been rendered through an ordinary audio element or buffer | 1-3 s, since the affected blocks must re-render | Exact: same engine as the bounce, including plugins |
| **Draft monitor (optional)** | The client approximates volume, 3-band EQ, filter, delay and reverb sends with the browser's own audio graph, over decoded excerpts of the two tracks | Instant | Approximate; no third-party plugins; labelled "draft" |

Why both: drawing lanes does not need low latency (re-render after the edit is fine), but **recording moves** does, and plugins cannot run in a browser audio graph. The recommendation is to build the exact mode first (it is needed for bounce anyway), add the draft monitor only if recording turns out to feel bad against a 1-3 s lag, and never let the draft claim to represent the master chain.

**Why not a live plugin host** (stays rejected, as in #175): a real-time audio thread inside a Python server behind a web view, plugin crashes, macOS signing and sandboxing, and the maintenance cost, for little gain over fast block rendering. If block rendering is too slow on the user's Mac, that is a measured finding for the spike, not a reason to start with a host.

**Scrubbing and tails.** Starting playback at any bar needs a **pre-roll** (render a few bars before so reverb/delay tails and compressor state are realistic). The first block after a seek is slightly less exact; the bounce is always exact.

## Master chain and plugins

- **Built-in devices** (Pedalboard's own effects: EQ shelves/peak, compressor, limiter, gain, reverb, delay, filters) cover the DJ-mixer parts and a basic master (gentle glue compressor + limiter) with **no third-party code**. Segue must be usable without any VST.
- **Third-party VST3 / AU** can be inserted in the **master chain** (ordered) and optionally in a deck bus. Discovery scans the standard folders (`~/Library/Audio/Plug-Ins/VST3`, `/Library/Audio/Plug-Ins/VST3`, `.../Components`); the result is cached with plugin name, version and format.
- **Parameters.** Pedalboard exposes a plugin's parameters by name with range, unit and display text. Segue builds a generic panel from that list (sliders, switches, choice menus), in its own UI.
- **Plugin editor (to verify).** Recent Pedalboard versions are documented to offer `show_editor()`, which opens the plugin's own native window. If that holds up, an **"Open plugin window"** button gives the real UI for fine tuning. Constraints: it must run on a process's main thread and blocks while open, and the Mac app's main thread belongs to the web view. So the design uses a **small editor helper process** that opens the plugin, waits for the window to close, and writes back the plugin's `raw_state` (opaque preset blob) to `segue.db`. This overrides the earlier note that Pedalboard has no plugin GUI. The spike must confirm this on the user's macOS version and with the plugins they actually use. **Fallback if it does not work:** the generic parameter panel only, plus import of a preset file saved from the user's DAW.
- **State.** The chain stores each plugin's `raw_state` (if available) rather than a parameter list alone, so patches with internal state restore exactly. Parameter values alone are the fallback and are shown as such.
- **Isolation.** The render worker is the sandbox: a plugin that crashes or hangs (timeout per block, per plugin) ends that render with "Plugin X failed at bar N", the app stays up, the chain marks the plugin as suspect. Plugin **scanning** also runs in a subprocess for the same reason.
- **Limits to state plainly in the UI:** plugins with demo-mode noise or licence dialogs behave as they would in a DAW; plugins built only for Intel will not load into an Apple-silicon process (listed as "incompatible", not hidden); sample-based instruments and sidechain-dependent plugins are out of scope (effects only); plugins with large look-ahead need **latency compensation** (the engine reads reported latency and delays the dry path or trims the tail), else a limiter's look-ahead shifts the master against the clock.
- **Determinism.** A plugin with random modulation can render differently twice; `plan_hash` identifies the plan, not bit-identical output. Acceptable and mentioned in the UI for bounce.

## Rendering pipeline

1. **Plan** (pure Python, unit-testable without audio): resolve each transition to absolute mix-clock positions from winning grids and cues; compute warp maps; evaluate templates/lanes into sorted breakpoint arrays; produce the `plan_hash`.
2. **Decode** each needed span (not whole files for a preview) with `ffmpeg` (already a dependency) to float32 at the mix sample rate (44.1 or 48 kHz; 48 kHz default only if the user's collection leans that way, setting).
3. **Per deck**: trim gain (from `loudness`), stretch by the warp map, DJ EQ and filter driven by lanes, sends into the shared effect bus.
4. **Sum** decks and effect bus to the master bus.
5. **Master chain**: built-ins and plugins in order, in blocks, with latency compensation, a tail allowance at the end (reverb/delay/limiter release).
6. **Measure and write**: integrated loudness (EBU R128) and true peak reported back; output WAV/FLAC/AIFF, optional MP3 320 or AAC via `ffmpeg`. Sample-rate conversion and dither only at the final step.
7. **Sidecars**: a tracklist with start times (for the user's use, and a CUE sheet or chapter markers where the format allows) written beside the audio.

**Caching.** Preview clips are keyed by `plan_hash` plus range and live in the cache folder (`~/Library/Caches/beetsGUI/renders`, expendable). Editing one transition invalidates only clips whose hash covers it. Bounces go to the user's chosen output folder, are never auto-deleted, and are recorded in `renders`. Cleanup: preview clips are pruned by age and total size.

**Performance targets** (goal metrics below) assume Pedalboard's speed on Apple silicon; stretch is likely the dominant cost.

## Server shape (sketch)

Follows the existing rules: `scope`/ids in, Host and Origin guards inherited, no `shell=True`, client never sees `track_uid` or paths it chooses itself.

| Endpoint (sketch) | Purpose |
|---|---|
| `GET/PUT /mix/<set_id>` | Plan summary, sample rate, chain id, target loudness |
| `GET/PUT /mix/<set_id>/transitions/<pos>` | Anchors, overlap, tempo mode, lanes |
| `POST /mix/<set_id>/transitions/<pos>/template` | Apply a template (returns the new lanes) |
| `POST /mix/<set_id>/render` | `{range, kind}` starts a worker; returns a render id |
| `GET /mix/render/<id>` / `/audio` | Progress, result, audio with range requests for the player |
| `GET /plugins`, `POST /plugins/scan` | Known plugins and incompatibilities |
| `POST /mix/chain/<id>/editor` | Opens the plugin window through the helper (if verified) |

**Jobs.** The render worker is not beets work and does not take the single-flight lock. It still wants a place in the dock's job panel (#140), so `GET /jobs/current` should list it as a non-exclusive job kind. How exactly is an open question for #140's view contract, see below.

## What the UI needs (behaviour, not components)

- **Set view**: the ordered tracks as a lane with a transition marker between each pair; per transition a badge with the mix score (#169), tempo ratio, and a play button.
- **Transition editor** for one pair: both waveforms on one bar ruler, draggable out/in anchors snapping to phrase bars, overlap length, tempo mode, a lane stack (one row per target, show/hide), template picker, play/stop/loop, "render preview" state.
- **Lane editing** as a pure data interaction: add/move/delete breakpoint, choose shape, snap setting. The component only needs the lane JSON in and out, so it carries over to any framework.
- **Chain view**: ordered devices, bypass, parameter panel, "Open plugin window" where supported, incompatible plugins listed.
- **Bounce dialog**: format, loudness target, output folder, tracklist sidecar, progress, result with measured LUFS/true peak.

All of these talk to the endpoints above with plain JSON; no component state leaks into the engine contract.

## Build order and relation to existing issues

| Step | Content | Depends on |
|---|---|---|
| **0 Spike** | Verify on the user's Mac: Pedalboard version and `time_stretch` quality, load a real VST3 and AU, `raw_state` round trip, `show_editor()` via a helper process, latency compensation, block-render speed, Apple-silicon plugin compatibility. Output: a short findings note, not product code | #174 decision recorded first (Pedalboard is GPLv3, Rubber Band GPL) |
| 1 | Plan model + tables + two-track overlap render with built-ins, tempo policy, 45 s preview. **This replaces #175.** | #159, #160, #156 |
| 2 | Automation lanes, templates, draw editor, block-streamed preview | 1 |
| 3 | Master chain, plugin scan, bounce with loudness report and tracklist | 1, 2, spike |
| 4 | Full-set bounce progress, caching/pruning, output formats | 3 |
| 5 | Record mode and optional draft monitor | 2 |
| 6 | Plugin editor helper (if the spike says yes) | 3 |
| Later | MIDI input for recording, per-deck plugin chains, auto-suggested transitions (from #169 and sections), "add bounce to library" | |

Relation to #164 children: **#173** (16-cue template) supplies candidate anchors beyond Mix in/out; **#169** (mix score) badges each transition and can later pick a default template; **#165/#166** (segments, key over time) let the plan warn when overlap spans a clashing key region; **#172** (stems) would allow true bass-swap on stems instead of EQ kills, and stays parked.

## Licence note

The repository is AGPL-3.0 (see #174). Pedalboard is GPLv3 and bundles JUCE; GPLv3 §13 explicitly allows combining GPLv3 code with AGPLv3 code, and AGPL's network clause then governs the combined work, so the combination is permitted. Rubber Band (GPL) and Essentia (AGPL) are likewise compatible. For a local Mac app, practical consequences are: source must be available to recipients of the app, forks stay AGPL, and distribution through the Mac App Store is awkward with (A)GPL terms. Keeping the option to relicense or dual-license later needs the user to remain sole copyright holder, i.e. a contributor agreement if outside code arrives. This is a design note, not legal advice; the decision stays in #174.

## Goal metrics (for the build issues)

- Grid alignment: on a click-track fixture, the downbeats of A and B coincide within 10 ms after stretch at 0, 3 and 6 % differences.
- Null test: two identical click tracks, neutral lanes, no stretch, produce the expected equal-power sum within -90 dB.
- A 45 s transition preview with built-ins renders in under 10 s on the user's Mac (as #175 said), and a 60-minute, 20-track bounce without third-party plugins in under 10 minutes (to be measured in the spike, then fixed in the issue).
- Lane evaluation is deterministic: the same plan gives identical breakpoint arrays (tested without audio); changing the grid moves anchors and lanes together.
- Cache: editing one transition re-renders only clips that cover it; the `plan_hash` test fails if any audio-relevant input is left out.
- A deliberately bad plugin path and a plugin that raises mid-block both end the render with "Plugin failed", and the server stays up.
- Tests use a throwaway `BEETSDIR` and app data folder, synthesised audio, and never `~/.config/beets` or `/Volumes`.

## Open questions

1. **Latency for recording.** Is a 1-3 s edit-to-ear lag acceptable for drawn lanes (probably yes), and is a browser-based draft monitor worth building for recorded moves (decide after step 2 is felt in use)?
2. **One tempo for the whole tape or drifting?** The design defaults to per-transition matching with native tempo in between. A "one BPM for the whole mix" option is cheap to add; does the user want it as a setting?
3. **Where jobs live in the dock.** A non-exclusive worker listing in `/jobs/current` touches the #140 job facade. Decide there, not here.
4. **Pedalboard `show_editor()` on the user's setup** (spike). It decides between a real plugin window and the generic panel only.
5. **#175's label and status.** It is parked, not run, with `model:opus`/`effort:high`. Once this is approved it should be rewritten as the step-1 issue (and steps 0, 2-6 filed), which needs the user's yes first.

## Changes to the approved designs (when this is approved)

- [track-facts-data-model.md](track-facts-data-model.md): add the tables under "Schema sketch" to `segue.db`, add `mix_*`/`transitions` to the change log's reach, add `renders` to "what lives where" (previews in the cache folder, bounces in the user's output folder).
- `scripts/check_repo.py` already guards headers and links; no new guard needed for this document.
