// Authorization is evaluated per-request from the verified session (#371,
// see src/server/auth/request-context.ts's getServerPrincipal); this route
// group must never be statically prerendered or cached, or a build-time
// snapshot would leak into every later request. Each page under here still
// denies independently (e.g. strategy-runs/page.tsx) rather than this
// layout redirecting, so a logged-in non-administrator sees an explicit
// "access denied" message instead of a silent bounce.
export const dynamic = "force-dynamic";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-accent">Administrator</span>
        <span className="text-xs text-gray-500">— separately server-gated from research routes</span>
      </div>
      {children}
    </div>
  );
}
