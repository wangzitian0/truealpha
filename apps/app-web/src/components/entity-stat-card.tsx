import type { ReactNode } from "react";

export function StatBox({
  label,
  value,
  title,
  testId,
  valueClass = "text-white",
}: {
  label: string;
  value: ReactNode;
  title?: string;
  testId?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-lg border border-border/60 bg-slate-900/50 p-3">
      <div className="text-xs text-gray-400">{label}</div>
      <div className={`mt-1 font-mono text-xl font-bold ${valueClass}`} data-testid={testId} title={title}>
        {value}
      </div>
    </div>
  );
}
