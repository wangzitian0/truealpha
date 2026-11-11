import { formatCell, formatRatio } from "@/client/format";
import type { WidePanelCell } from "@/server/mart/research-read";

/**
 * Standardized wide-panel node cell for rankings and coverage tables (#1679).
 * Renders value and confidence with exact font-mono tabular-nums formatting.
 */
export function PanelNodeTd({
  node,
  formatted,
}: {
  node: WidePanelCell | null | undefined;
  formatted: string | null;
}) {
  return (
    <td className="px-4 py-3 font-mono tabular-nums" title={node?.value ?? undefined}>
      <div>{formatCell(formatted)}</div>
      {node?.confidence && (
        <div className="text-[11px] text-gray-500" title={node.confidence}>
          conf {formatCell(formatRatio(node.confidence))}
        </div>
      )}
    </td>
  );
}
