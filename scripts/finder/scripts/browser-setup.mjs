import { createInterface } from 'node:readline/promises';
import { browserContext, browserProfile, closeBrowser } from '../dist/browser.js';
import { ANNAS_MIRRORS, LIBGEN_MIRRORS, ZLIB_MIRRORS } from '../dist/mirrors.js';
if (!process.stdin.isTTY) throw new Error('Run browser-setup in a terminal on your Linux desktop so you can complete verification manually.');
// Setup is an interactive desktop flow, even if the MCP server uses headless mode.
process.env.BIBLIO_BROWSER_HEADLESS = 'false';
const context = await browserContext();
const prompt = createInterface({input:process.stdin,output:process.stdout});
const abort = new AbortController();
const cancel = () => abort.abort();
process.once('SIGINT', cancel);
process.once('SIGTERM', cancel);
try {
  console.log(`Dedicated local browser profile: ${browserProfile()}`);
  console.log('Existing cookies and site storage are reused from this profile. Close other search sessions before setup.');
  console.log('Complete any normal browser verification in the opened tabs. Sign in yourself if a site requires it.');
  for(const url of [ANNAS_MIRRORS[0],LIBGEN_MIRRORS[0],ZLIB_MIRRORS[0]].filter(Boolean)) {
    if (abort.signal.aborted) break;
    const page=await context.newPage();
    await page.goto(url,{waitUntil:'domcontentloaded',timeout:15000}).catch(()=>console.log(`Navigation needs your attention: ${url}`));
  }
  await prompt.question('When the tabs are ready, press Enter here to save the local session and close the browser. ', {signal:abort.signal});
} catch (error) {
  if (error.name !== 'AbortError') throw error;
} finally {
  process.removeListener('SIGINT', cancel);
  process.removeListener('SIGTERM', cancel);
  prompt.close();
  await closeBrowser();
}
console.log('Browser profile saved locally. Run npm run start:browser to reuse it; no cookie export is needed.');
