/**
 * #368: resolves auth secrets/settings from the environment. The one
 * production-security invariant: `SECRET_KEY` must come from real config
 * (Vault in Staging/Production — see `.env.example`), never the hardcoded
 * dev default, in a production environment.
 */

const DEV_ONLY_DEFAULT_SECRET = "dev-only-insecure-secret-do-not-use-in-production-change-me";
const DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7; // 7 days — a small named group of users, not a public SaaS session policy
export const SESSION_COOKIE_NAME = "truealpha_session";

export interface AuthConfig {
  secret: Uint8Array;
  accessTokenExpireMinutes: number;
  cookieName: string;
}

export function loadAuthConfig(): AuthConfig {
  const secretKey = process.env.SECRET_KEY;
  const isProduction = process.env.NODE_ENV === "production";

  if (!secretKey && isProduction) {
    throw new Error(
      "SECRET_KEY is not set. In production this must come from Vault, never the development default.",
    );
  }
  // .env.example promises production refuses the dev default; an explicitly
  // pasted default must be rejected exactly like an unset key (#447).
  if (secretKey === DEV_ONLY_DEFAULT_SECRET && isProduction) {
    throw new Error(
      "SECRET_KEY is the development default. In production this must come from Vault, never the development default.",
    );
  }

  const parsedExpiry = Number.parseInt(process.env.ACCESS_TOKEN_EXPIRE_MINUTES ?? "", 10);
  const accessTokenExpireMinutes =
    Number.isFinite(parsedExpiry) && parsedExpiry > 0 ? parsedExpiry : DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES;

  return {
    secret: new TextEncoder().encode(secretKey ?? DEV_ONLY_DEFAULT_SECRET),
    accessTokenExpireMinutes,
    cookieName: SESSION_COOKIE_NAME,
  };
}
