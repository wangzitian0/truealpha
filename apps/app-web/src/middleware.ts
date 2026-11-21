import { NextResponse, type NextRequest } from "next/server";
import { resolveBackendOrigin } from "./backend-origin";

export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const target = new URL(`${pathname}${search}`, resolveBackendOrigin());
  return NextResponse.rewrite(target);
}

export const config = {
  matcher: ["/api/v1/:path*"],
};
