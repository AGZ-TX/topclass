/** Normalize valid ISBN-10/13 identifiers to ISBN-13, never guess from a query. */
export function normalizeIsbn(value: string): string | undefined {
  const raw = value.replace(/^isbn(?:-1[03])?\s*:?\s*/i, "").replace(/[\s-]/g, "").toUpperCase();
  if (/^\d{13}$/.test(raw) && /^(978|979)/.test(raw)) {
    const sum = [...raw.slice(0, 12)].reduce((s, n, i) => s + Number(n) * (i % 2 ? 3 : 1), 0);
    return (10 - sum % 10) % 10 === Number(raw[12]) ? raw : undefined;
  }
  if (/^\d{9}[\dX]$/.test(raw)) {
    const sum = [...raw].reduce((s, n, i) => s + (n === "X" ? 10 : Number(n)) * (10 - i), 0);
    if (sum % 11 !== 0) return undefined;
    const prefix = `978${raw.slice(0, 9)}`;
    const check = [...prefix].reduce((s, n, i) => s + Number(n) * (i % 2 ? 3 : 1), 0);
    return prefix + ((10 - check % 10) % 10);
  }
  return undefined;
}

export function isbnMetadata(text: string): { isbn?: string; isbns?: string[] } {
  const matches = text.match(/\b(?:97[89](?:[\s-]?\d){10}|\d(?:[\s-]?\d){8}[\s-]?[\dXx])\b/g) ?? [];
  const isbns = [...new Set(matches.map(normalizeIsbn).filter((s): s is string => !!s))];
  return isbns.length ? { isbn: isbns[0], isbns } : {};
}
