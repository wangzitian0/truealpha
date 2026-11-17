import { redirect } from "next/navigation";
import { ResearchNav } from "@/components/research-nav";
import { getServerPrincipal } from "@/server/auth/request-context";

export const dynamic = "force-dynamic";

export default async function ResearchLayout({ children }: { children: React.ReactNode }) {
  const principal = await getServerPrincipal();
  if (!principal) {
    redirect("/login?from=%2Fresearch");
  }

  return (
    <div className="space-y-8">
      <ResearchNav />
      {children}
    </div>
  );
}
