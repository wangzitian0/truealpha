/**
 * #540: the session's other end, walked. The logout endpoint has worked since
 * #368 and nothing called it, because #368's acceptance covered only the login
 * half of the lifecycle — a criterion that never named the state transition it
 * was missing.
 *
 * #811: the journey used to be one pass (click the control, then judge the
 * cookie), so a click that did nothing produced a SECURITY claim — "the session
 * cookie survived sign-out" — on staging deploys where nothing was wrong
 * (v0.0.53 run 34482191082, v0.0.60 run 34951072929). `SignOutButton` is a
 * client component; its `onClick` exists only after hydration, and Playwright's
 * `click()` waits for the element to be actionable, not for React to have
 * attached the handler. #853 added a second click after 8 s. That handles a
 * click that lands a little early. It does nothing when hydration takes
 * longer than both windows, and v0.0.73 (run 35082121025) failed with the same
 * message anyway. That failure printed 8 s after the previous line, which is
 * too fast for two missed clicks (8 s + 15 s). So the page did reach /login
 * while the cookie survived. Two things produce that: the logout POST failed
 * and the button navigated anyway (it navigates in a `finally`), or the page
 * was already on /login before anything was clicked. The one-pass journey
 * could not tell these apart. The halves below report each one separately.
 *
 * Now the journey is two halves. Each runs in its own browser context, so
 * neither half's result depends on the other's:
 *
 *   1. `checkControlSignsOut` — the control is present and wired. Wait for
 *      `SignOutButton` to mark itself hydrated (bounded), then click ONCE and
 *      follow what the click did: the POST to /api/auth/logout, its answer, the
 *      cookie, the landing on /login. After hydration a click is
 *      deterministic, so the retry is gone. A control that never hydrates is
 *      reported as that, not as a cookie claim.
 *   2. `checkEndpointEndsSession` — the security claim, judged through the
 *      endpoint and not through a click. POST /api/auth/logout through the
 *      context's request API, which shares the browser context's cookie jar.
 *      Then the cookie must be cleared and a protected route must bounce to
 *      /login.
 *
 * Every problem string names its own cause. `tests/sign-out-journey.test.ts`
 * drives both halves against a fake browser, one broken property at a time.
 */

export const SESSION_COOKIE = "truealpha_session";
export const CONTROL = "[data-sign-out]";
// Set by `SignOutButton` in an effect, so it exists only after the component
// has hydrated with its click handler. The server render never carries it.
export const HYDRATED_CONTROL = '[data-sign-out][data-hydrated="true"]';
const PROTECTED_PATH = "/research";
const LOGOUT_PATH = "/api/auth/logout";

/** @typedef {{ hydrationMs: number, signOutMs: number, clickMs: number }} Timeouts */

/** @type {Readonly<Timeouts>} */
export const DEFAULT_TIMEOUTS = Object.freeze({
  // Bounds the wait for the client bundle to download, parse and hydrate the
  // chrome. It costs time only when hydration is slow or broken. No walk log
  // measures hydration directly. What the logs do show: a passing journey takes
  // 3-8 s end to end (v0.0.70-v0.0.73), and both pre-#853 misses spent the whole
  // 15 s window without reaching /login. The walk opens /research about a
  // minute after a container swap on a shared host, so the bound is three of
  // those windows.
  hydrationMs: 45_000,
  // From the click to the logout request being sent and answered, and from
  // there to the /login landing. Both are same-origin round trips.
  signOutMs: 15_000,
  // The control has already been found hydrated. This bounds only
  // Playwright's own actionability checks.
  clickMs: 5_000,
});

const seconds = (ms) => `${ms / 1000} s`;
const firstLine = (error) => String(error?.message ?? error).split("\n")[0];

/** The session cookie if the jar still holds a usable one. Cleared means
 * absent, empty, or already expired.
 *
 * Reads the whole jar, not `cookies(base)`. Playwright's URL filter drops a
 * `Secure` cookie for any http URL except `localhost`. CI serves the build at
 * http://127.0.0.1 with NODE_ENV=production, so the session cookie is
 * `Secure` there, and a filtered read would find nothing and pass without
 * checking anything. The first local run of this journey did exactly that.
 * Each context is fresh and only talks to one base, so the whole jar is the
 * right scope. */
async function liveSessionCookie(context) {
  const now = Date.now() / 1000;
  return (await context.cookies()).find(
    (cookie) =>
      cookie.name === SESSION_COOKIE &&
      cookie.value !== "" &&
      (cookie.expires === -1 || cookie.expires > now),
  );
}

/** Resolves to the logout request's outcome, or `pending` after `timeoutMs`.
 * `request.response()` alone has no timeout and would hang on a request that
 * the proxy never answers. */
async function settle(request, timeoutMs) {
  let timer;
  const pending = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ kind: "pending" }), timeoutMs);
  });
  const outcome = request.response().then(
    (response) =>
      response
        ? { kind: "answered", response }
        : { kind: "failed", reason: request.failure()?.errorText ?? "no response" },
    (error) => ({ kind: "failed", reason: firstLine(error) }),
  );
  try {
    return await Promise.race([outcome, pending]);
  } finally {
    clearTimeout(timer);
  }
}

