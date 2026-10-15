import { readFile, readdir } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
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
  let modules=0;
  async function checkModules(directory){
    for(const item of await readdir(directory,{withFileTypes:true})){
      const path=new URL(item.name+(item.isDirectory()?'/':''),directory);
      if(item.isDirectory())await checkModules(path);
      else if(item.name.endsWith('.js')){
        const source=await readFile(path,'utf8');
        const result=spawnSync(process.execPath,['--check','--input-type=module'],{input:source,encoding:'utf8'});
        if(result.status!==0)throw new Error(fileURLToPath(path)+': '+result.stderr);
        modules++;
      }
    }
  }
  await checkModules(new URL('../panel/static/',import.meta.url));
  console.log(`Validated JavaScript syntax in ${appCount + chatCount} inline scripts and ${modules} modules.`);
}
