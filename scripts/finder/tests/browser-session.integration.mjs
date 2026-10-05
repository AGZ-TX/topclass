import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);

// Real Chromium, loopback only, and a disposable profile. Never inspect the
// user's profile, cookies or credentials.
test('dedicated profile preserves cookies, localStorage and IndexedDB across processes', async t => {
  const profile = await mkdtemp(join(tmpdir(), 'search-session-test-'));
  const server = createServer((req, res) => {
    if (req.url === '/set') res.setHeader('Set-Cookie', 'session_test=fixture; Max-Age=86400; HttpOnly; SameSite=Lax; Path=/');
    if (req.url === '/check' && req.headers.cookie !== 'session_test=fixture') {
      res.writeHead(401); res.end('missing fixture session'); return;
    }
    res.setHeader('Content-Type', 'text/html');
    res.end('<!doctype html><title>Local session fixture</title>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(profile, {recursive:true,force:true}); });
  const env = {...process.env, BIBLIO_BROWSER_PROFILE:profile, BIBLIO_BROWSER_HEADLESS:'true',
    SESSION_TEST_ORIGIN:`http://127.0.0.1:${server.address().port}`};
  const prefix = `
    import assert from 'node:assert/strict';
    import {browserContext, closeBrowser} from './dist/browser.js';
    const context = await browserContext();
    const page = await context.newPage();
  `;
  await run(process.execPath, ['--input-type=module', '-e', prefix + `
    try {
      await page.goto(process.env.SESSION_TEST_ORIGIN + '/set');
      await page.evaluate(async () => {
        localStorage.setItem('fixture', 'saved');
        await new Promise((resolve, reject) => {
          const request = indexedDB.open('fixture', 1);
          request.onupgradeneeded = () => request.result.createObjectStore('state');
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const db = request.result, tx = db.transaction('state', 'readwrite');
            tx.objectStore('state').put('saved', 'fixture');
            tx.oncomplete = () => { db.close(); resolve(); };
            tx.onerror = () => reject(tx.error);
          };
        });
      });
    } finally { await closeBrowser(); }
  `], {env, timeout:30000});
  await run(process.execPath, ['--input-type=module', '-e', prefix + `
    try {
      assert.equal((await page.goto(process.env.SESSION_TEST_ORIGIN + '/check')).status(), 200);
      assert.equal(await page.evaluate(() => localStorage.getItem('fixture')), 'saved');
      assert.equal(await page.evaluate(() => new Promise((resolve, reject) => {
        const request = indexedDB.open('fixture', 1);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result;
          const get = db.transaction('state').objectStore('state').get('fixture');
          get.onsuccess = () => { db.close(); resolve(get.result); };
          get.onerror = () => reject(get.error);
        };
      })), 'saved');
      // Closing the actual browser (as a user would) must not leave a dead
      // context cached in the long-running MCP process.
      await context.close();
      const reopened = await browserContext();
      assert.notEqual(reopened, context);
      const fresh = await reopened.newPage();
      assert.equal((await fresh.goto(process.env.SESSION_TEST_ORIGIN + '/check')).status(), 200);
    } finally { await closeBrowser(); }
  `], {env, timeout:30000});
  assert.equal((await stat(profile)).mode & 0o777, 0o700);
});

test('a visible profile already in use produces an actionable error without deleting it', {skip: !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY}, async () => {
  const profile = await mkdtemp(join(tmpdir(), 'search-lock-test-'));
  try {
    await run(process.execPath, ['--input-type=module', '-e', `
      import assert from 'node:assert/strict';
      import {chromium} from 'playwright';
      import {execFile} from 'node:child_process';
      import {promisify} from 'node:util';
      const first = await chromium.launchPersistentContext(process.env.BIBLIO_BROWSER_PROFILE,
        {headless:false,chromiumSandbox:true,acceptDownloads:false});
      try {
        await promisify(execFile)(process.execPath, ['--input-type=module', '-e',
          "import assert from 'node:assert/strict'; import {browserContext,closeBrowser} from './dist/browser.js'; try { await assert.rejects(browserContext(), error => error.code === 'browser_profile_in_use'); } finally { await closeBrowser(); }"],
          {env:process.env,timeout:15000});
      } finally { await first.close(); }
    `], {env:{...process.env,BIBLIO_BROWSER_PROFILE:profile,BIBLIO_BROWSER_HEADLESS:'false'},timeout:30000});
  } finally { await rm(profile,{recursive:true,force:true}); }
});
