---
status: approved
approved: 2026-11-17
language: en
issues: [140, 141, 150, 142, 144, 153]
source-artifact: https://claude.ai/artifact/LeWHJxFkXH9JLENe5akRZ8
---

> **Source of truth** for the #140 app shell (router, view contract, legacy coexistence, native drop).
> If code and this document disagree, fix one of them in the same PR. Index: [README.md](README.md).
>
> **Approved decisions (2026-11-17):**
> 1. Jobs and player stay legacy code behind thin facades in #140; ported in #141.
> 2. Minimum macOS is 13 Ventura (README + launch check).
> 3. First launch opens Add music (`#/add`).
>
> **Also settled by the plan:** hash router with lazy `import()` per route; view contract `mount(el, ctx)` with cleanup via `ctx.signal`; legacy `.content` stays unmoved as the "legacy host"; `legacy-bridge.js` is the only file touching legacy globals; no store; `/ui/` sent with `Cache-Control: no-cache`; four PR slices.
> **Open check:** on the Mac, a pywebview handler bound on a parent element must get `pywebviewFullPath` for drops on children (PR 4 of #140).
> **Assumes** #138 and #139 have landed. Nothing in this document is built yet.
> **Figures:** the shell-layout figure exists only in the original document (see `source-artifact`).

# App shell plan (#140)

2026-11-17 · @FasterMadman

The shell is a sidebar, one view outlet and a permanent dock, driven by a hash router; legacy tabs stay in the page untouched and are shown as one more view until each is replaced. This is the plan for the Sonnet build of #140. Nothing is built yet, and it assumes #138 and #139 have landed.

## Shell layout

Three regions never unmount: the sidebar, the dock (job panel, player queue, player bar) and the legacy host. Only the view outlet changes when the route changes. That is what keeps playback and a running job's panel alive across navigation.

<!-- embedded figure omitted: shell layout · sidebar, view outlet, legacy host, dock -->

The view outlet and the legacy host share the same space. Exactly one of them is visible at a time.

The legacy host is the existing `.content` element with its four `tab-panel`s, left exactly where stage A put it. Legacy code binds to element ids once at load (`setupDrop('import-section', ...)`, the scroll listener on `.content`), so those elements are never moved, cloned or re-created. A legacy route hides `#view` and shows the host; a module route does the opposite.

`ui/index.html` changes in four places only: the `.tabs` bar is removed, `<nav id="sidebar">` and `<main><div id="view"></div>...</main>` wrap the existing `.content`, the dock elements are wrapped in `<div id="dock">` without changing their ids, and one `<script type="module" src="ui/core/boot.js">` is added after the legacy scripts. The header's `openPrefs()` button moves to the foot of the sidebar.

## Files

Twelve core modules, two views, three stylesheets and one test folder. Every module uses relative imports with the `.js` extension, has no side effects at import time, and touches `document` only inside functions, so Node can import it for unit tests.

| File | Exports | Job |
|---|---|---|
| `ui/core/boot.js` | none (entry point) | Registers shell actions and keys, starts the router, restores the last route |
| `ui/core/router.js` | `start(routes, outlet)`, `navigate(path, {replace})`, `current()`, `match(routes, hash)` | Hash parsing, view lifecycle, keep-alive, error card |
| `ui/core/routes.js` | `routes` | The route table. The only file a new view has to be added to |
| `ui/core/api.js` | `get`, `post`, `getBinary`, `ApiError`, `scopeOf` | fetch with one error shape and an `AbortSignal` on every call |
| `ui/core/bus.js` | `on(type, fn, {signal})`, `emit(type, detail)` | App-wide events over one `EventTarget` |
| `ui/core/dom.js` | `html`, `render(el, tpl)`, `$`, `$$` | Tagged template that escapes every interpolation |
| `ui/core/actions.js` | `register(root, handlers, {signal})` | `data-action` delegation, scoped to a root element |
| `ui/core/keys.js` | `scope(bindings, {signal, exclusive})` | Keyboard shortcuts as a stack of scopes |
| `ui/core/jobs.js` | `start(url, body)`, `current()`, events on the bus | Facade over the legacy job panel (see Legacy coexistence) |
| `ui/core/player.js` | `play(tracks, index, {at})`, `stop()`, `state()` | Facade over the legacy player |
| `ui/core/desktop.js` | `isDesktop()`, `pickPath(kind)`, `notify(title, body)`, `receiveDropPaths(paths)` | pywebview bridge and drop pairing |
| `ui/core/legacy-bridge.js` | `legacy` (object of wrappers), `install()` | The only file that reads or writes legacy globals |
| `ui/views/legacy.js` | `mount`, `unmount` | Shows one legacy tab panel in the legacy host |
| `ui/views/not-found.js` | `mount` | Unknown route, with a link home |
| `ui/css/tokens.css` |  | Palette, Jost/DM Mono, spacing scale as custom properties |
| `ui/css/shell.css` |  | Grid for sidebar, main and dock |
| `ui/tests/*.test.js` |  | `node --test` unit tests, run by `test_ui_core.py` |

Later views add `ui/views/<place>.js` and `ui/css/views/<place>.css`. View CSS is linked from `index.html` and every class in it starts with the place name (`.add-`, `.library-`), because there is no build step to scope it.

## Router and routes

Routes are hash paths, so the server needs no new route, back and forward work in the pywebview window, and the last route is restored from `localStorage` on launch. The hash holds where you are and what you are looking at (place, album id, filter). It never holds transient UI state.

Each route is a pattern plus a loader. The loader is a dynamic `import()`, so a view's code is fetched the first time it is opened. That keeps launch fast once Atlas exists.

| Route | View when #140 lands | Replaced by |
|---|---|---|
| `#/add` | `legacy.js` with tab `inbox` | #142 |
| `#/library`, `#/library?q=<query>` | `legacy.js` with tab `library` | #144 |
| `#/library/album/<id>` | not registered yet | #144 |
| `#/collection/<id>` | not registered yet | #146 |
| `#/advanced/export` | `legacy.js` with tab `export` | phase 2 |
| `#/advanced/recover` | `legacy.js` with tab `recover` | History |
| anything else | `not-found.js` |  |

When #142 replaces `#/add`, the legacy Inbox tab moves to `#/advanced/import` in the same PR, so no legacy feature becomes unreachable.

First launch opens `#/add`. An empty hash opens the last route, or `#/add` if there is none.

**Navigation rules the router enforces**

- Every navigation gets a sequence number. If a second navigation starts while the first view is still loading, the first is aborted and never touches the outlet.
- Same view, new params (album 12 to album 13, a new `q`) calls the view's `update(ctx)` if it exports one. Otherwise it unmounts and mounts again.
- If `mount` throws or rejects, the outlet shows an error card with the message and a Reload button. The sidebar and dock keep working.
- The sidebar link whose `href` is a prefix of the current route gets `aria-current="page"`. Sidebar entries are plain `<a href="#/...">`, so no click handler is needed.

## View contract

A view is a module with one required export. Cleanup is automatic: everything a view starts is tied to `ctx.signal`, which the router aborts on the way out.

```
// ui/views/<place>.js
export const keepAlive = false;          // optional

export async function mount(el, ctx) {}  // required: build into el
export function update(ctx) {}           // optional: same view, new params
export function unmount() {}             // optional: only for what a signal cannot clean up
export function pause() {}               // optional, keepAlive views only
export function resume(ctx) {}           // optional, keepAlive views only
```

`ctx` is `{ params, query, signal, navigate }`. `params` come from the route pattern, `query` from the part after `?`.

Rules for view code:

- Pass `ctx.signal` to every `api` call, every `bus.on`, every `actions.register`, every `keys.scope` and every `addEventListener`. A view that does this needs no `unmount`.
- Build markup with `html` and `render`. No `innerHTML` with string concatenation, no inline `onclick`.
- Import from `ui/core/` only. A view never imports another view and never reads a legacy global.
- Keep state in module-level variables, reset at the top of `mount`.

**Keep-alive.** A view that exports `keepAlive = true` is hidden instead of unmounted. Its element stays in `#view` with `hidden` set, `pause()` is called, and coming back calls `resume(ctx)`. This is for views that are expensive to rebuild or must keep scroll position: Library grid, Atlas. The router keeps at most three and unmounts the least recently used.

## State and events

There is no store and no framework. State lives in one of four places, and each has one owner.

| State | Lives in | Example |
|---|---|---|
| Where you are | The hash | `#/library?q=genre:techno` |
| Data | The server. Views fetch on `mount` and on a bus event | Albums, facts, collections |
| App-wide live state | A core module that owns it and announces changes on the bus | Current job, play queue |
| View-local state | Module variables in the view | Selected rows, open panel |

**Bus events** are the only way one part of the page tells another that something changed. The first set:

| Event | Sent by | Detail | Listeners do |
|---|---|---|---|
| `job:changed` | `jobs.js` | `{job, queue}` | Update badges and buttons |
| `job:done` | `jobs.js` | `{kind, ok, result}` | Refetch if the job could have changed what they show |
| `library:changed` | Any code that changed tracks | `{reason}` | Refetch lists |
| `player:changed` | `player.js` | `{track, playing}` | Mark the playing row |
| `route:changed` | `router.js` | `{path, params, query}` | Sidebar highlight, remember last route |

**Rendering.** `html` returns a safe-markup object. Interpolated values are escaped for both text and quoted-attribute positions, nested `html` results and arrays of them are inserted as markup, and there is no export that marks a plain string as safe. `render(el, tpl)` replaces the element's content. A view re-renders the region that changed, not the page. There is no diffing. A list long enough to need it (Library grid) gets a windowing helper in #144, not here.

**Actions.** `actions.register(root, {play(el, event) {...}}, {signal})` attaches one delegated listener per event type on `document`. On a click it finds the closest `[data-action]`, then the nearest registered root above it, and calls that root's handler. Two views can use the same action name without clashing, and legacy inline `onclick` is unaffected.

**Keys.** `keys.scope({...}, {signal, exclusive})` pushes a scope. The newest scope sees a key first. An exclusive scope stops keys from reaching scopes below it. That is what lets Listen through own the arrow keys, Space, Enter and Z without the player or the shell reacting. Shell scope in #140: Cmd+K (placeholder), Cmd+1 to Cmd+3 for the places. The legacy listeners for Cmd+, and Cmd+O stay where they are.

## Legacy coexistence

No line under `ui/legacy/` changes in #140. Legacy scripts load first as classic scripts, then `boot.js` runs (module scripts are deferred), and `legacy-bridge.js` is the only file that knows legacy names.

`legacy-bridge.js` works in both directions:

| Direction | What | How |
|---|---|---|
| Legacy calls into the shell | `switchTab(name)` from `focusFilter()` and from buttons such as "Import" in the scan results | `install()` saves the original as `legacy.showTab`, then sets `window.switchTab = name => navigate(routeForTab(name))` |
| Shell shows a legacy tab | `views/legacy.js` | `legacy.showTab(name)` runs the original `switchTab`, which also calls `loadLibrary()` or `loadTraktor()` as today |
| Shell leaves the legacy host | `views/legacy.js` `unmount` | `legacy.leave()` sets the legacy `activeTab` to an empty string, so the import-decision key handler does not fire behind a module view |
| Modules start a job | `core/jobs.js` | `legacy.startJob(url, body, onDone)` wrapped in a Promise |
| Modules play audio | `core/player.js` | `legacy.playQueue(tracks, index)`, `legacy.stopPlayback()` |
| Modules build a query | `core/api.js` `scopeOf` | `legacy.shellQuote`, `legacy.``filterTerm` |
| Modules pick a path | `core/desktop.js` | Own implementation. `browsePath` stays for legacy fields |

**Jobs and player stay legacy code in #140.** The issue lists `jobs.js` as "SSE attach, the one job panel". I recommend a facade instead of a rewrite here: the panel markup and the `startJob`/`attachJob`/`handleJobEvent`/`pollJobQueue` functions keep running, and `core/jobs.js` gives modules a Promise and bus events on top. #141 changes the import flow anyway and is the right place to port the panel. Rewriting it in the shell PR would put a behaviour change where the goal is "every legacy feature still reachable".

**One implementation of query quoting.** `shellQuote` and `filterTerm` stay in `ui/legacy/filter.js`, where `test_filter.py` checks them against real bash and real beets. Modules reach them through the bridge. When #144 retires the legacy Library, the functions move into a core module and the test is repointed in the same PR. There are never two copies.

**Retiring legacy** is then mechanical: each wrapper in `legacy` is a named dependency. When `grep "legacy\." ui/core ui/views` is empty, the bridge and `ui/legacy/` can be deleted.

## Native drop with mounted views

The decision the issue asks for: bind once, to the shell, and let the page decide which zone was hit. `mount()` never talks to the bridge about drops.

Today `app.py` binds a pywebview drop handler to three fixed element ids at page load. A view mounted later gets none. The page itself already knows which zone a drop landed on, because the DOM `drop` event fires on it. What the page lacks is the full path, which only the native side has. So the two halves are paired:

1. `app.py` `register_drop_targets()` binds one handler on `#app`. On a drop it calls `window.__desktopDrop(paths)` with every full path, not only the first.
2. A view marks a zone with `data-drop-zone`. `desktop.js` has one delegated `drop` listener. It notes the zone that was hit.
3. `desktop.js` pairs the two halves, whichever arrives first, within 500 ms, and dispatches `CustomEvent('paths-dropped', {detail: {paths}})` on the zone.
4. In a browser there is no native half. After the 500 ms the zone gets the event with file names only, and the view shows the existing "copy the full path from Finder" hint.

Legacy zones keep working without edits: `legacy-bridge.js` holds the table that is in `app.py` today (`import-section` to `import-path`, `scan-section` to `scan-path`, `find-drop2` to `conv-src`) and answers a paired drop on one of those ids by calling the legacy `desktopDropPath(fieldId, path)`.

Changes outside `ui/`: `DROP_TARGETS` in `app.py` becomes the single shell binding, `test_desktop.py` asserts that one binding, and the desktop block in `test_smoke.py` calls `__desktopDrop` after a synthetic `drop` instead of calling `desktopDropPath` directly.

This needs one check on the Mac before the slice is built: that a pywebview handler bound on a parent element receives drops on its children with `pywebviewFullPath` filled in. If it does not, the fallback is to bind on `document.body` or to re-register from `desktop.js` through a bridge call when a zone appears.

## How Atlas and Listen through fit

Neither is built in #140, but the contract above is shaped so that neither needs a change to the shell later.

**Atlas (canvas, 10,000+ points)**

- A `keepAlive` view. `pause()` stops its animation frame loop, `resume()` restarts it, and the WebGL context and loaded points survive a trip to another place.
- Loaded lazily through its route, so its code costs nothing until opened.
- Drawing is hand-written WebGL2 point sprites in `ui/lib/atlas-gl.js`: one buffer, one draw call. At this size no rendering library is needed, which keeps the no-npm rule. Hover picking is a grid lookup in plain JS.
- Point data arrives through `api.getBinary` as a typed array, not JSON.
- Layout transitions (Sound, Tempo by energy, Camelot, Compass) run in a module Web Worker so the page stays responsive.
- A lasso selection becomes a temporary `sg_set` on the server and is handed to the Library or the action panel as an ordinary scope. This is the link to the data-model design.

**Listen through (card flow, gestures, keyboard)**

- A full view at `#/listen/<set id>`, not a second outlet or an overlay. It remembers the route it was opened from and returns there. Because the Library is `keepAlive`, coming back is instant and keeps scroll position.
- It pushes an exclusive key scope for the arrow keys, Space, Tab, Enter and Z.
- Swipes use pointer events on the card. Twenty cards are plain DOM.
- Audio goes through `core/player.js` with a start offset (`play(tracks, index, {at})`), so there is still one audio element and the dock shows what is playing.
- Each answer is one `api.post`, and Z posts an undo for the last change set.

**If a library is ever needed**, it is vendored as a single ES module file under `ui/vendor/` with its licence and version in `ui/vendor/README.md`. Nothing is loaded from a CDN, because the app must work offline.

## Build slices

Four PRs, each green on its own, in this order. The first changes nothing a user can see, so the visible change in the second is small enough to review.

| PR | Contains | Tests | Goal metric from #140 it closes |
|---|---|---|---|
| 1. Core primitives | `router.js` (`match` and lifecycle), `bus.js`, `dom.js`, `actions.js`, `keys.js`, `api.js`. `Cache-Control: no-cache` on `/ui/` in `server.py`. `ui/tests/`, `test_ui_core.py`, CI step | Node: route matching with params and query, stale navigation aborted, `html` escaping in text and attributes, action lookup by nearest root, exclusive key scope, `ApiError` for `{ok:false}`, HTTP errors and 409 busy | New code has zero inline `onclick` |
| 2. Shell | `tokens.css`, `shell.css`, `index.html` wrap, sidebar, `routes.js`, `boot.js`, `legacy-bridge.js` (`install`, `showTab`, `leave`), `views/legacy.js`, `views/not-found.js` | `test_smoke.py`: `tab_btn(name)` becomes a sidebar link lookup, plus back/forward, reload restores the route, unknown hash shows not-found, "Import" in scan results still lands on the import form | No beets verb in sidebar or shell. Every legacy feature reachable |
| 3. Dock | `core/jobs.js`, `core/player.js`, player bar always shown with an empty state, dock CSS | `test_smoke.py`: start playback, navigate twice, audio element still playing. Start a job, navigate, panel still shows it. Node: facades against a fake `legacy` | Switching place keeps playback and the job panel alive |
| 4. Drop | `core/desktop.js`, drop pairing, legacy zone table in the bridge, `app.py`, `test_desktop.py` | Node: pairing in both orders and the timeout. `test_smoke.py` desktop block. Manual check on the Mac | A Finder drop reaches the mounted view with its full path |

Sidebar content in PR 2, top to bottom: "Ask your library" button with the Cmd+K hint (opens a "coming soon" note), **Add music**, **Library**, a **Collections** heading with a disabled "New collection" row, an **Advanced** group with Export and Recover, and Preferences at the foot. Labels are sentence case, per #117.

The token file lifts the current values out of `ui/css/app.css` and that file then reads them through `var()`. No colour, font or spacing value changes in #140. The spacing scale is 4, 8, 12, 16, 24, 32, 48.

## Rules and traps for the builder

- **Stale modules.** Without a build step there are no hashed file names, and WKWebView keeps cached scripts across app launches. `/ui/` must answer with `Cache-Control: no-cache` so every load revalidates. On localhost that costs nothing. Skipping it means an updated app can run old views against a new server.
- **Legacy `let` globals are not on `window`.** `activeTab`, `libFilter`, `queue` and the other top-level `let`/`const` in classic scripts are reachable from a module by bare name, but `window.activeTab` is `undefined`. Functions declared with `function` are on `window`. Only `legacy-bridge.js` may use either form.
- **Do not move legacy DOM.** Wrapping `.content` in `<main>` is fine. Re-parenting a `tab-panel`, or rendering legacy markup from a template, breaks listeners that were bound by id at load.
- **`test_smoke.py` locators such as `#tab-library` must keep working unchanged.** If one needs editing for any reason other than navigation, something moved that should not have.
- **Node-importable core.** No `document` or `window` at module top level in `ui/core/`. `boot.js` is the exception and is not unit tested.
- **Host and Origin guards** apply to `/ui/` like every other route. Do not add a static folder that bypasses `before_request`.
- **A new `test_*.py` needs its CI step** in `.github/workflows/test.yml`, or it does not gate merges (CLAUDE.md).
- **Endpoint strings.** In new code, write endpoint paths as full literals in `api` calls, never built by concatenation, so a grep for a route finds its callers. The legacy trap in CLAUDE.md is not to be repeated.

## Decisions (all three approved 2026-11-17)

**1. Jobs and player as facades in #140, ported later.** This narrows one task in the issue. I recommend it, for the reason given under Legacy coexistence. The alternative is to port the job panel now, which roughly doubles the size of the shell work and puts import behaviour at risk in the same PR.

**2. Minimum macOS.** The plan uses module workers, WebGL2 and `AbortSignal` on listeners. All are in Safari 15 and later, and pywebview uses the system WebKit. I recommend macOS 13 Ventura as the floor, stated in the README and checked at launch. Say so if you need to support something older.

**3. First launch opens Add music.** An empty library is the common first state, and the first-run guide (#148) starts there. The alternative is Library.

With a yes to these, the #140 issue body gets a link to this plan and the task list is updated to match: facades for jobs and player, the drop mechanism, `keys.js`, `bus.js`, the no-cache header and `test_ui_core.py`.
