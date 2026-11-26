---
status: draft
language: en
issues: [150, 139, 140]
---

> **Draft recommendation, not approved.** Written 2026-11-26. Versions were read from the npm registry that day; nothing below was run in a spike, so every "works via compat" claim is unverified until a PR proves it. Index: [README.md](README.md).

# UI stack and Rust fit

Question: instead of hand-building everything, which frameworks and libraries should Segue's UI stand on, and does Rust belong anywhere?

## Facts as of 2026-11-26

- The backend on `master` is Python only: Flask `server.py` plus `libops`, `jobs`, `importsession`, `traktor`, `transcode` and others. Beets is the engine (decided earlier). There is no Rust, no `Cargo.toml` and no `package.json` in the repo.
- The UI on `master` is still the single `beetsgui.html`. The Preact port is not on `master` or on any pushed branch yet.
- The pywebview shell (`app.py`) lives only in draft PR #136.

## Recommendation

| Need | Choice | Why / caveat |
|---|---|---|
| Components | **Preact 11** (11.2.0) | Small, `preact/compat` is part of the package. |
| Build | **Vite 8 + `@preact/preset-vite` 2.10 + TypeScript** | Supersedes "no build step" in `CLAUDE.md` once the port lands; update that file in the same PR. |
| State | **`@preact/signals` 2.11** | Fits the app shell's "no store" rule ([app-shell-140.md](app-shell-140.md)): signals for player, job and selection, nothing global beyond that. |
| Big tables | **`@tanstack/preact-table` 9.3** with **`@tanstack/virtual-core` 3.19** | Table has an official Preact adapter. Use virtual-core directly, not `react-virtual`, to avoid compat. Needed for collections of 40k+ rows. |
| Styling | **Tailwind 4.3**, or plain CSS variables | Either is fine; pick one before the first view is ported and keep design tokens in one place. |
| Dialogs, popovers | Native `<dialog>`, the popover API, **`@floating-ui/dom` 1.8** | Covers most of the app with no component library. |
| Menus, comboboxes | Radix (`@radix-ui/react-*` 1.x) via `preact/compat` **only if needed** | Peer-depends on React; may work through compat but is unverified. Spike one component before committing. |
| Audio waveform, regions | **wavesurfer.js 8.1** | Framework-agnostic, BSD-3. |
| Cue lanes, key-over-time, energy curves | Own **canvas 2D** | Too specific for a library; keep each as a small module with a plain-data input. |
| Sound map | **canvas 2D + `d3-scale` 4**; **`regl` 2.1** (WebGL) only if profiling shows a need | Thousands of points are fine in 2D canvas. |
| Router | Keep the hash router from the shell plan | `preact-iso` (2.12) is the fallback if it grows. |

All listed packages are MIT, ISC, BSD or Apache-2.0, so they do not constrain an AGPLv3 release.

## Rust

Recommendation: **do not migrate; no Rust now.**

- The audio stack (Essentia, librosa, pedalboard, Rubber Band) is native code under Python. Rust would not make it faster and would lose the bindings.
- Beets is Python and is the engine; `segue.db`, the `sg_*` fields and the `sg_set` query all sit on top of it ([track-facts-data-model.md](track-facts-data-model.md)). A Rust core would have to re-implement or shell out to beets.
- **Tauri instead of pywebview:** no gain while the backend is Python (both use WKWebView). Revisit only if the backend is packaged as a sidecar.
- **Possible later, measured first:** a small standalone Rust engine for real-time mixing or preview with low latency, or a `pyo3` module for one proven hot loop. Neither is needed before there is a profile showing Python is the bottleneck.

## Next steps

1. Push the Preact port so the repo matches this document, and update `CLAUDE.md` (no build step) in that PR.
2. Spike one Radix component and one TanStack table on 40k rows before adopting either.
3. Re-read this document when the sound map or mixtape render ([#175](https://github.com/fastermadman/beetsGUI/issues/175)) reaches implementation.
