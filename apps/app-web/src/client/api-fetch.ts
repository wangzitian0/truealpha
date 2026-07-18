"use client";

/**
 * #368: client-side fetch wrapper — always sends the session cookie and
 * redirects to /login on a 401 rather than letting every caller reimplement
 * that check.
 */

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, { ...init, credentials: "include" });
  if (response.status === 401 && typeof window !== "undefined") {
    const from = encodeURIComponent(window.location.pathname);
    window.location.assign(`/login?from=${from}`);
  }
  return response;
}
