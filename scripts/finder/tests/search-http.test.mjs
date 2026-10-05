import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { fetchFromMirrors, validatePage } from '../dist/http.js';
import { validateSearchHtml } from '../dist/providers/page.js';

async function server(t, handler) {
  const s = createServer(handler); await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  t.after(() => { s.closeAllConnections(); s.close(); }); return `http://127.0.0.1:${s.address().port}`;
}
const options = { totalTimeoutMs: 1000, requestTimeoutMs: 500, concurrency: 1 };
const known = { ...options, validate: ({html}) => validateSearchHtml(html, 'a[href*="md5="]') };
const result = '<table><tr><td><a href="ads.php?md5=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">A book</a></td></tr></table>';

test('classifies HTTP-200 unavailable, browser challenge, CAPTCHA and login pages', () => {
  for (const [html, code] of [
    ['<title>Site Unavailable</title><p>Unable to access this site.</p>', 'access_blocked'],
    ['<title>DDoS-Guard</title><script src="/.well-known/ddos-guard/js-challenge/index.js"></script>', 'browser_challenge'],
    ['<title>Captcha</title>', 'captcha'],
    ['<title>Sign in</title><input type="password">', 'login_required'],
  ]) assert.throws(() => validatePage(html), e => e.code === code);
  assert.throws(() => validatePage('wait',429), e => e.code === 'rate_limited');
});
test('unknown pages fail; explicit no-result pages and known records validate', () => {
  assert.throws(() => validateSearchHtml('<h1>Welcome</h1>', 'a[href*="md5="]'), e => e.code === 'unexpected_page');
  validateSearchHtml('<p>No books found</p>', 'a[href*="md5="]');
  validateSearchHtml(result, 'a[href*="md5="]');
});
test('HTTP-200 unavailable page falls through to a working mirror', async t => {
  const bad = await server(t, (_req,res) => res.end('<title>Site Unavailable</title>'));
  const good = await server(t, (_req,res) => res.end(result));
  assert.equal((await fetchFromMirrors('fallback', [bad,good], b => b, undefined, known)).base, good);
});
test('a wrong endpoint tries the alternate path on the same mirror', async t => {
  const paths = [];
  const url = await server(t, (req,res) => { paths.push(req.url); res.end(req.url === '/search.php' ? result : '<h1>Welcome</h1>'); });
  assert.equal((await fetchFromMirrors('paths', [url], b => [b+'/index.php',b+'/search.php'], undefined, known)).finalUrl, url+'/search.php');
  assert.deepEqual(paths, ['/index.php','/search.php']);
});
test('timeouts after headers fall back and honor a source-wide deadline', async t => {
  const slow = await server(t, (_req,res) => { res.flushHeaders(); res.write('part'); });
  const good = await server(t, (_req,res) => res.end(result));
  const start = performance.now();
  assert.equal((await fetchFromMirrors('timeout', [slow,good], b => b, undefined, { ...known, requestTimeoutMs: 60 })).base, good);
  assert.ok(performance.now() - start < 800);
  const hanging = await server(t, (_req,res) => { res.flushHeaders(); });
  const began = performance.now();
  await assert.rejects(fetchFromMirrors('budget', [hanging], b => b, undefined, { totalTimeoutMs:80, requestTimeoutMs:500 }), e => e.code === 'deadline_exceeded');
  assert.ok(performance.now()-began < 600);
});
test('blocked hosts enter cooldown instead of being repeatedly requested', async t => {
  let calls=0;
  const url=await server(t, (_req,res) => { calls++; res.end('<title>Site Unavailable</title>'); });
  await assert.rejects(fetchFromMirrors('cooldown', [url], b=>b, undefined, options));
  await assert.rejects(fetchFromMirrors('cooldown', [url], b=>b, undefined, options), e => e.attempts[0].code === 'cooldown:access_blocked');
  assert.equal(calls,1);
});
test('the first healthy response cancels a stalled parallel mirror', async t => {
  const slow=await server(t, (_req,res) => { res.flushHeaders(); });
  const good=await server(t, (_req,res) => res.end(result));
  const start=performance.now();
  assert.equal((await fetchFromMirrors('parallel', [slow,good], b=>b, undefined, { ...known, concurrency:2 })).base, good);
  assert.ok(performance.now()-start<500);
});
test('HTML size is bounded and malformed timeout settings reject immediately', async t => {
  const huge=await server(t, (_req,res) => { res.writeHead(200, {'content-length':'999999999'});res.end(); });
  await assert.rejects(fetchFromMirrors('huge',[huge],b=>b,undefined,options));
  await assert.rejects(fetchFromMirrors('bad',[],b=>b,undefined,{totalTimeoutMs:NaN}), /Invalid/);
});
