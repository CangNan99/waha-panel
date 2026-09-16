import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

export function extractInlineScripts(appSource) {
  return [...appSource.matchAll(/<script>\s*([\s\S]*?)\s*<\/script>/g)].map((match) =>
    match[1].replace(/^\s*<!-- SPONSOR_SCRIPT -->\s*$/m, ''),
  );
}

export function checkInlineScripts(appSource, filename = 'panel/app.py', minimumScripts = 1) {
  const scripts = extractInlineScripts(appSource);
  if (scripts.length < minimumScripts) {
    throw new Error(`expected ${filename} to contain at least ${minimumScripts} inline script(s)`);
  }
  scripts.forEach((source, index) => {
    new vm.Script(source, { filename: `${filename} inline script ${index + 1}` });
  });
  return scripts.length;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const appPath = new URL('../panel/app.py', import.meta.url);
  const chatPath = new URL('../panel/chat_page.py', import.meta.url);
  const appSource = await readFile(appPath, 'utf8');
  const chatSource = await readFile(chatPath, 'utf8');
  const appCount = checkInlineScripts(appSource, fileURLToPath(appPath), 3);
  const chatCount = checkInlineScripts(chatSource, fileURLToPath(chatPath));
  console.log(`Validated JavaScript syntax in ${appCount + chatCount} inline scripts.`);
}
