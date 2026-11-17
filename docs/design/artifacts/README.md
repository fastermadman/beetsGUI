# Product vision and mockups (static HTML)

Exports of the claude.ai artifacts that were only linked from [#150](https://github.com/fastermadman/beetsGUI/issues/150) / [#151](https://github.com/fastermadman/beetsGUI/issues/151), so they live in git. Open any `.html` file in a browser; nothing needs a server or the network (fonts are embedded).

**Status: historical reference, not approved designs.** The decisions that drive code are in the design documents one level up. If something in here starts to drive code, copy the decision into a document there.

| File | What | Language | Source artifact (last edited) |
|---|---|---|---|
| [vision.html](vision.html) | Product vision: experiences, Atlas / set builder, "you teach it" enrichment | da | «beetsGUI: produktvision» (2026-10-12) |
| [lexicon-mixo.html](lexicon-mixo.html) | What Segue should borrow from Lexicon and MIXO | da | «Lexicon og MIXO: hvad Segue skal låne» (2026-10-13) |
| [mockups/index.html](mockups/index.html) | 19 UI mockup boards (+ "broken engine" variant): Library, Add music, Health, History, Atlas, set builder, first run, Listen through, New in, set ETA | en | «beetsGUI mockups» design canvas (2026-11-07) |

## How the mockups were exported

Each canvas board was rendered in headless Chromium and its DOM saved as plain HTML/CSS (Jost and DM Mono embedded, no JavaScript, links between boards rewritten to the sibling files). Compared pixel by pixel with the live boards: only anti-aliasing differences. What is lost: the canvas itself (layout, row titles, pages) and the editor's tweak props; the one prop that changed content (engine "missing" / "broken") was exported as two files. The editable canvas stays in claude.ai.

## Not exported here

The app shell plan (#140) and the track-facts data model were also artifacts; their approved versions already live in git as [app-shell-140.md](../app-shell-140.md) and [track-facts-data-model.md](../track-facts-data-model.md).
