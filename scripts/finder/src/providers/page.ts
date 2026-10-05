import * as cheerio from "cheerio";
import { MirrorError } from "../http.js";

/** Unknown/challenge/layout-changed pages must not become "no books found". */
export function validateSearchHtml(html: string, selector: string): void {
  const $ = cheerio.load(html.replace(/<!--/g, "").replace(/-->/g, ""));
  if ($(selector).length) return;
  $("script, style, nav, footer").remove();
  const text = $("body").text().replace(/\s+/g, " ").trim();
  if (/no (?:matching )?(?:books|files|results|records)(?:\s+(?:were )?found)?|nothing found|found\s*:?\s*0\s*(?:books|files|results)|0\s+(?:books|files|results)\s+found|no matches/i.test(text)) return;
  throw new MirrorError("unexpected_page", "Source page has neither recognizable search results nor an explicit no-results message");
}

/** Recognizable markup with no usable parsed records is a parser error. */
export function validateParsedResults(html: string, count: number): void {
  if (count > 0) return;
  validateSearchHtml(html, "__no_matching_search_records__");
}
