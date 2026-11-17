export const SESSION_COOKIE_NAME = "truealpha_session";
export interface AuthConfig {
  cookieName: string;
}
export function loadAuthConfig(): AuthConfig {
  return { cookieName: SESSION_COOKIE_NAME };
}
