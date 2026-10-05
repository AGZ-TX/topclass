import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normalizeIsbn, isbnMetadata } from '../dist/isbn.js';

test('normalizes valid hyphenated ISBNs and rejects invalid checksums', () => {
  assert.equal(normalizeIsbn('ISBN-13: 978-0-596-80552-4'), '9780596805524');
  assert.equal(normalizeIsbn('0-596-80552-7'), '9780596805524');
  assert.equal(normalizeIsbn('9780596805520'), undefined);
  assert.equal(normalizeIsbn('a book title'), undefined);
});
test('extracts only valid record ISBNs and deduplicates ISBN-10/13 equivalents', () => {
  assert.deepEqual(isbnMetadata('ISBN: 978-0-596-80552-4; 0-596-80552-7; 9780596805520'), {isbn:'9780596805524',isbns:['9780596805524']});
  assert.deepEqual(isbnMetadata('2010 PDF 123 MB'), {});
});
