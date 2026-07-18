/**
 * #368: per-IP login rate limiting — a fixed-window in-memory counter.
 *
 * Known limitation: this is process-local. It resets on restart and does
 * not share state across multiple app-web instances. That is an accepted
 * gap for the current single-instance deployment (docker-compose, one
 * `app-web` container); moving to a shared store (e.g. Redis) is follow-up
 * work if/when app-web is horizontally scaled, not blocking for v1.
 */

export interface RateLimiter {
  /** Records an attempt for `key` and returns whether it is allowed. */
  attempt(key: string): boolean;
}

export function createFixedWindowRateLimiter(
  maxAttempts: number,
  windowMs: number,
  now: () => number = Date.now,
): RateLimiter {
  const windows = new Map<string, { windowStart: number; count: number }>();

  return {
    attempt(key: string): boolean {
      const t = now();
      const entry = windows.get(key);

      if (!entry || t - entry.windowStart >= windowMs) {
        windows.set(key, { windowStart: t, count: 1 });
        return true;
      }
      if (entry.count >= maxAttempts) {
        return false;
      }
      entry.count += 1;
      return true;
    },
  };
}

/** Process-wide singleton for the login route: 10 attempts per IP per 5 minutes. */
export const loginRateLimiter = createFixedWindowRateLimiter(10, 5 * 60_000);
