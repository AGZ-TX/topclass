import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readBrowserPage, fetchMode, browserProfile, fetchBrowserMirrors, withBrowserSlot } from '../dist/browser.js';
import { validateSearchHtml } from '../dist/providers/page.js';
const valid='<a href="/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Book</a>';
const validate=({html})=>validateSearchHtml(html,'a[href^="/md5/"]');
function page(contents,status=200){let pos=0;return {goto:async()=>({status:()=>status}),content:async()=>contents[Math.min(pos++,contents.length-1)],url:()=> 'https://fixture.test/search'};}

test('browser mode defaults to HTTP and rejects invalid options',()=>{
  assert.equal(fetchMode(),'http');const old=process.env.BIBLIO_FETCH_MODE;
  process.env.BIBLIO_FETCH_MODE='invalid';assert.throws(()=>fetchMode(),/must be/);
  if(old===undefined)delete process.env.BIBLIO_FETCH_MODE;else process.env.BIBLIO_FETCH_MODE=old;
  const before=process.env.BIBLIO_BROWSER_PROFILE;process.env.BIBLIO_BROWSER_PROFILE='relative';assert.throws(()=>browserProfile(),/absolute/);
  if(before===undefined)delete process.env.BIBLIO_BROWSER_PROFILE;else process.env.BIBLIO_BROWSER_PROFILE=before;
});
test('browser DOM rendering can complete a normal JavaScript verification',async()=>{
  const result=await readBrowserPage(page(['<title>DDoS-Guard</title><script src="https://check.ddos-guard.net/check.js"></script>',valid],403),'https://fixture.test/search','https://fixture.test',1000,validate);
  assert.equal(result.html,valid);
});
test('an unresolved browser challenge reports an actionable error',async()=>{
  await assert.rejects(readBrowserPage(page(['<title>Captcha</title>']),'https://fixture.test/search','https://fixture.test',50,validate),e=>e.code==='captcha');
});
test('unavailable browser pages and failed navigation never count as empty searches',async()=>{
  await assert.rejects(readBrowserPage(page(['<title>Site Unavailable</title>']),'https://fixture.test','https://fixture.test',50,validate),e=>e.code==='access_blocked');
  const failed={...page([valid]),goto:async()=>{throw new Error('failed');}};
  await assert.rejects(readBrowserPage(failed,'https://fixture.test','https://fixture.test',50,validate),e=>e.code==='timeout');
});
test('invalid browser budgets reject before attempting to launch a browser',async()=>{
  await assert.rejects(fetchBrowserMirrors('test',[],b=>b,{totalTimeoutMs:NaN}),/Invalid/);
});


test('a stalled browser source cannot starve the other two sources; tabs stay bounded', async () => {
  let releaseFirst;
  const stalled = new Promise(resolve => { releaseFirst = resolve; });
  let active = 0, peak = 0;
  const started = [];
  const job = (id, wait = Promise.resolve()) => withBrowserSlot(async () => {
    started.push(id); peak = Math.max(peak, ++active);
    try { await wait; return id; } finally { active--; }
  });
  const first = job('annas', stalled);
  const second = job('libgen');
  const third = job('zlibrary');
  const fourth = job('next-query');
  try {
    assert.deepEqual(started, ['annas', 'libgen', 'zlibrary']);
    assert.deepEqual(await Promise.all([second, third, fourth]), ['libgen', 'zlibrary', 'next-query']);
    assert.equal(peak, 3);
  } finally { releaseFirst(); await first; }
});

test('a failed browser request releases its slot to waiting work', async () => {
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  const running = [1, 2, 3].map(() => withBrowserSlot(async () => { await wait; throw new Error('source failed'); }));
  const failures = running.map(p => assert.rejects(p, /source failed/));
  const queued = withBrowserSlot(async () => 'recovered');
  release();
  await Promise.all(failures);
  assert.equal(await queued, 'recovered');
});
