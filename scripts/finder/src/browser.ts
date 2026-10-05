import { mkdir, chmod } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { MirrorError, validatePage, type MirrorFetchResult, type MirrorOptions, type MirrorAttempt } from "./http.js";

export function fetchMode(): "http" | "browser" {
  const mode = process.env.BIBLIO_FETCH_MODE ?? "http";
  if (mode !== "http" && mode !== "browser") throw new Error("BIBLIO_FETCH_MODE must be http or browser");
  return mode;
}
export function browserProfile(): string {
  const path = process.env.BIBLIO_BROWSER_PROFILE ?? join(homedir(), ".local", "share", "search", "browser");
  if (!isAbsolute(path)) throw new Error("BIBLIO_BROWSER_PROFILE must be absolute");
  return resolve(path);
}
export interface BrowserContext {
  newPage(): Promise<SearchPage & { close(): Promise<void> }>;
  close(): Promise<void>;
}
let contextPromise: Promise<BrowserContext> | undefined;
export async function browserContext(): Promise<BrowserContext> {
  if (!contextPromise) contextPromise = (async () => {
    const path = browserProfile();
    try {
      await mkdir(path, { recursive: true, mode: 0o700 });
      await chmod(path, 0o700);
      const packageName = "playwright";
      const { chromium } = await import(packageName);
      // A separate, visible local profile. No stealth flags, CAPTCHA solving,
      // credential extraction, proxy rotation, or attachment to a daily browser.
      const context = await chromium.launchPersistentContext(path, {
        headless: process.env.BIBLIO_BROWSER_HEADLESS === "true",
        chromiumSandbox: true,
        acceptDownloads: false,
        timeout: 15000,
      });
      const opening = contextPromise;
      context.on("close", () => {
        if (contextPromise === opening) contextPromise = undefined;
      });
      return context;
    } catch (error) {
      contextPromise = undefined;
      if (/ProcessSingleton|SingletonLock|profile.*in use/i.test(String(error))) {
        throw new MirrorError("browser_profile_in_use", "The dedicated browser profile is already open. Close the other search server or browser-setup session, then retry. Do not delete the profile or its lock files.");
      }
      throw new MirrorError("browser_unavailable", "Browser could not start. Install Node 20+, run npm ci and npx playwright install chromium, then npm run browser-setup on your Linux desktop. Check display, browser dependencies and sandbox support; the sandbox is not disabled automatically.");
    }
  })();
  return contextPromise;
}
export async function closeBrowser(): Promise<void> {
  const pending = contextPromise; contextPromise = undefined;
  if (pending) await (await pending).close();
}

export interface SearchPage {
  goto(url: string, options: { waitUntil: "domcontentloaded"; timeout: number }): Promise<{ status(): number } | null>;
  content(): Promise<string>;
  url(): string;
}
/** Wait for normal page rendering or manual verification, under one deadline. */
export async function readBrowserPage(page: SearchPage, url: string, base: string, timeoutMs: number, validate?: MirrorOptions["validate"]): Promise<MirrorFetchResult> {
  const deadline = performance.now() + timeoutMs;
  let status = 200;
  try { status = (await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs }))?.status() ?? 200; }
  catch { throw new MirrorError("timeout", "Browser navigation timed out or failed"); }
  let last: unknown;
  while (performance.now() < deadline) {
    try {
      const html = await page.content();
      if (Buffer.byteLength(html) > 4 * 1024 * 1024) throw new MirrorError("page_too_large", "Browser page exceeds HTML size limit");
      // DOM validation after rendering avoids treating the initial 403 on a
      // successfully completed verification flow as the current document status.
      validatePage(html);
      if (status >= 400 && (status !== 403 || !validate)) validatePage(html, status);
      const result = { html, base, finalUrl: page.url() };
      validate?.(result);
      return result;
    } catch (e) {
      last = e;
      if (!(e instanceof MirrorError) || !["browser_challenge", "captcha", "login_required", "unexpected_page"].includes(e.code)) throw e;
      await new Promise(resolve => setTimeout(resolve, Math.min(200, Math.max(0, deadline-performance.now()))));
    }
  }
  if (last instanceof MirrorError) throw last;
  throw new MirrorError("timeout", "Browser page was not ready before the search deadline");
}

// Allow all three book sources to start together. A single shared slot lets
// the first blocked source consume every queued source's deadline.
let activeBrowserRequests = 0;
const waitingBrowserRequests: (() => void)[] = [];
export async function withBrowserSlot<T>(job: () => Promise<T>): Promise<T> {
  if (activeBrowserRequests >= 3) await new Promise<void>(resolve => waitingBrowserRequests.push(resolve));
  else activeBrowserRequests++;
  try { return await job(); }
  finally {
    const next = waitingBrowserRequests.shift();
    if (next) next();
    else activeBrowserRequests--;
  }
}
export function fetchBrowserMirrors(groupKey: string, mirrors: string[], buildPath: (base: string) => string | string[], options: MirrorOptions = {}): Promise<MirrorFetchResult> {
  const queuedAt = performance.now();
  const budget = options.totalTimeoutMs ?? Number(process.env.BIBLIO_SEARCH_TIMEOUT_MS ?? 30000);
  if (!Number.isSafeInteger(budget) || budget < 1 || budget > 120000) return Promise.reject(new Error("Invalid browser search budget"));
  return withBrowserSlot(async () => {
    const remaining = () => budget - (performance.now()-queuedAt);
    if (remaining() <= 0) throw new MirrorError("deadline_exceeded", "Browser queue exceeded the source deadline");
    const ctx = await browserContext();
    const page = await ctx.newPage();
    const attempts: MirrorAttempt[] = [];
    try {
      for (const base of [...new Set(mirrors)]) {
        const paths = buildPath(base);
        for (const url of Array.isArray(paths) ? paths : [paths]) {
          if (remaining() <= 0) throw new MirrorError("deadline_exceeded", `${groupKey} browser deadline exceeded`, attempts);
          const start = performance.now();
          try { return await readBrowserPage(page, url, base, Math.max(1,Math.floor(remaining())), options.validate); }
          catch (e) {
            attempts.push({base,code:e instanceof MirrorError?e.code:"browser_error",elapsedMs:Math.round(performance.now()-start)});
            if (e instanceof MirrorError && ["browser_challenge", "captcha", "login_required"].includes(e.code)) break;
          }
        }
      }
      throw new MirrorError("sources_unavailable", `${groupKey} browser mirrors unavailable; complete verification in npm run browser-setup and retry`, attempts);
    } finally { await page.close().catch(()=>{}); }
  });
}
