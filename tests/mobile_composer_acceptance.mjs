// Real templates + synthetic local API. No business data or external messages.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)('playwright');
const base=process.env.PANEL_UI_TEST_URL;
const cdp=process.env.PANEL_BROWSER_CDP;
if(!base?.startsWith('http://127.0.0.1:')||!cdp)throw new Error('Use the local synthetic fixture and agent-browser CDP');
const browser=await chromium.connectOverCDP(cdp);
const page=await browser.contexts()[0].newPage();
const evidence=process.env.PANEL_UI_EVIDENCE;
if(evidence)await mkdir(evidence,{recursive:true});
async function checkComposer(label){
  await page.locator('#composerInput').waitFor();
  await page.waitForFunction(()=>document.getElementById('shellContent').getBoundingClientRect().bottom<=Math.min(innerHeight,visualViewport?.height??innerHeight)+1);
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const bounds=await page.evaluate(()=>{
    const root=document.querySelector('[data-view-host]:not([hidden])').shadowRoot;
    const content=document.getElementById('shellContent').getBoundingClientRect();
    const field=root.getElementById('composerInput');
    const send=root.getElementById('sendButton');
    const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height};};
    const input=rect(field),button=rect(send);
    const fits=r=>r.y>=content.top-1&&r.bottom<=Math.min(content.bottom,innerHeight)+1&&r.x>=0&&r.right<=innerWidth;
    const hit=root.elementFromPoint(input.x+input.width/2,input.y+input.height/2);
    return {input,button,viewport:{width:innerWidth,height:innerHeight},content:{top:content.top,bottom:content.bottom},inputFits:fits(input),buttonFits:fits(button),inputHit:hit===field};
  });
  if(evidence)await page.screenshot({path:evidence+'/composer-'+label+'.png'});
  assert.ok(bounds.inputFits&&bounds.buttonFits&&bounds.inputHit,label+': composer must be visible and tappable: '+JSON.stringify(bounds));
  await page.locator('#composerInput').fill('手机输入框验收草稿');
  assert.equal(await page.locator('#composerInput').inputValue(),'手机输入框验收草稿');
  console.log('PASS visible and tappable composer '+label);
}
try{
  for(const [width,height]of [[390,844],[320,568],[375,812],[430,932],[812,375],[1440,900]]){
    await page.setViewportSize({width,height});
    await page.goto(base+'/sessions/support/chats?chat=customer-a');
    await page.locator('.message-row').last().waitFor();
    await checkComposer(width+'x'+height);
  }
  await page.setViewportSize({width:375,height:812});
  await page.goto(base+'/sessions/support/chats?chat=customer-a');
  await page.locator('.message-row').last().waitFor();
  await page.locator('#composerInput').focus();
  await page.setViewportSize({width:375,height:512});
  await checkComposer('keyboard');
  // Safari may shrink only visualViewport while innerHeight stays unchanged.
  await page.evaluate(()=>{Object.defineProperty(visualViewport,'height',{configurable:true,get:()=>360});visualViewport.dispatchEvent(new Event('resize'));});
  await page.waitForFunction(()=>document.getElementById('shellContent').getBoundingClientRect().bottom<=361);
  await checkComposer('visual-viewport');
  await page.evaluate(()=>{delete visualViewport.height;visualViewport.dispatchEvent(new Event('resize'));});
  await page.locator('#composerInput').fill('第一行\n第二行\n第三行');
  await checkComposer('multiline');
  await page.locator('#shellTheme').selectOption('night');
  await checkComposer('night');
  console.log('PASS mobile composer regression, reduced viewport, draft entry, landscape and desktop');
}finally{await page.close();await browser.close();}
