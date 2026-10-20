"use client";

/**
 * #1412 W2: the document says when React has hydrated the page.
 *
 * The release walk (`e2e/navigation.mjs`, `gotoStable`) reads, leaves and
 * clicks pages. Before this marker, it could do so before React had hydrated
 * the page, and a hydration error of one page could reach the verdict of the
 * next route. The only marker was on the sign-out button, which a page can lack.
 *
 * The root layout renders this component, so every page has it. It renders no
 * element. An effect runs only on the client, after React has committed the
 * hydrated tree, so `html[data-hydrated="true"]` appears only after hydration.
 * The server HTML never carries the attribute. `tests/hydration-marker.test.ts`
 * checks both halves.
 */

import { useEffect } from "react";

export function HydrationMarker() {
  useEffect(() => {
    document.documentElement.setAttribute("data-hydrated", "true");
  }, []);
  return null;
}
