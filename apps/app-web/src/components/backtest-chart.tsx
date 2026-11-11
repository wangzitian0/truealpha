"use client";

import { useState } from "react";
import type { BacktestValuationRecord } from "@/server/mart/backtest-repository";

interface Props {
  valuationsMonthly: BacktestValuationRecord[];
  valuationsDaily: BacktestValuationRecord[];
}

export function BacktestChart({ valuationsMonthly, valuationsDaily }: Props) {
  const [resolution, setResolution] = useState<"both" | "1M" | "1D">("both");
  const [activeTab, setActiveTab] = useState<"nav" | "drawdown">("nav");

  const allVals = [
    ...(resolution !== "1D" ? valuationsMonthly : []),
    ...(resolution !== "1M" ? valuationsDaily : []),
  ];
  if (allVals.length === 0) {
    return <div className="p-8 text-center text-gray-400">No valuation data available</div>;
  }

  const benchmark = activeTab === "nav" ? valuationsDaily.filter((v) => v.benchmark_cum_nav !== null) : [];
  const plotted = [
    ...allVals.map((v) => (activeTab === "nav" ? v.cum_nav : v.drawdown)),
    ...benchmark.map((v) => v.benchmark_cum_nav as number),
  ];
  const minNav = Math.min(...plotted);
  const maxNav = Math.max(...plotted);
  const rangeNav = Math.max(maxNav - minNav, 0.01);

  const width = 800;
  const height = 320;
  const pad = 40;
  const toX = (idx: number, total: number) => pad + (idx / Math.max(total - 1, 1)) * (width - 2 * pad);
  const toY = (val: number) => height - pad - ((val - minNav) / rangeNav) * (height - 2 * pad);

  const renderPath = (
    data: BacktestValuationRecord[],
    color: string,
    strokeWidth = 2,
    value: (d: BacktestValuationRecord) => number = (d) => (activeTab === "nav" ? d.cum_nav : d.drawdown),
  ) => {
    if (data.length === 0) return null;
    const points = data.map((d, i) => `${toX(i, data.length)},${toY(value(d))}`);
    return <path d={`M ${points.join(" L ")}`} fill="none" stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />;
  };

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex gap-2">
          {([
            ["nav", "Cumulative NAV"],
            ["drawdown", "Underwater Drawdown"],
          ] as const).map(([tab, label]) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`rounded-lg px-3 py-1 text-xs font-semibold ${
                activeTab === tab ? "bg-accent text-white" : "bg-muted text-gray-400 hover:text-white"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2 text-xs">
          <span className="text-gray-400">Resolution:</span>
          {([
            ["both", "Both (Overlay)"],
            ["1M", "Month ends"],
            ["1D", "Daily"],
          ] as const).map(([res, label]) => (
            <button
              key={res}
              type="button"
              onClick={() => setResolution(res)}
              className={`rounded px-2 py-0.5 ${resolution === res ? "bg-primary text-white" : "text-gray-400"}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="relative overflow-x-auto">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto">
          {[minNav, minNav + rangeNav * 0.5, maxNav].map((yVal, idx) => (
            <line key={idx} x1={pad} y1={toY(yVal)} x2={width - pad} y2={toY(yVal)} stroke="#333" strokeDasharray="4 4" />
          ))}
          <text x={pad} y={toY(maxNav) - 8} fill="#888" fontSize="10">{maxNav.toFixed(2)}</text>
          <text x={pad} y={toY(minNav) + 16} fill="#888" fontSize="10">{minNav.toFixed(2)}</text>
          {resolution !== "1D" && renderPath(valuationsMonthly, "#3b82f6", 2.5)}
          {resolution !== "1M" && renderPath(valuationsDaily, "#10b981", 1.5)}
          {renderPath(benchmark, "#a3a3a3", 1.5, (d) => d.benchmark_cum_nav as number)}
        </svg>
      </div>

      <div className="mt-3 flex items-center justify-center gap-6 text-xs text-gray-400">
        {resolution !== "1D" && (
          <div className="flex items-center gap-2"><span className="h-2 w-4 rounded bg-blue-500" /><span>Strategy, month ends</span></div>
        )}
        {resolution !== "1M" && (
          <div className="flex items-center gap-2"><span className="h-2 w-4 rounded bg-emerald-500" /><span>Strategy, daily</span></div>
        )}
        {benchmark.length > 0 && (
          <div className="flex items-center gap-2"><span className="h-2 w-4 rounded bg-neutral-400" /><span>Benchmark, daily</span></div>
        )}
      </div>
    </div>
  );
}
