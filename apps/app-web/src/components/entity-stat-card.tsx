import type { ReactNode } from "react";
import { formatCell, formatRatio } from "@/client/format";

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

export function ModuleMetaFooter({
  extractor,
  extractorTestId,
  confidence,
  cutoff,
  secondLabel,
  secondValue,
  reasonCodes,
  reasonLabel,
  reasonTestId,
}: {
  extractor: string;
  extractorTestId: string;
  confidence: string | null;
  cutoff: string;
  secondLabel: string;
  secondValue: string;
  reasonCodes: readonly string[];
  reasonLabel: string;
  reasonTestId: string;
}) {
  return (
    <div className="border-t border-border/60 pt-3 text-xs text-gray-400 space-y-1">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>
          Extractor: <span className="font-mono text-gray-300" data-testid={extractorTestId}>{extractor}</span>
        </span>
        <span>
          Confidence:{" "}
          <span className="font-mono text-gray-300" title={confidence ?? undefined}>
            {formatCell(formatRatio(confidence))}
          </span>
        </span>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>
          Cutoff: <span className="font-mono text-gray-300">{cutoff}</span>
        </span>
        <span>
          {secondLabel}: <span className="font-mono text-gray-300">{secondValue}</span>
        </span>
      </div>
      {reasonCodes.length > 0 && (
        <div className="font-mono text-[11px] text-gray-500" data-testid={reasonTestId}>
          {reasonLabel}: {reasonCodes.join(", ")}
        </div>
      )}
    </div>
  );
}
