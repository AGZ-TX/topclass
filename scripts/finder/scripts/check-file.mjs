import { readFile } from 'node:fs/promises';
import { validateBook } from '../dist/download.js';

try {
  const [path, md5, contentType] = process.argv.slice(2);
  const format = validateBook(await readFile(path), md5, contentType);
  process.stdout.write(JSON.stringify({ format }) + '\n');
} catch {
  process.stderr.write('File validation failed\n');
  process.exitCode = 1;
}
