# ui-prototype — Segue UI in React + Tailwind (for Onlook)

A **throwaway, self-contained** prototype for trying Segue's UI visually in [Onlook](https://docs.onlook.com). It is *not* part of the shipped app: nothing in the repo imports it, and it does not talk to `server.py`. Work is tracked in [#178](https://github.com/fastermadman/beetsGUI/issues/178).

Stack: Vite 8 + React 19 + TypeScript + Tailwind 4. Written in React-compatible style so it can later alias to `preact/compat` (see `docs/design/ui-stack.md`, PR #177).

## Run it

Needs Node 20.19+ or 22.12+.

```
cd ui-prototype
npm install
npm run dev        # http://localhost:5173
npm run build      # type-check + production build
```

## Open in Onlook (macOS)

1. `git clone` the repo (or use your existing checkout) and make sure this folder is on the branch/`master` you want.
2. In Onlook: **Add codebase** and pick the **`ui-prototype`** folder (it has its own `package.json`, so it works as a project root on its own).
3. If Onlook asks which app/port to use, choose the Vite app on **5173**.

Onlook's docs do not say whether a subfolder of a repo is supported as a project root, or how it picks the port; if it fails, record what happened in #178.

## Where the design lives

- Approved designs and decisions: `docs/design/README.md` (start there; do not diverge silently)
- UI mockups to copy from: `docs/design/artifacts/mockups/index.html` (Add music = `Tilfoj`, Collection = `Main`, Health = `Sundhed`, History = `Historie`, Sound map = `Lydkort`/`LydkortSaet`)
- Product vision: `docs/design/artifacts/vision.html`; app-shell plan: `docs/design/app-shell-140.md`
- The app UI is **English**; the mockup file names are Danish.

## Rules for this folder

- Do not edit `beetsgui.html`, `server.py` or anything outside `ui-prototype/`.
- Function components and hooks only; no React-only libraries; mock data in `src/data/`.
- Colours and fonts as tokens in `src/index.css` only.
