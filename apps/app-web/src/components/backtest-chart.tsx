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

  const dataM = valuationsMonthly;
  const dataD = valuationsDaily;

  // Determine bounds
  const allVals = [
    ...(resolution !== "1D" ? dataM : []),
    ...(resolution !== "1M" ? dataD : []),
  ];

  if (allVals.length === 0) {
    return <div className="p-8 text-center text-gray-400">No valuation data available</div>;
  }

  const getVal = (v: BacktestValuationRecord): number => {
    const raw = activeTab === "nav" ? v.cum_nav : v.drawdown;
    if (raw === null || raw === undefined) return 0;
    const n = parseFloat(raw);
    return Number.isFinite(n) ? n : 0;
  };

  const minNav = Math.min(...allVals.map(getVal));
  const maxNav = Math.max(...allVals.map(getVal));
  const rangeNav = Math.max(maxNav - minNav, 0.01);

  const width = 800;
  const height = 320;
  const padding = 40;

  const toX = (idx: number, total: number) => padding + (idx / Math.max(total - 1, 1)) * (width - 2 * padding);
  const toY = (val: number) => height - padding - ((val - minNav) / rangeNav) * (height - 2 * padding);

  const renderPath = (data: BacktestValuationRecord[], color: string, strokeWidth = 2) => {
    if (data.length === 0) return null;
    const points = data.map((d, i) => `${toX(i, data.length)},${toY(getVal(d))}`);
    return <path d={`M ${points.join(" L ")}`} fill="none" stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />;
  };

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setActiveTab("nav")}
            className={`rounded-lg px-3 py-1 text-xs font-semibold ${
              activeTab === "nav" ? "bg-accent text-white" : "bg-muted text-gray-400 hover:text-white"
            }`}
          >
            Cumulative NAV
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("drawdown")}
            className={`rounded-lg px-3 py-1 text-xs font-semibold ${
              activeTab === "drawdown" ? "bg-accent text-white" : "bg-muted text-gray-400 hover:text-white"
            }`}
          >
            Underwater Drawdown
          </button>
        </div>

        <div className="flex items-center gap-2 text-xs">
          <span className="text-gray-400">Resolution:</span>
          <button
            type="button"
            onClick={() => setResolution("both")}
            className={`rounded px-2 py-0.5 ${resolution === "both" ? "bg-primary text-white" : "text-gray-400"}`}
          >
            Both (Overlay)
          </button>
          <button
            type="button"
            onClick={() => setResolution("1M")}
            className={`rounded px-2 py-0.5 ${resolution === "1M" ? "bg-primary text-white" : "text-gray-400"}`}
          >
            10Y Monthly
          </button>
          <button
            type="button"
            onClick={() => setResolution("1D")}
            className={`rounded px-2 py-0.5 ${resolution === "1D" ? "bg-primary text-white" : "text-gray-400"}`}
          >
            3Y Daily
          </button>
        </div>
      </div>

      {/* SVG Chart */}
      <div className="relative overflow-x-auto">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto">
          {/* Grid lines */}
          <line x1={padding} y1={toY(minNav)} x2={width - padding} y2={toY(minNav)} stroke="#333" strokeDasharray="4 4" />
          <line x1={padding} y1={toY(minNav + rangeNav * 0.5)} x2={width - padding} y2={toY(minNav + rangeNav * 0.5)} stroke="#333" strokeDasharray="4 4" />
          <line x1={padding} y1={toY(maxNav)} x2={width - padding} y2={toY(maxNav)} stroke="#333" strokeDasharray="4 4" />

          {/* Labels */}
          <text x={padding} y={toY(maxNav) - 8} fill="#888" fontSize="10">
            {maxNav.toFixed(2)}
          </text>
          <text x={padding} y={toY(minNav) + 16} fill="#888" fontSize="10">
            {minNav.toFixed(2)}
          </text>

          {/* Lines */}
          {resolution !== "1D" && renderPath(dataM, "#3b82f6", 2.5)}
          {resolution !== "1M" && renderPath(dataD, "#10b981", 1.5)}
        </svg>
      </div>

      <div className="mt-3 flex items-center justify-center gap-6 text-xs text-gray-400">
        {resolution !== "1D" && (
          <div className="flex items-center gap-2">
            <span className="h-2 w-4 rounded bg-blue-500" />
            <span>10-Year Monthly Master (CAGR Track)</span>
          </div>
        )}
        {resolution !== "1M" && (
          <div className="flex items-center gap-2">
            <span className="h-2 w-4 rounded bg-emerald-500" />
            <span>3-Year Daily Overlay (Sharpe & Drawdown Track)</span>
          </div>
        )}
      </div>
    </div>
  );
}
