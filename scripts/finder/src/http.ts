// Resilient HTTP layer with mirror rotation.
//
// Shadow libraries move between domains constantly and any given mirror may be
// blocked from a given network. Every provider therefore declares a LIST of
// candidate mirrors; we try them in order, remember the one that worked, and
// prefer it next time. Requests stop when mirrors are exhausted or the source
// deadline expires; blocked pages are never treated as empty searches.

const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const DEFAULT_HEADERS: Record<string, string> = {
  "User-Agent": USER_AGENT,
  Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
};

function positiveEnv(name: string, fallback: number, max: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isSafeInteger(value) || value <= 0 || value > max) throw new Error(`${name} must be between 1 and ${max}`);
  return value;
}
export const TIMEOUT_MS = positiveEnv("BIBLIO_TIMEOUT_MS", 20000, 120000);
export const SEARCH_TIMEOUT_MS = positiveEnv("BIBLIO_SEARCH_TIMEOUT_MS", 15000, 120000);
const MIRROR_TIMEOUT_MS = positiveEnv("BIBLIO_MIRROR_TIMEOUT_MS", 5000, 120000);
const MAX_HTML_BYTES = 4 * 1024 * 1024;
const COOLDOWN_MS = 30000;
const preferredMirror = new Map<string, string>();
const unhealthy = new Map<string, { until: number; code: string }>();

export interface MirrorFetchResult { html: string; base: string; finalUrl: string }
export interface MirrorAttempt { base: string; code: string; elapsedMs: number; status?: number }
export class MirrorError extends Error {
  constructor(public code: string, message: string, public attempts: MirrorAttempt[] = []) { super(message); }
}
export interface MirrorOptions {
  validate?: (result: MirrorFetchResult) => void;
  totalTimeoutMs?: number;
  requestTimeoutMs?: number;
  concurrency?: number;
}

/** Classify blocking/error pages even when the server returns HTTP 200. */
export function validatePage(html: string, status = 200): void {
  const title = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] ?? "";
  if (/ddos-guard\/js-challenge|check\.ddos-guard\.net|cf-chl-|checking your browser|just a moment/i.test(html))
    throw new MirrorError("browser_challenge", "Source requires browser verification; plain HTTP cannot complete it");
  if (/captcha|verify you are human|are you a robot/i.test(title) || /(?:g-recaptcha|h-captcha|cf-turnstile)/i.test(html))
    throw new MirrorError("captcha", "Source requires human verification");
  if (/site unavailable|access denied|service unavailable|temporarily unavailable|attention required/i.test(title) ||
      /<h1[^>]*>\s*(?:site unavailable|access denied|service unavailable)/i.test(html) || /unable to access this site/i.test(html))
    throw new MirrorError("access_blocked", "Source returned an unavailable/access-denied page");
  if (/sign in|log in|login/i.test(title) && /type=["']password|name=["']password/i.test(html))
    throw new MirrorError("login_required", "Source requires a signed-in browser session");
  if (status === 429) throw new MirrorError("rate_limited", "Source rate-limited this request");
  if (status === 401) throw new MirrorError("login_required", "Source requires authentication");
  if (status === 403) throw new MirrorError("access_blocked", "Source denied HTTP access");
  if (status < 200 || status >= 300) throw new MirrorError("http_error", `Source returned HTTP ${status}`);
  if (!html.trim()) throw new MirrorError("unexpected_page", "Source returned an empty page");
}

/** A source-wide budget covers all paths, mirrors, headers and response bodies. */
export async function fetchFromMirrors(
  groupKey: string, mirrors: string[], buildPath: (base: string) => string | string[],
  init: RequestInit = {}, options: MirrorOptions = {}
): Promise<MirrorFetchResult> {
  if ((process.env.BIBLIO_FETCH_MODE ?? "http") !== "http") {
    const { fetchMode, fetchBrowserMirrors } = await import("./browser.js");
    if (fetchMode() === "browser") return fetchBrowserMirrors(groupKey, mirrors, buildPath, options);
  }
  const budget = options.totalTimeoutMs ?? SEARCH_TIMEOUT_MS;
  const attemptTimeout = Math.min(options.requestTimeoutMs ?? MIRROR_TIMEOUT_MS, TIMEOUT_MS);
  const concurrency = options.concurrency ?? 2;
  if (!Number.isSafeInteger(budget) || budget <= 0 || !Number.isSafeInteger(attemptTimeout) || attemptTimeout <= 0 ||
      !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 4) throw new Error("Invalid mirror request limits");
  const start = performance.now();
  const preferred = preferredMirror.get(groupKey);
  const ordered = [...new Set(mirrors)].sort((a, b) => Number(b === preferred) - Number(a === preferred));
  const attempts: MirrorAttempt[] = [];
  const eligible = ordered.filter(base => {
    const bad = unhealthy.get(base);
    if (bad && bad.until > Date.now()) { attempts.push({ base, code: `cooldown:${bad.code}`, elapsedMs: 0 }); return false; }
    if (bad) unhealthy.delete(base);
    return true;
  });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), budget);
  const externalAbort = () => controller.abort();
  init.signal?.addEventListener("abort", externalAbort, { once: true });
  if (init.signal?.aborted) controller.abort();
  let cursor = 0;
  let winner: MirrorFetchResult | undefined;
  async function worker(): Promise<void> {
    while (!winner && !controller.signal.aborted && cursor < eligible.length) {
      const base = eligible[cursor++];
      const paths = buildPath(base);
      for (const url of Array.isArray(paths) ? paths : [paths]) {
        if (winner || controller.signal.aborted) break;
        const began = performance.now();
        const request = new AbortController();
        const cancel = () => request.abort();
        controller.signal.addEventListener("abort", cancel, { once: true });
        const timeout = setTimeout(cancel, attemptTimeout);
        let status: number | undefined;
        try {
          const res = await fetch(url, { ...init, redirect: "follow", signal: request.signal,
            headers: { ...DEFAULT_HEADERS, ...(init.headers as object) } });
          status = res.status;
          const html = (await readLimitedBody(res, MAX_HTML_BYTES)).toString("utf8");
          validatePage(html, status);
          const result = { html, base, finalUrl: res.url || url };
          options.validate?.(result);
          if (!winner) { winner = result; preferredMirror.set(groupKey, base); unhealthy.delete(base); controller.abort(); }
          return;
        } catch (e) {
          if (winner) return;
          const code = e instanceof MirrorError ? e.code : request.signal.aborted ? "timeout" : "network_error";
          attempts.push({ base, code, elapsedMs: Math.round(performance.now() - began), status });
          // Wrong endpoint/HTML layout can be repaired by the next path on this
          // mirror. Host-wide failures/challenges cannot; avoid hammering it.
          if (!["unexpected_page", "http_error"].includes(code) || (status !== undefined && status >= 500)) {
            unhealthy.set(base, { until: Date.now() + COOLDOWN_MS, code });
            break;
          }
        } finally {
          clearTimeout(timeout); controller.signal.removeEventListener("abort", cancel);
        }
      }
    }
  }
  try {
    await Promise.all(Array.from({ length: Math.min(concurrency, eligible.length) }, () => worker()));
    if (winner) return winner;
    const code = controller.signal.aborted ? "deadline_exceeded" : attempts.some(a => /browser_challenge/.test(a.code)) ? "browser_challenge" : "sources_unavailable";
    throw new MirrorError(code, `${groupKey} unavailable after ${Math.round(performance.now() - start)}ms; ${attempts.map(a => `${a.base}: ${a.code}`).join("; ")}`, attempts);
  } finally {
    clearTimeout(timer); init.signal?.removeEventListener("abort", externalAbort);
  }
}

