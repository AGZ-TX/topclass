import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, symlink, mkdir, readdir, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { saveBook, validateBook, validateFilename, validateOutputDirectory } from '../dist/download.js';

const pdf = Buffer.from('%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n');
const md5 = (b) => createHash('md5').update(b).digest('hex');

// Stored ZIP fixtures: no third-party downloads or book contents are needed.
function epub(extraName) {
  const entries = [['mimetype', 'application/epub+zip'], ['META-INF/container.xml', '<container/>']];
  if (extraName) entries.push([extraName, 'x']);
  const locals = [], centrals = [];
  let offset = 0;
  for (const [name, text] of entries) {
    const n = Buffer.from(name), body = Buffer.from(text), local = Buffer.alloc(30), central = Buffer.alloc(46);
    local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4);
    local.writeUInt32LE(body.length, 18); local.writeUInt32LE(body.length, 22); local.writeUInt16LE(n.length, 26);
    central.writeUInt32LE(0x02014b50); central.writeUInt16LE(20, 6);
    central.writeUInt32LE(body.length, 20); central.writeUInt32LE(body.length, 24); central.writeUInt16LE(n.length, 28);
    central.writeUInt32LE(offset, 42);
    const record = Buffer.concat([local, n, body]);
    locals.push(record); centrals.push(Buffer.concat([central, n])); offset += record.length;
  }
  const directory = Buffer.concat(centrals), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}

async function root(t) {
  const dir = await mkdtemp(join(tmpdir(), 'search-test-'));
  const before = process.env.BIBLIO_DOWNLOAD_ROOT;
  process.env.BIBLIO_DOWNLOAD_ROOT = join(dir, 'books');
  t.after(async () => {
    if (before === undefined) delete process.env.BIBLIO_DOWNLOAD_ROOT;
    else process.env.BIBLIO_DOWNLOAD_ROOT = before;
    await rm(dir, { recursive: true, force: true });
  });
  return { dir, books: process.env.BIBLIO_DOWNLOAD_ROOT };
}