/** Half 1: the control is present, hydrates, and one click signs out. */
export async function checkControlSignsOut(context, { base, gotoStable, timeouts }) {
  const page = await context.newPage();
  await gotoStable(page, `${base}${PROTECTED_PATH}`);
  const landed = new URL(page.url()).pathname;
  if (landed !== PROTECTED_PATH) {
    // A fresh session bounced before anything was clicked. That is an
    // authentication finding, and no claim about sign-out can follow from it.
    return [`a fresh session was sent from ${PROTECTED_PATH} to ${landed} before sign-out was tried`];
  }
  if ((await page.locator(CONTROL).count()) === 0) {
    return ["no sign-out control on an authenticated page"];
  }

  try {
    await page.waitForSelector(HYDRATED_CONTROL, { state: "attached", timeout: timeouts.hydrationMs });
  } catch {
    return [`the sign-out control never hydrated within ${seconds(timeouts.hydrationMs)} (client bundle did not run)`];
  }

  const didNot = (why) => [`control present but did not sign out (${why})`];
  // Armed before the click, so a fast request cannot fire before anyone is listening.
  const sent = page
    .waitForRequest(
      (request) => request.method() === "POST" && new URL(request.url()).pathname === LOGOUT_PATH,
      { timeout: timeouts.signOutMs },
    )
    .catch(() => null);
  try {
    await page.locator(HYDRATED_CONTROL).first().click({ timeout: timeouts.clickMs });
  } catch (error) {
    return didNot(`the click failed: ${firstLine(error)}`);
  }

  const request = await sent;
  if (!request) return didNot(`no POST ${LOGOUT_PATH} within ${seconds(timeouts.signOutMs)} of the click`);
  const outcome = await settle(request, timeouts.signOutMs);
  if (outcome.kind === "pending") return didNot(`POST ${LOGOUT_PATH} unanswered after ${seconds(timeouts.signOutMs)}`);
  if (outcome.kind === "failed") return didNot(`POST ${LOGOUT_PATH} failed: ${outcome.reason}`);
  if (!outcome.response.ok()) return didNot(`POST ${LOGOUT_PATH} answered ${outcome.response.status()}`);

  const problems = [];
  // `commit`, not the default `load`: the question is where the click sent the
  // page, not whether /login finished loading its scripts. On a cold deploy the
  // load event can arrive long after the navigation. A real-browser run with
  // delayed client chunks reported "still on /login" while waiting for `load`.
  const reached = await page
    .waitForURL((url) => url.pathname === "/login", { timeout: timeouts.signOutMs, waitUntil: "commit" })
    .then(
      () => true,
      () => false,
    );
  if (!reached) {
    problems.push(`still on ${new URL(page.url()).pathname} ${seconds(timeouts.signOutMs)} after the logout answered`);
  }
  if (await liveSessionCookie(context)) {
    problems.push(`${SESSION_COOKIE} is still set after the click`);
  }
  return problems.length > 0 ? didNot(problems.join("; ")) : [];
}

/** Half 2: the endpoint ends the session. No click involved. */
export async function checkEndpointEndsSession(context, { base, gotoStable }) {
  if (!(await liveSessionCookie(context))) {
    return [`login set no ${SESSION_COOKIE} cookie, so there was no session for the logout endpoint to end`];
  }
  const problems = [];
  let response;
  try {
    // context.request shares the context's cookie jar. It sends the session
    // cookie wherever the jar would, and it applies the response's Set-Cookie
    // to the same jar, so the page opened below sees the result.
    response = await context.request.post(`${base}${LOGOUT_PATH}`);
  } catch (error) {
    problems.push(`logout endpoint could not be reached (${firstLine(error)})`);
  }
  if (response && !response.ok()) {
    problems.push(`logout endpoint answered ${response.status()}`);
  }
  if (await liveSessionCookie(context)) {
    problems.push("logout endpoint did not clear the cookie");
  }

  const page = await context.newPage();
  await gotoStable(page, `${base}${PROTECTED_PATH}`);
  const landed = new URL(page.url()).pathname;
  if (landed !== "/login") {
    problems.push(`protected route still rendered after logout (${landed})`);
  }
  return problems;
}

const HALVES = [
  {
    label: "sign-out control",
    check: checkControlSignsOut,
    passed: "control hydrated; one click POSTed the logout and landed on /login with the cookie cleared",
  },
  {
    label: "sign-out endpoint",
    check: checkEndpointEndsSession,
    passed: "logout endpoint cleared the cookie; the protected route bounces to /login",
  },
];

const VIEWPORT = { width: 1440, height: 900 };

/** Logs in once and returns the signed-in storage state (the session cookie).
 *
 * One login for both halves, not one each. The login route allows 10 attempts
 * per IP per 5 minutes, and a walk already spends up to two on its route
 * passes. A login per half would make it four, so three walks re-dispatched
 * inside five minutes from one runner IP would hit 429. Sharing the cookie is
 * safe because the logout route clears only the caller's cookie. There is no
 * server-side revocation (see its header). So one context's sign-out leaves
 * the other context's copy valid. If revocation is added, give each half its
 * own login. */
async function signIn(browser, { base, email, password }) {
  const context = await browser.newContext({ viewport: VIEWPORT });
  try {
    const login = await context.request.post(`${base}/api/auth/login`, { data: { email, password } });
    if (login.status() !== 200) {
      console.error(`login failed for the sign-out journey: ${login.status()}`);
      await context.close();
      process.exit(2);
    }
    return await context.storageState();
  } finally {
    await context.close();
  }
}

/** Runs both halves, each in its own context holding the same fresh session,
 * and returns how many failed. */
export async function walkSignOutJourney(
  browser,
  { base, email, password, gotoStable, timeouts = DEFAULT_TIMEOUTS, log = console.log },
) {
  const session = await signIn(browser, { base, email, password });
  let failures = 0;
  for (const { label, check, passed } of HALVES) {
    const context = await browser.newContext({ viewport: VIEWPORT, storageState: session });
    try {
      const problems = await check(context, { base, gotoStable, timeouts });
      if (problems.length > 0) {
        failures += 1;
        log(`FAIL [${label}] — ${problems.join("; ")}`);
      } else {
        log(`ok   [${label}] ${passed}`);
      }
    } finally {
      await context.close();
    }
  }
  return failures;
}