/** Full-body timeout and size limit also apply to individual HTML requests. */
export async function getText(url: string, init: RequestInit = {}): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const cancel = () => controller.abort();
  init.signal?.addEventListener("abort", cancel, { once: true });
  if (init.signal?.aborted) controller.abort();
  try {
    const res = await fetch(url, { ...init, signal: controller.signal, redirect: "follow", headers: { ...DEFAULT_HEADERS, ...(init.headers as object) } });
    const html = (await readLimitedBody(res, MAX_HTML_BYTES)).toString("utf8");
    validatePage(html, res.status);
    return html;
  } finally { clearTimeout(timer); init.signal?.removeEventListener("abort", cancel); }
}

export const MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024;

/** Limit bytes actually received (including chunked/decompressed bodies). */
export async function readLimitedBody(res: Response, maxBytes: number): Promise<Buffer> {
  const declared = res.headers.get("content-length");
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > maxBytes)) {
    await res.body?.cancel();
    throw new Error(`Download exceeds the ${maxBytes}-byte limit`);
  }
  if (!res.body) throw new Error("Empty download response");
  const reader = res.body.getReader();
  const chunks: Buffer[] = [];
  let total = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) throw new Error(`Download exceeds the ${maxBytes}-byte limit`);
      chunks.push(Buffer.from(value));
    }
    return Buffer.concat(chunks, total);
  } catch (e) {
    await reader.cancel().catch(() => {});
    throw e;
  } finally {
    reader.releaseLock();
  }
}

/** Size and timeout limits cover the entire download, not just headers. */
export async function getBuffer(
  url: string,
  init: RequestInit = {},
  limits: { maxBytes?: number; timeoutMs?: number } = {}
): Promise<{ buffer: Buffer; contentType: string | null }> {
  const maxBytes = limits.maxBytes ?? MAX_DOWNLOAD_BYTES;
  const timeoutMs = limits.timeoutMs ?? TIMEOUT_MS;
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0 || !Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) {
    throw new Error("Download size and timeout limits must be positive integers");
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const abort = () => controller.abort();
  init.signal?.addEventListener("abort", abort, { once: true });
  if (init.signal?.aborted) controller.abort();
  try {
    const res = await fetch(url, {
      ...init,
      redirect: "follow",
      signal: controller.signal,
      headers: { ...DEFAULT_HEADERS, ...(init.headers as object) },
    });
    if (!res.ok) {
      await res.body?.cancel();
      throw new Error(`HTTP ${res.status} for download`);
    }
    const contentType = res.headers.get("content-type");
    if (/html|json|javascript|text\/plain/i.test(contentType ?? "")) {
      await res.body?.cancel();
      throw new Error("Server returned a page or error response, not a book");
    }
    const buffer = await readLimitedBody(res, maxBytes);
    return { buffer, contentType };
  } finally {
    clearTimeout(timer);
    init.signal?.removeEventListener("abort", abort);
  }
}
