// Aggregation layer.
//
// Fans a query out to every book provider concurrently, merges the results, and
// dedups by md5 (the universal key) while keeping the richest record. Per-source
// failures are collected, not thrown, so one dead mirror never blanks the search.

import * as annas from "./annas.js";
import * as libgen from "./libgen.js";
import * as scihub from "./scihub.js";
import * as zlibrary from "./zlibrary.js";
import { MirrorError } from "../http.js";
import { normalizeIsbn } from "../isbn.js";
import { IPFS_GATEWAYS } from "../mirrors.js";
import type {
  Book,
  DownloadLink,
  Paper,
  SearchResult,
  SourceId,
  SourceError,
} from "../types.js";

export const BOOK_SOURCES: SourceId[] = ["annas", "libgen", "zlibrary"];

const bookSearchers: Record<
  string,
  (q: string, limit: number) => Promise<Book[]>
> = {
  annas: annas.search,
  libgen: libgen.search,
  zlibrary: zlibrary.search,
};

/** Merge two records for the same md5, preferring non-empty fields. */
function mergeBook(a: Book, b: Book): Book {
  const pick = <K extends keyof Book>(k: K) => a[k] || b[k];
  return {
    ...a,
    title: a.title.length >= b.title.length ? a.title : b.title,
    author: pick("author"),
    publisher: pick("publisher"),
    year: pick("year"),
    language: pick("language"),
    format: pick("format"),
    size: pick("size"),
    pages: pick("pages"),
    isbn: pick("isbn"),
    isbns: [...new Set([...(a.isbns ?? []), ...(b.isbns ?? [])])],
    coverUrl: pick("coverUrl"),
    url: pick("url"),
    mirrors: [...(a.mirrors ?? []), ...(b.mirrors ?? [])],
  };
}

const searchCache = new Map<string, { expires: number; result: SearchResult<Book> }>();
const pendingSearches = new Map<string, Promise<SearchResult<Book>>>();

/** Coalesce identical requests and cache only complete, validated responses. */
export async function searchBooks(
  query: string, sources: SourceId[], limit: number, refresh = false
): Promise<SearchResult<Book>> {
  const isbn = normalizeIsbn(query.trim());
  if (!isbn && /^(?:isbn(?:-1[03])?\s*:?\s*)?[\dXx\s-]{10,}$/i.test(query.trim())) throw new Error("Invalid ISBN checksum or length");
  const normalized = isbn ?? query.trim();
  if (!normalized) throw new Error("Search query must not be empty");
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error("Search limit must be between 1 and 100");
  const active = [...new Set(sources)].filter(s => s in bookSearchers).sort();
  if (!active.length) throw new Error("Select at least one book source");
  const key = JSON.stringify([normalized, active, limit]);
  const cached = searchCache.get(key);
  if (!refresh && cached && cached.expires > Date.now()) return { ...structuredClone(cached.result), elapsedMs: 0, cached: true };
  searchCache.delete(key);
  const pending = pendingSearches.get(key);
  if (pending) return structuredClone(await pending);
  const promise = runSearch(normalized, active, limit, isbn);
  pendingSearches.set(key, promise);
  try {
    const result = await promise;
    if (!result.errors.length) {
      if (searchCache.size >= 100) searchCache.delete(searchCache.keys().next().value!);
      searchCache.set(key, { expires: Date.now() + (result.results.length ? 300000 : 30000), result: structuredClone(result) });
    }
    return result;
  } finally { pendingSearches.delete(key); }
}

