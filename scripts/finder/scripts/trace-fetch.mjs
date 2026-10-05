// Passive diagnostics. No cookies, authorization headers, API keys, or IP
// addresses are collected. Keep the native fetch behavior unchanged.
const fetchOriginal = globalThis.fetch;
let recorded = 0;
globalThis.fetch = async (...args) => {
  const started = performance.now();
  const raw = String(args[0]);
  const url = new URL(raw);
  for (const key of ['key', 'token', 'api_key']) if (url.searchParams.has(key)) url.searchParams.set(key, '[redacted]');
  const emit = fields => {
    if (recorded++ < 1000) process.stderr.write(JSON.stringify({ benchmarkHttp: true, url: url.toString(), elapsedMs: Math.round(performance.now()-started), ...fields }) + '\n');
  };
  try {
    const response = await fetchOriginal(...args);
    emit({ status: response.status, server: response.headers.get('server') });
    return response;
  } catch (e) {
    emit({ error: e.message, cause: e.cause?.code ?? e.cause?.message });
    throw e;
  }
};
