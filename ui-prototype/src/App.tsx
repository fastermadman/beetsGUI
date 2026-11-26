// Placeholder shell. Screens are added in issue #178, one component per screen,
// function components + hooks only (so the app can alias to preact/compat later).
const screens = ["Add music", "Collection", "Health", "History", "Sound map"];

export function App() {
  return (
    <div className="flex h-screen">
      <nav className="w-52 shrink-0 border-r border-line bg-panel p-4">
        <div className="mb-6 text-lg font-medium tracking-wide">Segue</div>
        <ul className="space-y-1">
          {screens.map((s) => (
            <li key={s} className="rounded px-3 py-2 text-muted hover:bg-line hover:text-text">
              {s}
            </li>
          ))}
        </ul>
      </nav>
      <main className="flex flex-1 items-center justify-center">
        <div className="text-center">
          <h1 className="text-2xl font-medium">Segue UI prototype</h1>
          <p className="mt-2 font-mono text-sm text-muted">
            Placeholder. Select any element in Onlook and describe the screen you want.
          </p>
        </div>
      </main>
    </div>
  );
}