test('valid PDF passes byte checks and MD5 verification', () => {
  assert.equal(validateBook(pdf, md5(pdf), 'application/octet-stream'), 'pdf');
  assert.equal(validateBook(pdf, md5(pdf).toUpperCase()), 'pdf');
});
test('rejects tampered bytes, empty bodies, HTML of any size, and MIME spoofing', () => {
  assert.throws(() => validateBook(Buffer.concat([pdf, Buffer.from('changed')]), md5(pdf)), /MD5/);
  assert.throws(() => validateBook(Buffer.alloc(0), md5(Buffer.alloc(0))), /Empty/);
  for (const body of [Buffer.from('<html>error</html>'), Buffer.from('<html>' + 'x'.repeat(60000))]) {
    assert.throws(() => validateBook(body, md5(body), 'text/html'), /page/);
    assert.throws(() => validateBook(body, md5(body), 'application/pdf'), /Unsupported/);
  }
  for (const body of [Buffer.from('MZmalware'), Buffer.from('#!/bin/sh\necho bad'), Buffer.from('PK\x03\x04not an epub'), pdf.subarray(0, 25)]) {
    assert.throws(() => validateBook(body, md5(body), 'application/pdf'), /Unsupported/);
  }
});
test('EPUB needs a valid directory and cannot contain traversal entries', () => {
  const valid = epub();
  assert.equal(validateBook(valid, md5(valid)), 'epub');
  for (const bad of [valid.subarray(0, -1), epub('../escape'), epub('C:/escape')]) {
    assert.throws(() => validateBook(bad, md5(bad)), /Unsupported/);
  }
});
test('recognizes structured MOBI and DjVu headers, rejects truncated forms', () => {
  const mobi = Buffer.alloc(110); mobi.write('BOOKMOBI', 60); mobi.writeUInt16BE(1, 76); mobi.writeUInt32BE(86, 78); mobi.write('MOBI', 102);
  assert.equal(validateBook(mobi, md5(mobi)), 'mobi');
  const djvu = Buffer.alloc(20); djvu.write('AT&TFORM'); djvu.writeUInt32BE(8, 8); djvu.write('DJVU', 12);
  assert.equal(validateBook(djvu, md5(djvu)), 'djvu');
  for (const b of [mobi.subarray(0, 85), djvu.subarray(0, 16)]) assert.throws(() => validateBook(b, md5(b)), /Unsupported/);
});
test('rejects traversal, absolute paths, hidden names, streams, and unsafe extensions', () => {
  for (const name of ['../escape.pdf', '..\\escape.pdf', '/tmp/a.pdf', 'C:\\x.pdf', '.bashrc', 'a.pdf:stream', 'x.exe', 'a.pdf\0', 'CON.pdf', 'x.pdf.', 'x.pdf ']) {
    assert.throws(() => validateFilename(name), /filename/);
  }
  validateFilename('A book.PDF');
});
test('saves valid files privately and never overwrites an existing file', async (t) => {
  const { books } = await root(t);
  const result = await saveBook(pdf, md5(pdf), books, 'book.pdf');
  assert.deepEqual(await readFile(result.path), pdf);
  if (process.platform !== 'win32') assert.equal((await stat(result.path)).mode & 0o777, 0o600);
  await assert.rejects(saveBook(pdf, md5(pdf), books, 'book.pdf'), { code: 'EEXIST' });
  assert.deepEqual(await readFile(result.path), pdf);
});
test('concurrent saves have exactly one winner', async (t) => {
  const { books } = await root(t);
  await mkdir(books);
  const outcomes = await Promise.allSettled(Array.from({ length: 8 }, () => saveBook(pdf, md5(pdf), books, 'book.pdf')));
  assert.equal(outcomes.filter(x => x.status === 'fulfilled').length, 1);
  for (const o of outcomes.filter(x => x.status === 'rejected')) assert.equal(o.reason.code, 'EEXIST');
  assert.deepEqual(await readFile(join(books, 'book.pdf')), pdf);
});
test('refuses destination symlinks without changing their target', async (t) => {
  const { dir, books } = await root(t);
  await mkdir(books); const target = join(dir, 'original'); await writeFile(target, 'keep');
  await symlink(target, join(books, 'book.pdf'));
  await assert.rejects(saveBook(pdf, md5(pdf), books, 'book.pdf'), { code: 'EEXIST' });
  assert.equal(await readFile(target, 'utf8'), 'keep');
});
test('refuses symlinked subdirectories and roots', async (t) => {
  const { dir, books } = await root(t);
  await mkdir(books); const outside = join(dir, 'outside'); await mkdir(outside);
  await symlink(outside, join(books, 'link'), 'dir');
  await assert.rejects(saveBook(pdf, md5(pdf), join(books, 'link')), /symlink/);
  assert.deepEqual(await readdir(outside), []);
  await rm(books, { recursive: true }); await symlink(outside, books, 'dir');
  await assert.rejects(saveBook(pdf, md5(pdf), books), /symlink/);
});
test('rejects paths outside root, including prefix lookalikes, before creating anything', async (t) => {
  const { dir, books } = await root(t);
  for (const output of [dir, books + '-outside', join(books, '..', 'outside'), 'relative']) {
    assert.throws(() => validateOutputDirectory(output), /output_dir/);
    await assert.rejects(saveBook(pdf, md5(pdf), output), /output_dir/);
  }
  assert.deepEqual(await readdir(dir), []);
});
test('rejects traversal and mismatched extension without creating files', async (t) => {
  const { dir, books } = await root(t);
  await assert.rejects(saveBook(pdf, md5(pdf), books, '../escape.pdf'), /filename/);
  await assert.rejects(saveBook(pdf, md5(pdf), books, 'book.epub'), /extension/);
  await assert.rejects(saveBook(pdf, '0'.repeat(32), books), /MD5/);
  assert.deepEqual(await readdir(dir), []);
});
test('valid nested directories and default hash filenames work', async (t) => {
  const { books } = await root(t);
  const output = await saveBook(pdf, md5(pdf), join(books, 'nested', 'books'));
  assert.equal(output.path, join(books, 'nested', 'books', `${md5(pdf)}.pdf`));
});
