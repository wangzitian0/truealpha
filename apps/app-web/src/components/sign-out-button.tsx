"use client";

import { useEffect, useState } from "react";
import { api } from "@/client/api";

export function SignOutButton() {
  const [submitting, setSubmitting] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setHydrated(true);
  }, []);

  async function signOut() {
    setSubmitting(true);
    try {
      await api.logout();
    } catch {
      // Redirect even when session already cleared
    } finally {
      window.location.assign("/login");
    }
  }

  return (
    <button
      type="button"
      onClick={signOut}
      disabled={submitting}
      data-sign-out="true"
      data-hydrated={hydrated ? "true" : undefined}
      className="rounded-lg border border-border px-3 py-1.5 text-xs text-gray-400 hover:border-accent hover:text-white disabled:opacity-50"
    >
      {submitting ? "Signing out…" : "Sign out"}
    </button>
  );
}
