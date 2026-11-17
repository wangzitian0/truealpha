import { SESSION_COOKIE_NAME } from "./config";
export { SESSION_COOKIE_NAME };
export function cookieHeaderForToken(token: string | undefined): string | undefined {
  return token ? `${SESSION_COOKIE_NAME}=${token}` : undefined;
}
