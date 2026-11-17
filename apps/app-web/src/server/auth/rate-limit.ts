export interface RateLimiter {
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

export const loginRateLimiter = createFixedWindowRateLimiter(10, 5 * 60_000);
