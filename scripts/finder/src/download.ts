import { createHash } from "node:crypto";
import { lstat, mkdir, open, realpath, unlink } from "node:fs/promises";
import { homedir } from "node:os";
import { extname, isAbsolute, join, relative, resolve, sep } from "node:path";

export type BookFormat = "pdf" | "epub" | "mobi" | "djvu";

function hasEpubDirectory(buffer: Buffer): boolean {
  const end = buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (end < 58 || end + 22 > buffer.length ||
      end + 22 + buffer.readUInt16LE(end + 20) !== buffer.length ||
      buffer.readUInt16LE(end + 4) !== 0 || buffer.readUInt16LE(end + 6) !== 0 ||
      buffer.readUInt16LE(end + 8) !== buffer.readUInt16LE(end + 10)) return false;
  const start = buffer.readUInt32LE(end + 16);
  if (start < 58 || start + buffer.readUInt32LE(end + 12) !== end) return false;
  const names = new Set<string>();
  let pos = start;
  for (let i = 0; i < buffer.readUInt16LE(end + 10); i++) {
    if (pos + 46 > end || buffer.readUInt32LE(pos) !== 0x02014b50) return false;
    const nameLength = buffer.readUInt16LE(pos + 28);
    const next = pos + 46 + nameLength + buffer.readUInt16LE(pos + 30) + buffer.readUInt16LE(pos + 32);
    if (next > end) return false;
    const name = buffer.subarray(pos + 46, pos + 46 + nameLength).toString("utf8");
    if (!name || name.startsWith("/") || /[\\\x00:]/.test(name) || name.split("/").includes("..") || names.has(name)) return false;
    const local = buffer.readUInt32LE(pos + 42);
    if (local + 30 > start || buffer.readUInt32LE(local) !== 0x04034b50 ||
        local + 30 + buffer.readUInt16LE(local + 26) + buffer.readUInt16LE(local + 28) + buffer.readUInt32LE(pos + 20) > start ||
        (buffer.readUInt16LE(pos + 8) & 1) !== 0) return false;
    names.add(name);
    pos = next;
  }
  return pos === end && names.has("mimetype") && names.has("META-INF/container.xml");
}

/** A catalog MD5 verifies identity, not authenticity or absence of malware. */
export function validateBook(buffer: Buffer, md5: string, contentType?: string | null): BookFormat {
  if (!buffer.length) throw new Error("Empty download");
  if (/html|json|javascript|text\/plain/i.test(contentType ?? "")) {
    throw new Error("Server returned a page or error response, not a book");
  }
  if (!/^[a-f0-9]{32}$/i.test(md5) || createHash("md5").update(buffer).digest("hex") !== md5.toLowerCase()) {
    throw new Error("Downloaded file does not match the requested MD5");
  }

  // These are format checks, not a full parser or antivirus scanner. Never fall
  // back to a MIME type, filename, generic ZIP/RAR, or arbitrary .bin payload.
  if (/^%PDF-(1\.[0-7]|2\.0)[\r\n ]/.test(buffer.subarray(0, 12).toString("latin1")) &&
      /%%EOF\s*$/.test(buffer.subarray(-1024).toString("latin1"))) return "pdf";

  // EPUB requires an uncompressed first ZIP entry named "mimetype" with no
  // extra field and the exact EPUB media type. Also require an end record.
  if (buffer.length >= 80 && buffer.readUInt32LE(0) === 0x04034b50 &&
      (buffer.readUInt16LE(6) & ~0x0800) === 0 && buffer.readUInt16LE(8) === 0 &&
      buffer.readUInt32LE(18) === 20 && buffer.readUInt32LE(22) === 20 &&
      buffer.readUInt16LE(26) === 8 && buffer.readUInt16LE(28) === 0 &&
      buffer.subarray(30, 58).toString("ascii") === "mimetypeapplication/epub+zip") {
    if (hasEpubDirectory(buffer)) return "epub";
  }

  if (buffer.length >= 86 && buffer.subarray(60, 68).toString("ascii") === "BOOKMOBI") {
    const records = buffer.readUInt16BE(76);
    const offset = buffer.readUInt32BE(78);
    if (records > 0 && offset >= 78 + records * 8 && offset + 20 <= buffer.length &&
        buffer.subarray(offset + 16, offset + 20).toString("ascii") === "MOBI") return "mobi";
  }
  if (buffer.length >= 16 && buffer.subarray(0, 8).toString("ascii") === "AT&TFORM" &&
      ["DJVU", "DJVM"].includes(buffer.subarray(12, 16).toString("ascii")) &&
      buffer.readUInt32BE(8) === buffer.length - 12) return "djvu";

  throw new Error("Unsupported or malformed book: only PDF, EPUB, MOBI and DjVu are accepted");
}

