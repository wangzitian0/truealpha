import { notFound, redirect } from "next/navigation";
import { ApiError, type RequestOpts } from "@/client/api";
import { getServerPrincipal } from "@/server/auth/request-context";

export async function loadAuthed<T>(
  fromPath: string,
  loader: (opts: RequestOpts) => Promise<T>,
  opts: { admin?: boolean; notFoundOn404?: boolean } = {},
): Promise<{ data: T | null; error: string | null }> {
  const principal = await getServerPrincipal();
  if (!principal || (opts.admin && principal.principalKind !== "administrator")) {
    redirect(`/login?from=${encodeURIComponent(fromPath)}`);
  }
  try {
    return { data: await loader(principal.requestOpts), error: null };
  } catch (err) {
    if (err instanceof ApiError && opts.notFoundOn404 && err.status === 404) notFound();
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      redirect(`/login?from=${encodeURIComponent(fromPath)}`);
    }
    return { data: null, error: err instanceof Error ? err.message : String(err) };
  }
}
