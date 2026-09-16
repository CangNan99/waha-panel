import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

export function extractInlineScripts(appSource) {
  return [...appSource.matchAll(/<script>\s*([\s\S]*?)\s*<\/script>/g)].map((match) =>
    match[1].replace(/^\s*<!-- SPONSOR_SCRIPT -->\s*$/m, ''),
  );
}

export function checkInlineScripts(appSource, filename = 'panel/app.py') {
  const scripts = extractInlineScripts(appSource);
  if (scripts.length < 3) {
    throw new Error('expected the panel templates to contain at least three inline scripts');
  }
  scripts.forEach((source, index) => {
    new vm.Script(source, { filename: `${filename} inline script ${index + 1}` });
  });
  return scripts.length;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const appPath = new URL('../panel/app.py', import.meta.url);
  const appSource = await readFile(appPath, 'utf8');
  const count = checkInlineScripts(appSource, fileURLToPath(appPath));
  console.log(`Validated JavaScript syntax in ${count} inline scripts.`);
}
