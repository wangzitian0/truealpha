"use client";

/**
 * #368: client-side gate that bounces an unauthenticated visitor to /login.
 * This is a UX convenience only — it is NOT the authorization boundary.
 * The real boundary is server-side: every protected route's server loader
 * must independently call getSessionAccessContext / getRequestAccessContext
 * and refuse to render/return data without it (#371 wires this into the
 * /research and /admin route groups). A client-side check alone can always
 * be bypassed by calling the API directly.
 */

import { useEffect, useState } from "react";
import { apiFetch } from "@/client/api-fetch";

type Status = "checking" | "authenticated" | "unauthenticated";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<Status>("checking");

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/auth/me")
      .then((response) => {
        if (cancelled) return;
        setStatus(response.ok ? "authenticated" : "unauthenticated");
      })
      .catch(() => {
        if (!cancelled) setStatus("unauthenticated");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (status === "checking") return null;
  if (status === "unauthenticated") return null; // apiFetch already redirected on 401
  return <>{children}</>;
}
