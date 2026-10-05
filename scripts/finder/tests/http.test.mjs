import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { getBuffer, readLimitedBody, MAX_DOWNLOAD_BYTES } from '../dist/http.js';

test('default download ceiling is 100 MiB', () => assert.equal(MAX_DOWNLOAD_BYTES, 100 * 1024 * 1024));
test('enforces actual body bytes with absent or misleading content-length', async () => {
  for (const headers of [{}, { 'content-length': '1' }]) {
    await assert.rejects(readLimitedBody(new Response('12345', { headers }), 4), /limit/);
  }
  assert.equal((await readLimitedBody(new Response('1234'), 4)).toString(), '1234');
});
test('oversized declared length rejects before reading and cancels stream', async () => {
  let cancelled = false;
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  await assert.rejects(readLimitedBody(new Response(body, { headers: { 'content-length': '999' } }), 4), /limit/);
  assert.equal(cancelled, true);
});
test('chunked overflow cancels the stream', async () => {
  let cancelled = false;
  const body = new ReadableStream({
    pull(controller) { controller.enqueue(new Uint8Array(3)); },
    cancel() { cancelled = true; },
  });
  await assert.rejects(readLimitedBody(new Response(body), 4), /limit/);
  assert.equal(cancelled, true);
});
async function server(t, handler) {
  const s = createServer(handler);
  await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  t.after(() => { s.closeAllConnections(); s.close(); });
  return `http://127.0.0.1:${s.address().port}`;
}
test('deadline aborts a response that sends headers then stalls', async (t) => {
  const url = await server(t, (_req, res) => { res.writeHead(200); res.flushHeaders(); res.write('part'); });
  const start = Date.now();
  await assert.rejects(getBuffer(url, {}, { timeoutMs: 100 }), /abort/i);
  assert.ok(Date.now() - start < 2000);
});
test('downloads binary bytes and rejects HTML regardless of body size', async (t) => {
  const url = await server(t, (req, res) => {
    if (req.url === '/html') { res.writeHead(200, { 'content-type': 'text/html' }); res.end('x'.repeat(60000)); }
    else { res.writeHead(200, { 'content-type': 'application/octet-stream' }); res.end('1234'); }
  });
  assert.equal((await getBuffer(url)).buffer.toString(), '1234');
  await assert.rejects(getBuffer(url + '/html'), /page/);
  await assert.rejects(getBuffer(url, {}, { maxBytes: 3 }), /limit/);
});
test('rejects invalid limits and honors pre-aborted requests', async () => {
  await assert.rejects(getBuffer('https://example.invalid', {}, { maxBytes: NaN }), /positive/);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(getBuffer('https://example.invalid', { signal: controller.signal }), /abort/i);
});
