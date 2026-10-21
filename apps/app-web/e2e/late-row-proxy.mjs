/**
 * A proxy that delivers a page with some flight rows late (#1468).
 *
 * The HTML of an App Router page ends with `<script>self.__next_f.push([1,"..."])</script>`
 * tags. Their strings, joined in order, are the React Server Components (flight) stream.
 * A row is `<hex id>:<payload>` and ends with a newline. An element is `["$", type, key, props]`.
 * A child `"$L<hex id>"` is a lazy reference: "the element in the row with this id, which may
 * not have arrived yet". The async `AppChrome` row and the table rows that Flight outlines are
 * such references.
 *
 * Next 15.5 bundles a React copy that hydrates wrongly when the browser holds a lazy reference
 * under a host element (`div`, `tbody`, ...) and the row is still missing: React replays the host
 * element and claims a server node twice. It raises error #418 and renders the page again on the
 * client. The deployed server delivers the `AppChrome` row last, so the window was open on about one
 * visit in ten. A loopback server delivers all rows in one burst, so CI never saw it.
 * This proxy opens the window on purpose. It sends the document without the rows that a host
 * element references lazily, waits, and then sends those rows in a script of their own.
 *
 * This module has no side effects at import. `tests/late-row-proxy.test.ts` drives it.
 */

import http from "node:http";

const FLIGHT_PUSH = /<script>self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)<\/script>/g;
const LAZY_REFERENCE = /^\$L([0-9a-f]+)$/;
const ROW_START = /^([0-9a-f]+):/;
const HOST_TAG = /^[a-z][a-z0-9-]*$/;

/**
 * @param {string} html the document of one page
 * @returns {{ start: number, end: number, text: string }[]} the flight scripts in document order;
 *   `text` is the decoded string that the script pushes
 */
export function readFlightScripts(html) {
  return [...html.matchAll(FLIGHT_PUSH)].map((match) => ({
    start: match.index,
    end: match.index + match[0].length,
    text: JSON.parse(match[1]),
  }));
}

/**
 * @param {string} stream the joined flight stream
 * @returns {{ id: string, text: string }[]} the rows; `text` includes the id and the final newline
 */
export function splitRows(stream) {
  const rows = [];
  let start = 0;
  for (let newline = stream.indexOf("\n"); newline !== -1; newline = stream.indexOf("\n", start)) {
    const line = stream.slice(start, newline + 1);
    const id = ROW_START.exec(line)?.[1];
    // A line without an id continues a text row. It belongs to the row before it.
    if (id === undefined && rows.length > 0) rows[rows.length - 1].text += line;
    else if (id !== undefined) rows.push({ id, text: line });
    start = newline + 1;
  }
  return rows;
}

/** @param {unknown} node @param {Set<string>} found */
function collectHostLazyChildren(node, found) {
  if (Array.isArray(node)) {
    const isElement = node[0] === "$" && typeof node[1] === "string" && node.length >= 4;
    if (!isElement) {
      for (const child of node) collectHostLazyChildren(child, found);
      return;
    }
    const props = node[3];
    if (props !== null && typeof props === "object") {
      if (HOST_TAG.test(node[1])) {
        const children = props.children;
        const list = Array.isArray(children) && children[0] !== "$" ? children : [children];
        for (const child of list) {
          const reference = typeof child === "string" ? LAZY_REFERENCE.exec(child) : null;
          if (reference !== null) found.add(reference[1]);
        }
      }
      for (const value of Object.values(props)) collectHostLazyChildren(value, found);
    }
    return;
  }
  if (node !== null && typeof node === "object") {
    for (const value of Object.values(node)) collectHostLazyChildren(value, found);
  }
}

/**
 * @param {{ id: string, text: string }[]} rows
 * @returns {Set<string>} the ids of the rows that a host element holds as a lazy child
 */
