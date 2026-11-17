import { NextResponse, type NextRequest } from "next/server";

export function resolveBackendOrigin(): string {
  const configured = process.env.LLM_SERVICE_URL?.trim();
  if (configured) return configured.replace(/\/+$/, "");
  const suffix = process.env.ENV_SUFFIX;
  if (suffix !== undefined) return `http://truealpha-llm${suffix}:8000`;
  const env = (process.env.APP_ENV || "").trim().toLowerCase();
  if (env === "staging") return "http://truealpha-llm-staging:8000";
  if (env === "prod" || env === "production") return "http://truealpha-llm:8000";
  return "http://127.0.0.1:8000";
}

export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const target = new URL(`${pathname}${search}`, resolveBackendOrigin());
  return NextResponse.rewrite(target);
}

export const config = {
  matcher: ["/api/v1/:path*"],
};
