/**
 * #371: root `/` no longer reads anything or resolves an AccessContext
 * itself. #370 built the dashboard directly on this route via
 * `getLocalAdminAccessContext()`; that content now lives at /research,
 * gated by a real verified session (login page for anonymous visitors, via
 * /research's own layout). This route is just a redirect, so there is no
 * data read here for the local-admin path to ever reach in the first place.
 */

import { redirect } from "next/navigation";

export default function RootPage() {
  redirect("/research");
}