export function findHostLazyRows(rows) {
  const found = new Set();
  for (const row of rows) {
    const payload = row.text.slice(row.id.length + 1);
    if (!payload.startsWith("[") && !payload.startsWith("{")) continue;
    try {
      collectHostLazyChildren(JSON.parse(payload), found);
    } catch {
      // A text row can hold a newline in its payload. It is not an element.
    }
  }
  return found;
}

/** Valid inside an inline script: a JSON string with `<` escaped, so that `</script` cannot end the script. */
const scriptFor = (text) => `<script>self.__next_f.push([1,${JSON.stringify(text).replace(/</g, "\\u003c")}])</script>`;

/**
 * Splits a document in two. `early` has every row except the rows that a host element holds as a
 * lazy child. `late` has those rows in a script of their own, then the rest of the document.
 *
 * @param {string} html the document of one page
 * @returns {{ early: string, late: string, rows: string[] } | null}
 *   `null` means that no host element holds a lazy child. A caller that expects a late row must
 *   treat `null` as a failure, because a proxy that holds nothing back tests nothing.
 */
export function splitAtLateRows(html) {
  const scripts = readFlightScripts(html);
  if (scripts.length === 0) return null;
  const rows = splitRows(scripts.map((script) => script.text).join(""));
  const lateIds = findHostLazyRows(rows);
  const lateRows = rows.filter((row) => lateIds.has(row.id));
  if (lateRows.length === 0) return null;

  const earlyText = rows.filter((row) => !lateIds.has(row.id)).map((row) => row.text).join("");
  let early = html.slice(0, scripts[0].start) + scriptFor(earlyText);
  for (let index = 1; index < scripts.length; index += 1) early += html.slice(scripts[index - 1].end, scripts[index].start);
  const lastEnd = scripts[scripts.length - 1].end;
  const late = scriptFor(lateRows.map((row) => row.text).join("")) + html.slice(lastEnd);
  return { early, late, rows: lateRows.map((row) => row.id) };
}

/**
 * Starts a proxy in front of `origin`. For every document that has a late row, it sends `early`,
 * waits `holdMs`, then sends `late`. Other responses pass through.
 *
 * @param {{ origin: string, holdMs: number }} options
 * @returns {Promise<{ url: string, stats: { held: number, unheld: string[] }, close: () => Promise<void> }>}
 *   `stats.unheld` lists the documents that had no late row to hold back
 */
export async function startLateRowProxy({ origin, holdMs }) {
  const target = new URL(origin);
  const stats = { held: 0, unheld: /** @type {string[]} */ ([]) };

  const server = http.createServer((request, response) => {
    const headers = { ...request.headers, host: target.host };
    // The proxy reads the document, so it asks for an identity encoding.
    delete headers["accept-encoding"];
    const upstream = http.request(
      { host: target.hostname, port: target.port, method: request.method, path: request.url, headers },
      (answer) => {
        const type = String(answer.headers["content-type"] ?? "");
        const isDocument = type.startsWith("text/html") && request.headers.rsc === undefined;
        if (!isDocument) {
          response.writeHead(answer.statusCode ?? 502, answer.headers);
          answer.pipe(response);
          return;
        }
        const chunks = [];
        answer.on("data", (chunk) => chunks.push(chunk));
        answer.on("end", () => {
          const html = Buffer.concat(chunks).toString("utf8");
          const split = splitAtLateRows(html);
          const outgoing = { ...answer.headers };
          delete outgoing["transfer-encoding"];
          delete outgoing["content-length"];
          response.writeHead(answer.statusCode ?? 502, outgoing);
          if (split === null) {
            stats.unheld.push(request.url ?? "");
            response.end(html);
            return;
          }
          stats.held += 1;
          response.write(split.early);
          setTimeout(() => response.end(split.late), holdMs);
        });
      },
    );
    upstream.on("error", (error) => {
      response.writeHead(502);
      response.end(String(error));
    });
    request.pipe(upstream);
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(undefined)));
  const address = /** @type {import("node:net").AddressInfo} */ (server.address());
  return {
    url: `http://127.0.0.1:${address.port}`,
    stats,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve(undefined));
      }),
  };
}