export function validateFilename(filename: string): void {
  // Reject both platforms' separators, control characters, NTFS streams and
  // hidden/config filenames even when the server is running on Unix.
  if (!filename || filename.length > 200 || /[\\/<>:"|?*\x00-\x1f\x7f]/.test(filename) ||
      filename.startsWith(".") || /[. ]$/.test(filename) ||
      /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(filename)) {
    throw new Error("filename must be a plain, non-hidden filename without path separators");
  }
  if (![".pdf", ".epub", ".mobi", ".djvu"].includes(extname(filename).toLowerCase())) {
    throw new Error("filename must end in .pdf, .epub, .mobi or .djvu");
  }
}

function isWithin(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith(`..${sep}`));
}

export function downloadRoot(): string {
  const root = process.env.BIBLIO_DOWNLOAD_ROOT ?? join(homedir(), "Downloads", "biblio");
  if (!isAbsolute(root)) throw new Error("BIBLIO_DOWNLOAD_ROOT must be an absolute path");
  return resolve(root);
}

export function validateOutputDirectory(outputDir: string): string {
  const root = downloadRoot();
  if (!isAbsolute(outputDir) || !isWithin(root, resolve(outputDir))) {
    throw new Error(`output_dir must be inside the configured download root: ${root}`);
  }
  return resolve(outputDir);
}

async function prepareDirectory(outputDir: string): Promise<string> {
  const dir = validateOutputDirectory(outputDir);
  const root = downloadRoot();
  await mkdir(root, { recursive: true, mode: 0o700 });
  if ((await lstat(root)).isSymbolicLink()) throw new Error("Download root must not be a symlink");
  const canonicalRoot = await realpath(root);
  let current = canonicalRoot;
  for (const part of relative(root, dir).split(sep).filter(Boolean)) {
    current = join(current, part);
    try { await mkdir(current, { mode: 0o700 }); }
    catch (e) { if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e; }
    const stat = await lstat(current);
    if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Download directories must not contain symlinks");
  }
  const canonicalDir = await realpath(current);
  if (!isWithin(canonicalRoot, canonicalDir)) throw new Error("Download directory escapes the configured root");
  return canonicalDir;
}

/** Exclusive creation refuses existing files, hard links and symlinks. */
export async function saveBook(
  buffer: Buffer, md5: string, outputDir: string, filename?: string, contentType?: string | null
): Promise<{ path: string; format: BookFormat }> {
  validateOutputDirectory(outputDir);
  if (filename !== undefined) validateFilename(filename);
  const format = validateBook(buffer, md5, contentType);
  const name = filename ?? `${md5.toLowerCase()}.${format}`;
  if (extname(name).toLowerCase() !== `.${format}`) throw new Error("Filename extension does not match the file contents");
  const dir = await prepareDirectory(outputDir);
  const path = join(dir, name);
  const handle = await open(path, "wx", 0o600);
  try {
    await handle.writeFile(buffer);
    await handle.sync();
  } catch (e) {
    await handle.close();
    await unlink(path).catch(() => {});
    throw e;
  }
  await handle.close();
  return { path, format };
}
