# CLAUDE.md — ui-prototype

Read the root `CLAUDE.md` first (safety rule: never touch `~/.config/beets` or `/Volumes`). This folder is a React + Tailwind prototype of the Segue UI for Onlook; see `README.md` here and issue #178.

- Visual reference: `../docs/design/artifacts/mockups/`. Decisions: `../docs/design/README.md`.
- UI text is English. Function components + hooks only, so it can alias to `preact/compat`.
- Do not change files outside `ui-prototype/`. No calls to the Flask server; use mock data.
- Verify with `npm run build`.
