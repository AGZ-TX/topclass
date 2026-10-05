import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { test } from 'node:test';

test('portable benchmark runs through real MCP and writes verified source results',async t=>{
  const dir=await mkdtemp(join(tmpdir(),'search-benchmark-'));
  const isbn='9780596805524',hash='a'.repeat(32);
  const s=createServer((_req,res)=>res.end(`<div><a href="/md5/${hash}"><h3>Fixture book</h3></a><span>ISBN ${isbn}</span></div><table><tr><td>Author</td><td><a href="ads.php?md5=${hash}">Fixture book</a></td><td>ISBN ${isbn}</td></tr></table><z-bookcard title="Fixture book" isbn="${isbn}" href="/book/1"></z-bookcard>`));
  await new Promise(resolve=>s.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{s.closeAllConnections();s.close();await rm(dir,{recursive:true,force:true});});
  const input=join(dir,'isbns.json'),output=join(dir,'report.json'),url=`http://127.0.0.1:${s.address().port}`;
  await writeFile(input,JSON.stringify([isbn]));
  const env={...process.env,BIBLIO_FETCH_MODE:'http',BIBLIO_ANNAS_MIRRORS:url,BIBLIO_LIBGEN_MIRRORS:url,BIBLIO_ZLIB_MIRRORS:url};
  await promisify(execFile)(process.execPath,['scripts/benchmark.mjs','--input',input,'--output',output,'--label','fixture'],{env,timeout:10000});
  const report=JSON.parse(await readFile(output,'utf8'));
  assert.equal(report.passes[0].withVerifiedIsbn,1);
  assert.equal(report.passes[0].rows[0].status,'complete');
  assert.ok(report.http.length>=3);
  assert.match(report.sourceDigest,/^[a-f0-9]{64}$/);
});
