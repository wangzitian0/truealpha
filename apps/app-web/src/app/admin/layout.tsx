// Authorization is evaluated per-request (see src/server/auth-context.ts);
// this route group must never be statically prerendered or cached, or a
// build-time environment snapshot would leak into every later request.
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
