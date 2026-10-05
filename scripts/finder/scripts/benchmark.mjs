import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const args = process.argv.slice(2);
const supported = new Set(['--label','--concurrency','--runs','--input','--output']);
const options = {};
for (let i=0;i<args.length;i+=2) {
  if(!supported.has(args[i]) || args[i+1]===undefined) throw new Error('Usage: npm run benchmark -- --label residential [--concurrency 13] [--runs 2] [--input isbns.json] [--output report.json]');
  options[args[i]]=args[i+1];
}
const label=options['--label'] ?? 'local';
if(!/^[a-zA-Z0-9_-]{1,40}$/.test(label)) throw new Error('label must contain 1–40 letters, numbers, underscores or hyphens');
const concurrency=Number(options['--concurrency'] ?? 3), runs=Number(options['--runs'] ?? 1);
if(!Number.isInteger(concurrency)||concurrency<1||concurrency>13||!Number.isInteger(runs)||runs<1||runs>2) throw new Error('concurrency must be 1–13; runs must be 1–2');
const isbns=JSON.parse(await readFile(resolve(options['--input'] ?? join(here,'benchmark-isbns.json')),'utf8'));
if(!Array.isArray(isbns)||!isbns.length||isbns.length>100||isbns.some(x=>typeof x!=='string'||!x.trim())) throw new Error('input must be a JSON array of 1–100 ISBN/query strings');
const output=resolve(options['--output'] ?? join(root,'benchmark-results',`${label}-${new Date().toISOString().replace(/[:.]/g,'-')}.json`));
let commit='unknown'; try { commit=execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim(); } catch {}
async function sourceDigest() {
  const digest=createHash('sha256');
  async function walk(dir) {
    for(const entry of (await readdir(dir,{withFileTypes:true})).sort((a,b)=>a.name.localeCompare(b.name))) {
      const path=join(dir,entry.name);
      if(entry.isDirectory()) await walk(path);
      else { digest.update(path.slice(root.length)); digest.update(await readFile(path)); }
    }
  }
  await walk(join(root,'src')); await walk(join(root,'scripts'));
  digest.update(await readFile(join(root,'package.json')));digest.update(await readFile(join(root,'package-lock.json')));
  return digest.digest('hex');
}
const env=Object.fromEntries(Object.entries(process.env).filter(([,v])=>v!==undefined));
const transport=new StdioClientTransport({command:process.execPath,args:['--import',join(here,'trace-fetch.mjs'),join(root,'dist/index.js')],cwd:root,env,stderr:'pipe'});
const http=[];let pending='';
transport.stderr.on('data', chunk=>{
  pending+=chunk.toString();const lines=pending.split('\n');pending=lines.pop();
  for(const line of lines){try{const v=JSON.parse(line);if(v.benchmarkHttp)http.push(v);}catch{}}
});
const client=new Client({name:'isbn-network-benchmark',version:'1.0.0'});
const report={label,startedAt:new Date().toISOString(),commit,sourceDigest:await sourceDigest(),node:process.version,platform:process.platform,concurrency,
  configuration:{requestTimeoutMs:Number(env.BIBLIO_MIRROR_TIMEOUT_MS??5000),sourceBudgetMs:Number(env.BIBLIO_SEARCH_TIMEOUT_MS??(env.BIBLIO_FETCH_MODE==='browser'?30000:15000)),fetchMode:env.BIBLIO_FETCH_MODE??'http',mirrorOverridesConfigured:['BIBLIO_ANNAS_MIRRORS','BIBLIO_LIBGEN_MIRRORS','BIBLIO_ZLIB_MIRRORS'].filter(k=>env[k])},
  passes:[],http,limitations:'Search only. Candidates without isbnMatch=verified are not confirmed editions. Blocked sources do not establish absence from their catalog. Comparing networks requires the same commit/configuration/concurrency. Warm unavailable lookups can be fast because of circuit cooldown, not successful retrieval.'};
const startup=performance.now();
try {
  await client.connect(transport);report.startupMs=Math.round(performance.now()-startup);
  for(let pass=0;pass<runs;pass++){
    console.log(`Pass ${pass+1}/${runs}: ${isbns.length} queries, concurrency ${concurrency}, label ${label}`);
    const started=performance.now(),rows=new Array(isbns.length);let cursor=0;
    await Promise.all(Array.from({length:Math.min(concurrency,isbns.length)},async()=>{
      while(cursor<isbns.length){
        const index=cursor++,isbn=isbns[index],began=performance.now();let data;
        try{
          const response=await client.callTool({name:'search_books',arguments:{query:isbn,limit:20,refresh:pass===0}},undefined,{timeout:Math.max(30000,report.configuration.sourceBudgetMs+20000)});
          data=response.isError?{toolError:response.content}:JSON.parse(response.content[0].text);
        }catch(e){data={toolError:e.message};}
        rows[index]={isbn,wallMs:Math.round(performance.now()-began),...data};
        console.log(`${isbn}: ${(rows[index].wallMs/1000).toFixed(2)}s | ${data.status??'tool_error'} | ${data.results?.length??0} candidates | ${data.results?.filter(b=>b.isbnMatch==='verified').length??0} verified | ${data.errors?.map(e=>`${e.source}:${e.code}`).join(', ')??''}`);
      }
    }));
    report.passes.push({pass:pass+1,wallMs:Math.round(performance.now()-started),withCandidates:rows.filter(r=>r.results?.length).length,withVerifiedIsbn:rows.filter(r=>r.results?.some(b=>b.isbnMatch==='verified')).length,rows});
    console.log(`Batch: ${(report.passes.at(-1).wallMs/1000).toFixed(2)}s; ${report.passes.at(-1).withVerifiedIsbn}/${isbns.length} verified ISBNs`);
  }
} finally {
  await client.close().catch(()=>{});report.endedAt=new Date().toISOString();
  await mkdir(dirname(output),{recursive:true,mode:0o700});await writeFile(output,JSON.stringify(report,null,2)+'\n',{mode:0o600});
  console.log(`Report saved: ${output}`);
}
