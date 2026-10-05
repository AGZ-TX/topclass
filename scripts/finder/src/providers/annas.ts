// Anna's Archive provider.
//
// Anna's Archive is the broadest aggregator — it indexes Libgen, Z-Library,
// Sci-Hub/scimag, IPFS, and more. We use it as the primary book source and as
// the detail/metadata resolver for any md5.

import * as cheerio from "cheerio";
import { fetchFromMirrors } from "../http.js";
import { isbnMetadata } from "../isbn.js";
import { validateSearchHtml, validateParsedResults } from "./page.js";
import { ANNAS_MIRRORS } from "../mirrors.js";
import type { Book, DownloadLink } from "../types.js";

const GROUP = "annas";

/** Pull the first metadata line out of a result block and parse loosely. */
function parseMeta(metaText: string): Partial<Book> {
  const out: Partial<Book> = {};
  const year = metaText.match(/\b(1[5-9]\d{2}|20\d{2})\b/);
  if (year) out.year = year[1];
  const fmt = metaText.match(/\b(pdf|epub|mobi|djvu|azw3|cbr|cbz|fb2)\b/i);
  if (fmt) out.format = fmt[1].toUpperCase();
  const size = metaText.match(/(\d+(?:\.\d+)?\s?(?:KB|MB|GB))/i);
  if (size) out.size = size[1].replace(/\s+/, " ");
  const lang = metaText.match(
    /\b(English|Spanish|French|German|Russian|Chinese|Arabic|Portuguese|Italian|Dutch|Japanese|Korean|Turkish|Persian|Hindi|Polish|Ukrainian)\b/i
  );
  if (lang) out.language = lang[1];
  return out;
}

export async function search(query: string, limit: number): Promise<Book[]> {
  const { html, base } = await fetchFromMirrors(GROUP, ANNAS_MIRRORS, (b) =>
    `${b}/search?q=${encodeURIComponent(query)}`, undefined,
    { validate: ({ html, base }) => validateParsedResults(html, parseSearchHtml(html, base, limit).length) }
  );
  return parseSearchHtml(html, base, limit);
}

export function parseSearchHtml(html: string, base: string, limit: number): Book[] {
  const $ = cheerio.load(html);
  const books: Book[] = [];
  const seen = new Set<string>();

  // Each record is an <a href="/md5/HASH"> block. Anna's ships some results
  // inside HTML comments (lazy-render); strip comment markers first so the
  // parser sees them too.
  const normalized = html.replace(/<!--/g, "").replace(/-->/g, "");
  const $$ = cheerio.load(normalized);

  // The live page also has a recent-download ticker with unrelated /md5 links.
  // Restrict modern pages to result containers, preserving older layouts.
  $$(".js-recent-downloads-container").remove();
  const resultLists = $$(".js-aarecord-list-outer");
  const links = resultLists.length ? resultLists.find('a[href^="/md5/"]') : $$('a[href^="/md5/"]');
  links.each((_i, el) => {
    if (books.length >= limit) return false;
    const href = $$(el).attr("href") || "";
    const md5 = href.match(/\/md5\/([a-f0-9]{32})/)?.[1];
    if (!md5 || seen.has(md5)) return;

    const block = $$(el);
    const text = block.text().replace(/\s+/g, " ").trim();
    // Title is the most prominent text node; fall back to the block text.
    const title =
      block.find("h3").first().text().trim() ||
      block.find(".text-xl, .font-bold, .text-lg").first().text().trim() ||
      text.slice(0, 120);
    if (!title) return;

    const record = block.closest(".js-aarecord-list-outer").length
      ? block.parentsUntil(".js-aarecord-list-outer").last()
      : block.parent();
    const coverUrl = record.find("img").first().attr("src") || undefined;
    // Title, cover and ISBN metadata are siblings within the same result row.
    const metaText = record.text().replace(/\s+/g, " ").trim();

    seen.add(md5);
    books.push({
      source: "annas",
      md5,
      title,
      url: `${base}/md5/${md5}`,
      coverUrl,
      ...parseMeta(metaText),
      ...isbnMetadata(metaText),
    });
  });

  return books;
}

/** Fetch the md5 detail page and extract structured metadata + links. */
export async function details(
  md5: string
): Promise<Book & { downloadLinks: DownloadLink[] }> {
  const { html, base } = await fetchFromMirrors(GROUP, ANNAS_MIRRORS, (b) =>
    `${b}/md5/${md5}`
  );
  const $ = cheerio.load(html);

  const title = $("h1").first().text().trim() || $("title").text().trim();
  const metaText = $("main, body").text().replace(/\s+/g, " ").trim();

  const downloadLinks = extractDownloadLinks($, base);

  return {
    source: "annas",
    md5,
    title,
    url: `${base}/md5/${md5}`,
    ...parseMeta(metaText),
    ...isbnMetadata(metaText),
    downloadLinks,
  };
}

/**
 * Member fast-download. Per Anna's Archive's own FAQ this is the ONE stable
 * JSON API they offer: `/dyn/api/fast_download.json` (docs live inside the JSON
 * response itself). Everything else — custom search, iterating files — they
 * point at their ElasticSearch/MariaDB dumps and torrent lists instead.
 *
 * Two reasons this matters beyond speed:
 *   1. It returns a direct file URL, skipping the slow-download waiting page.
 *   2. It is a JSON endpoint, so it is not behind the DDoS-Guard JS challenge
 *      that blocks the HTML mirrors from any non-browser client.
 *
 * No key set => returns null and the caller falls through to the scraped links.
 * The key is read from the environment and never logged, echoed, or included
 * in the returned label.
 */
export async function fastDownload(md5: string): Promise<DownloadLink | null> {
  const key = process.env.BIBLIO_ANNAS_API_KEY?.trim();
  if (!key) return null;

  for (const base of ANNAS_MIRRORS) {
    try {
      const res = await fetch(
        `${base}/dyn/api/fast_download.json?md5=${md5}&key=${encodeURIComponent(key)}`,
        { signal: AbortSignal.timeout(20_000) }
      );
      if (!res.ok) continue;
      const data: any = await res.json();
      const url: unknown = data?.download_url ?? data?.url;
      if (typeof url !== "string" || !url) continue;

      // Surface remaining quota when the API reports it, so a run can see it
      // is burning through the daily allowance.
      const left =
        data?.account_fast_download_info?.downloads_left ??
        data?.downloads_left;
      const label =
        typeof left === "number"
          ? `Anna's Archive fast download (member, ${left} left today)`
          : "Anna's Archive fast download (member)";

      return { source: "annas", label, url, direct: true };
    } catch {
      // Dead or blocked mirror — try the next one.
    }
  }
  return null;
}

function extractDownloadLinks(
  $: cheerio.CheerioAPI,
  base: string
): DownloadLink[] {
  const links: DownloadLink[] = [];
  $("a").each((_i, el) => {
    const href = $(el).attr("href") || "";
    const text = $(el).text().replace(/\s+/g, " ").trim();
    const isDownload =
      /\/(slow_download|fast_download|download)\//.test(href) ||
      /ipfs/i.test(href) ||
      /^download/i.test(text) ||
      /download now|option #/i.test(text.toLowerCase());
    if (!isDownload) return;
    const url = href.startsWith("http") ? href : `${base}${href}`;
    links.push({
      source: "annas",
      label: text.slice(0, 80) || "download",
      url,
      direct: /ipfs|\.(pdf|epub|mobi|djvu)(\?|$)/i.test(url),
    });
  });
  return links;
}
