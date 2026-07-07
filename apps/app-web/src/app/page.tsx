const modules = [
  { n: 1, name: "PEG", note: "switchable growth conventions", phase: "Phase 1" },
  { n: 2, name: "Gross profit / employee", note: "headcount gaps flagged, not dropped", phase: "Phase 2" },
  { n: 3, name: "Supply-chain graph", note: "causal reasoning gated on edge confidence", phase: "Phase 4" },
  { n: 4, name: "Analyst backtesting", note: "blocked until a rating source is confirmed", phase: "Phase 3" },
  { n: 5, name: "ETF virtual company", note: "needs a holdings-weight source", phase: "Phase 1" },
  { n: 6, name: "Pure-blood screening", note: "LLM-assisted segment classification", phase: "Phase 5" },
  { n: 7, name: "Three-tier valuation", note: "composite factor over module 2", phase: "Phase 2.5" },
];

export default function Home() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>
        <p className="mt-2 text-gray-400">
          Reads the <code className="text-accent">mart</code> schema directly. Nothing is materialized yet —
          currently in Phase&nbsp;-1 (data reconnaissance).
        </p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {modules.map((m) => (
          <div key={m.n} className="rounded-xl border border-border bg-card p-5">
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-500">Module {m.n}</span>
              <span className="text-xs rounded-full border border-border px-2 py-0.5 text-gray-400">{m.phase}</span>
            </div>
            <h2 className="mt-2 font-semibold">{m.name}</h2>
            <p className="mt-1 text-sm text-gray-400">{m.note}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
