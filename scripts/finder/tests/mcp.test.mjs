import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

test('MCP download tool validates files and refuses overwrite end to end', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'search-mcp-'));
  const pdf = Buffer.from('%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n');
  const hash = createHash('md5').update(pdf).digest('hex');
  let corrupt = false, downloadCalls = 0;
  const server = createServer((req, res) => {
    if (req.url.startsWith('/ads.php')) res.end(`<a href="/get.php?md5=${hash}">GET</a>`);
    else if (req.url.startsWith('/get.php')) {
      downloadCalls++;
      res.writeHead(200, { 'content-type': 'application/octet-stream' });
      res.end(corrupt ? Buffer.from('MZcorrupt') : pdf);
    } else res.end('<html><h1>Test fixture</h1></html>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const mirror = `http://127.0.0.1:${server.address().port}`;
  const env = Object.fromEntries(Object.entries(process.env).filter(([,v]) => v !== undefined));
  delete env.BIBLIO_ANNAS_API_KEY;
  Object.assign(env, { BIBLIO_DOWNLOAD_ROOT: root, BIBLIO_LIBGEN_MIRRORS: mirror, BIBLIO_ANNAS_MIRRORS: mirror });
  const transport = new StdioClientTransport({ command: process.execPath, args: ['dist/index.js'], env, stderr: 'pipe' });
  const client = new Client({ name: 'regression-test', version: '1.0.0' });
  t.after(async () => {
    await client.close(); server.closeAllConnections(); server.close(); await rm(root, { recursive: true, force: true });
  });
  await client.connect(transport);
  const tools = await client.listTools();
  assert.equal(tools.tools.length, 7);
  const call = (args) => client.callTool({ name: 'download_book', arguments: { md5: hash, output_dir: root, ...args } });
  const first = JSON.parse((await call({ filename: 'book.pdf' })).content[0].text);
  assert.equal(first.saved, true); assert.equal(first.md5Verified, true); assert.equal(first.format, 'pdf');
  assert.deepEqual(await readFile(first.path), pdf);
  const again = JSON.parse((await call({ filename: 'book.pdf' })).content[0].text);
  assert.equal(again.saved, false); assert.match(again.reason, /overwrite/);
  assert.deepEqual(await readFile(first.path), pdf);
  const count = downloadCalls;
  const traversal = await call({ filename: '../escape.pdf' });
  assert.equal(traversal.isError, true); assert.equal(downloadCalls, count);
  corrupt = true;
  const invalid = JSON.parse((await call({ filename: 'corrupt.pdf' })).content[0].text);
  assert.equal(invalid.saved, false); assert.ok(invalid.errors.some(e => /MD5/.test(e)));
});
