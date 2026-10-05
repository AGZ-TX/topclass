import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseSearchHtml as parseAnna } from '../dist/providers/annas.js';
import { parseSearchHtml as parseLibgen } from '../dist/providers/libgen.js';
import { parseSearchHtml as parseZlib } from '../dist/providers/zlibrary.js';
import { validateParsedResults } from '../dist/providers/page.js';
const hash='a'.repeat(32);
const isbn='9780596805524';

test('all book parsers retain source-supported ISBN metadata',()=>{
  const a=parseAnna(`<div><a href="/md5/${hash}"><h3>Fixture book</h3></a><span>ISBN ${isbn}</span></div>`,'https://fixture.test',20);
  const l=parseLibgen(`<table><tr><td>Author</td><td><a href="ads.php?md5=${hash}">Fixture book</a></td><td>ISBN ${isbn}</td></tr></table>`,'https://fixture.test',20);
  const z=parseZlib(`<z-bookcard title="Fixture book" href="/book/1" isbn="${isbn}"></z-bookcard>`,'https://fixture.test',20);
  for(const books of [a,l,z]) { assert.equal(books.length,1);assert.equal(books[0].isbn,isbn); }
});
test('recognized but malformed result markup cannot become a successful empty search',()=>{
  const html='<z-bookcard href="/book/1"></z-bookcard>';
  const parsed=parseZlib(html,'https://fixture.test',20);assert.equal(parsed.length,0);
  assert.throws(()=>validateParsedResults(html,parsed.length),e=>e.code==='unexpected_page');
});


test('Anna search excludes the live recent-download ticker before applying the result limit', () => {
  const html = `<div class="js-recent-downloads-container"><div class="js-recent-downloads-scroll">
    <a href="/md5/${'b'.repeat(32)}">Unrelated recent download</a></div></div>
    <div class="js-aarecord-list-outer"><div class="flex">
      <a href="/md5/${hash}"><img src="/cover.jpg"></a>
      <div><div><a href="/md5/${hash}" class="text-lg">Actual search result</a></div>
      <span>ISBN ${isbn} PDF 2010</span></div>
    </div><div><a href="/md5/${"c".repeat(32)}">Other edition</a><span>ISBN 9781593273880</span></div></div>`;
  const books = parseAnna(html, 'https://fixture.test', 1);
  assert.equal(books.length, 1);
  assert.equal(books[0].title, 'Actual search result');
  assert.equal(books[0].md5, hash);
  assert.equal(books[0].isbn, isbn);
  assert.deepEqual(books[0].isbns, [isbn]);
  assert.equal(books[0].format, 'PDF');
  assert.equal(books[0].coverUrl, '/cover.jpg');
});

test('Anna recent downloads cannot turn an empty or unknown search page into candidates', () => {
  const ticker = `<div class="js-recent-downloads-container"><a href="/md5/${hash}">Unrelated book</a></div>`;
  const empty = ticker + '<p>No books found</p>';
  assert.deepEqual(parseAnna(empty, 'https://fixture.test', 20), []);
  validateParsedResults(empty, 0);
  assert.throws(() => validateParsedResults(ticker, parseAnna(ticker, 'https://fixture.test', 20).length),
    e => e.code === 'unexpected_page');
});
