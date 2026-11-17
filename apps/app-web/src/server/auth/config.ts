const DEV_ONLY_DEFAULT_SECRET = "dev-only-insecure-secret-do-not-use-in-production-change-me";
const DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7;
export const SESSION_COOKIE_NAME = "truealpha_session";

export interface AuthConfig {
  secret: Uint8Array;
  accessTokenExpireMinutes: number;
  cookieName: string;
  secureCookie: boolean;
}

const DEPLOYED_APP_ENVS = new Set(["production", "staging", "preview"]);

export function loadAuthConfig(): AuthConfig {
  const secretKey = process.env.SECRET_KEY;
  const appEnv = (process.env.ENVIRONMENT ?? process.env.APP_ENV ?? "").trim().toLowerCase();
  const isProduction =
    process.env.NODE_ENV === "production" ||
    (process.env.NODE_ENV !== "development" && process.env.NODE_ENV !== "test" && DEPLOYED_APP_ENVS.has(appEnv));

  if (!secretKey && isProduction) {
    throw new Error(
      "SECRET_KEY is not set. In production this must come from Vault, never the development default.",
    );
  }
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
    secureCookie: isProduction,
  };
}
