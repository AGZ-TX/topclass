import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';

// Set mirrors before importing provider configuration; all fixtures are local.
let calls=0;
const hash='a'.repeat(32);
const s=createServer((req,res)=>{
  calls++;
  const q=new URL(req.url,'http://test').searchParams.get('req');
  if(q==='broken'){res.end('<title>Site Unavailable</title>');return;}
  if(q==='empty'){res.end('<p>No books found</p>');return;}
  const isbn=q==='9780321618528' ? '9780596805524' : q==='9780596806750' ? '' : '9780596805524';
  res.end(`<table><tr><td>Author</td><td><a href="ads.php?md5=${hash}">Fixture book</a></td><td>ISBN: ${isbn} PDF 1 MB</td></tr></table>`);
});
await new Promise(resolve=>s.listen(0,'127.0.0.1',resolve));
process.env.BIBLIO_LIBGEN_MIRRORS=`http://127.0.0.1:${s.address().port}`;
const { searchBooks, searchBooksBatch }=await import('../dist/providers/index.js');

test('verified ISBNs come from record metadata; unknown/different editions stay explicit', async () => {
  const valid=await searchBooks('9780596805524',['libgen'],20);
  assert.equal(valid.results[0].isbnMatch,'verified');assert.equal(valid.status,'complete');
  assert.equal((await searchBooks('9780321618528',['libgen'],20)).results[0].isbnMatch,'different');
  assert.equal((await searchBooks('9780596806750',['libgen'],20)).results[0].isbnMatch,'unverified');
});
test('cache avoids duplicate requests and refresh forces a new valid lookup', async()=>{
  const start=calls;
  const cached=await searchBooks('9780596805524',['libgen'],20);assert.equal(cached.cached,true);assert.equal(calls,start);
  await searchBooks('9780596805524',['libgen'],20,true);assert.equal(calls,start+1);
});
test('in-flight duplicate ISBN variants coalesce into one network lookup',async()=>{
  const start=calls;
  await Promise.all(Array.from({length:8},(_,i)=>searchBooks(i%2?'0-596-80552-7':'9780596805524',['libgen'],19,true)));
  assert.equal(calls,start+1);
});
test('batch retains input order and repeated queries share cached results',async()=>{
  const result=await searchBooksBatch(['9780596805524','9780596806750','9780596805524'],['libgen'],20,2);
  assert.deepEqual(result.searches.map(x=>x.query),['9780596805524','9780596806750','9780596805524']);
  assert.ok(result.searches.every(x=>x.cached));
});
test('explicit empty results are valid; unavailable pages are surfaced as errors',async()=>{
  assert.equal((await searchBooks('empty',['libgen'],20)).status,'complete');
  const bad=await searchBooks('broken',['libgen'],20);assert.equal(bad.status,'unavailable');assert.equal(bad.errors[0].attempts[0].code,'access_blocked');assert.equal(bad.cached,false);
});
test('invalid inputs fail before making requests',async()=>{
  const before=calls;
  await assert.rejects(searchBooks('  ',['libgen'],20),/empty/);
  await assert.rejects(searchBooks('book',[],20),/source/);
  await assert.rejects(searchBooksBatch(['book'],['libgen'],20,0),/concurrency/);
  assert.equal(calls,before);
});
test.after(()=>{s.closeAllConnections();s.close();});
