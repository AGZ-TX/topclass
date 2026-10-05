import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';

test('Z-Library defaults use the verified mirrors and retain operator overrides', () => {
  const env = { ...process.env }; delete env.BIBLIO_ZLIB_MIRRORS;
  const read = () => JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', 'import { ZLIB_MIRRORS } from "./dist/mirrors.js"; process.stdout.write(JSON.stringify(ZLIB_MIRRORS));'], { env, encoding: 'utf8' }));
  assert.deepEqual(read(), ['https://z-lib.sk', 'https://z-library.sk']);
  env.BIBLIO_ZLIB_MIRRORS = 'https://operator.example/';
  assert.deepEqual(read(), ['https://operator.example']);
});
