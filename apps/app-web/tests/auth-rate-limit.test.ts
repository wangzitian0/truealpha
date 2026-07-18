/**
 * #368: per-IP login rate limiting — fixed-window counter, deterministic via
 * an injected clock so the test has no real sleeps.
 *
 * Run standalone: `bun run tests/auth-rate-limit.test.ts`.
 */

import { createFixedWindowRateLimiter } from "../src/server/auth/rate-limit";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function run() {
  // --- allows up to the max, then blocks within the same window ---
  {
    let now = 1_000_000;
    const limiter = createFixedWindowRateLimiter(3, 60_000, () => now);
    assert(limiter.attempt("1.2.3.4") === true, "attempt 1 must be allowed");
    assert(limiter.attempt("1.2.3.4") === true, "attempt 2 must be allowed");
    assert(limiter.attempt("1.2.3.4") === true, "attempt 3 must be allowed");
    assert(limiter.attempt("1.2.3.4") === false, "attempt 4 within the window must be blocked");
  }

  // --- a different key has its own independent counter ---
  {
    let now = 1_000_000;
    const limiter = createFixedWindowRateLimiter(1, 60_000, () => now);
    assert(limiter.attempt("1.2.3.4") === true, "first key's first attempt must be allowed");
    assert(limiter.attempt("1.2.3.4") === false, "first key's second attempt must be blocked");
    assert(limiter.attempt("5.6.7.8") === true, "a different key must not be blocked by the first key's usage");
  }

  // --- once the window elapses, the counter resets ---
  {
    let now = 1_000_000;
    const limiter = createFixedWindowRateLimiter(1, 60_000, () => now);
    assert(limiter.attempt("1.2.3.4") === true, "first attempt must be allowed");
    assert(limiter.attempt("1.2.3.4") === false, "second attempt in-window must be blocked");
    now += 60_001;
    assert(limiter.attempt("1.2.3.4") === true, "attempt after the window elapses must be allowed again");
  }

  console.log("auth-rate-limit.test.ts: all assertions passed");
}

run();