async function runSearch(query: string, active: SourceId[], limit: number, isbn?: string): Promise<SearchResult<Book>> {
  const start = performance.now();
  const sourceTimings: NonNullable<SearchResult<Book>["sourceTimings"]> = [];
  const settled = await Promise.allSettled(active.map(async source => {
    const began = performance.now();
    try {
      const results = await bookSearchers[source](query, limit);
      sourceTimings.push({ source, elapsedMs: Math.round(performance.now() - began), status: "ok" });
      return results;
    } catch (e) {
      sourceTimings.push({ source, elapsedMs: Math.round(performance.now() - began), status: "error" });
      throw e;
    }
  }));
  const errors: SourceError[] = [];
  const byMd5 = new Map<string, Book>();
  const noMd5: Book[] = [];
  settled.forEach((r, i) => {
    const source = active[i];
    if (r.status === "rejected") {
      errors.push({ source, error: String(r.reason?.message ?? r.reason),
        code: r.reason instanceof MirrorError ? r.reason.code : "provider_error",
        attempts: r.reason instanceof MirrorError ? r.reason.attempts : undefined });
      return;
    }
    for (const book of r.value) {
      if (book.md5) { const existing = byMd5.get(book.md5); byMd5.set(book.md5, existing ? mergeBook(existing, book) : book); }
      else noMd5.push(book);
    }
  });
  const results = [...byMd5.values(), ...noMd5];
  if (isbn) for (const book of results) {
    const known = [...(book.isbns ?? []), ...(book.isbn ? [book.isbn] : [])].map(normalizeIsbn).filter(Boolean);
    book.isbnMatch = known.includes(isbn) ? "verified" : known.length ? "different" : "unverified";
  }
  return { query, results, errors, status: errors.length === active.length ? "unavailable" : errors.length ? "partial" : "complete",
    elapsedMs: Math.round(performance.now() - start), cached: false, sourceTimings: sourceTimings.sort((a, b) => a.source.localeCompare(b.source)) };
}

export async function searchBooksBatch(queries: string[], sources: SourceId[], limit: number, concurrency = 3, refresh = false) {
  if (!queries.length || queries.length > 100 || queries.some(q => !q.trim())) throw new Error("Provide 1 to 100 nonempty queries");
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 6) throw new Error("Batch concurrency must be between 1 and 6");
  const start = performance.now();
  const results: SearchResult<Book>[] = new Array(queries.length);
  let cursor = 0;
  await Promise.all(Array.from({ length: Math.min(concurrency, queries.length) }, async () => {
    while (cursor < queries.length) {
      const index = cursor++;
      results[index] = await searchBooks(queries[index], sources, limit, refresh);
    }
  }));
  return { elapsedMs: Math.round(performance.now() - start), concurrency, searches: results };
}

/** Resolve every download candidate we can find for an md5. */
export async function resolveDownloads(md5: string): Promise<DownloadLink[]> {
  return (await resolveDownloadsWithDiagnostics(md5)).links;
}

export async function resolveDownloadsWithDiagnostics(md5: string) {
  const links: DownloadLink[] = [];
  const errors: SourceError[] = [];

  const [fastRes, libgenRes, annasRes] = await Promise.allSettled([
    annas.fastDownload(md5),
    libgen.downloadLinks(md5),
    annas.details(md5),
  ]);

  // Member fast-download goes first when available: it is a direct file URL and
  // the only path that survives the DDoS-Guard challenge on the HTML mirrors.
  // Resolves to null when no API key is configured, so unsubscribed setups are
  // unaffected.
  if (fastRes.status === "fulfilled" && fastRes.value) links.push(fastRes.value);
  if (libgenRes.status === "fulfilled") links.push(...libgenRes.value);
  if (annasRes.status === "fulfilled") links.push(...annasRes.value.downloadLinks);
  for (const [source, result] of [["libgen", libgenRes], ["annas", annasRes]] as const) {
    if (result.status === "rejected") errors.push({
      source, error: "Source could not resolve file links",
      code: result.reason instanceof MirrorError ? result.reason.code : "provider_error",
      attempts: result.reason instanceof MirrorError ? result.reason.attempts : undefined,
    });
  }


  // Surface an IPFS CID as gateway links if one appears among Anna's links.
  const cid = links
    .map((l) => l.url.match(/\/ipfs\/([A-Za-z0-9]+)/)?.[1])
    .find(Boolean);
  if (cid) {
    for (const gw of IPFS_GATEWAYS) {
      const url = `${gw}/${cid}`;
      if (!links.some((l) => l.url === url))
        links.push({ source: "ipfs", label: "IPFS gateway", url, direct: true });
    }
  }

  return { links, errors };
}

export { annas, libgen, scihub, zlibrary };
export type { Book, Paper, DownloadLink, SearchResult, SourceId };
