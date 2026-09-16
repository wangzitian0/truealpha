"use client";

/**
 * #540: the session's other end.
 *
 * `POST /api/auth/logout` has existed and worked since #368 — nothing in the
 * interface ever called it. #368 scoped the logout ROUTE under its remediation
 * and its "frontend glue" listed `/login`, a client guard, `apiFetch` and
 * `/api/auth/me`; no sign-out control, and its acceptance covered only the
 * login half of the lifecycle. So a 7-day cookie could be ended from devtools
 * and nowhere else.
 *
 * A full reload rather than a router push: the session cookie is what every
 * server gate reads, so the client-side cache of a logged-in render must not
 * survive the logout that invalidated it.
 *
 * #811: `data-hydrated` says the click handler is live. The server render and
 * the pre-hydration DOM carry `data-sign-out` with no handler behind it, so a
 * click there does nothing. An effect runs only on the client, after React has
 * committed this component with its `onClick`, so the attribute appearing
 * means a click will POST the logout. `e2e/sign-out-journey.mjs` waits for it
 * before clicking. `tests/sign-out-button.test.ts` checks both halves: the
 * attribute is absent from the server render and present after mount.
 */

import { useEffect, useState } from "react";

export function SignOutButton() {
  const [submitting, setSubmitting] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setHydrated(true);
  }, []);

  async function signOut() {
    setSubmitting(true);
    try {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
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
