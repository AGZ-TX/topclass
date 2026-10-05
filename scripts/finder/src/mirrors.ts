// Centralized mirror registry.
//
// These domains rotate over time. When a source stops working, this is the ONE
// file to update — add/reorder hosts here and every provider picks it up. Order
// = preference; the HTTP layer will fall through to later entries automatically.
//
// Overridable at runtime via comma-separated env vars, e.g.:
//   BIBLIO_ANNAS_MIRRORS="https://annas-archive.org,https://annas-archive.se"

function fromEnv(name: string, fallback: string[]): string[] {
  const raw = process.env[name];
  if (!raw) return fallback;
  return raw
    .split(",")
    .map((s) => s.trim().replace(/\/+$/, ""))
    .filter(Boolean);
}

// Refreshed 2026-08-28. The old defaults were stale and mostly dead:
//   .org — taken offline by the registrar 2026-01-11
//   .li  — permanently deleted 2026-03-01 under publisher legal pressure
//   .se  — no longer resolving
// Anna's Archive tells users to check its Wikipedia article for the current
// list, since these rotate under takedown pressure. Verify there before editing;
// do not trust the SEO "current links" blogspam, which is where fake mirrors live.
export const ANNAS_MIRRORS = fromEnv("BIBLIO_ANNAS_MIRRORS", [
  "https://annas-archive.gl",
  "https://annas-archive.gd",
  "https://annas-archive.pk",
]);

export const LIBGEN_MIRRORS = fromEnv("BIBLIO_LIBGEN_MIRRORS", [
  "https://libgen.li",
  "https://libgen.is",
  "https://libgen.rs",
  "https://libgen.st",
  "https://libgen.gs",
  "https://libgen.vg",
]);

// Checked 2026-10-02 against the project's own announcements/profile:
// https://x.com/Z_Lib_official/status/2017161551325827501
// https://x.com/Z_Lib_official (links to z-lib.sk)
// Official status is separate from availability: automated access may be gated.
// Never restore z-lib.io or add mirrors from SEO "current links" pages.
export const ZLIB_MIRRORS = fromEnv("BIBLIO_ZLIB_MIRRORS", [
  "https://z-lib.sk",
  "https://z-library.sk",
]);

export const SCIHUB_MIRRORS = fromEnv("BIBLIO_SCIHUB_MIRRORS", [
  "https://sci-hub.ren",
  "https://sci-hub.mksa.top",
  "https://sci-hub.hkvisa.net",
  "https://sci-hub.se",
  "https://sci-hub.st",
  "https://sci-hub.ru",
]);

// Public IPFS gateways used as a last-resort download path for records that
// expose an IPFS CID (common on Anna's Archive).
export const IPFS_GATEWAYS = fromEnv("BIBLIO_IPFS_GATEWAYS", [
  "https://ipfs.io/ipfs",
  "https://cloudflare-ipfs.com/ipfs",
  "https://gateway.pinata.cloud/ipfs",
]);
