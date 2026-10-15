// Real-browser regressions for the final independent review; synthetic localhost only.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)('playwright');
const base=process.env.PANEL_UI_TEST_URL;
if(!base?.startsWith('http://127.0.0.1:'))throw new Error('Use the isolated localhost fixture');
const browser=await chromium.connectOverCDP(process.env.PANEL_BROWSER_CDP);
const page=browser.contexts()[0].pages()[0];
const mode=process.argv[2];
const errors=[];page.on('pageerror',error=>errors.push(error.message));
const readyChat=async()=>{await page.goto(base+'/sessions/support/chats?chat=customer-a');await page.locator('.message-row').last().waitFor();await page.locator('#sendButton').waitFor({state:'visible'});};
const otherChat=()=>page.locator('.chat-item').nth(1).click();
const firstChat=()=>page.locator('.chat-item').first().click();
async function holdPost(action){
  let release,started,completed;const seen=new Promise(resolve=>started=resolve);const gate=new Promise(resolve=>release=resolve);const done=new Promise(resolve=>completed=resolve);
  const pattern='**/api/chat/sessions/support/'+action;
  await page.route(pattern,async route=>{if(route.request().method()!=='POST')return route.continue();started();await gate;await route.fulfill({json:{state:'SENT',note:'第一版备注'}});completed();});
  return {seen,release:async()=>{release();await done;await page.unroute(pattern);}};
}
const png=name=>({name,mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB/0AAAAASUVORK5CYII=','base64')});
try{
  await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({fail:'',delay_metadata:0,delay_media:0,delay_note_save:0,event_type:'heartbeat',latest:70})});
  await page.setViewportSize({width:1440,height:900});
  if(mode==='send-text'){
    await readyChat();await otherChat();await page.locator('#composerInput').fill('客户乙未发送草稿');await firstChat();await page.locator('#composerInput').fill('客户甲已发送内容');
    let held=await holdPost('send-text');await page.locator('#sendButton').click();await held.seen;await otherChat();await held.release();await page.locator('#sendButton').waitFor();await page.waitForFunction(()=>!document.querySelector('[data-view-host]:not([hidden])').shadowRoot.getElementById('sendButton').disabled);
    assert.equal(await page.locator('#composerInput').inputValue(),'客户乙未发送草稿');assert.ok((await page.locator('#takeoverPill').textContent()).includes('AI 可回复'));
    await firstChat();assert.equal(await page.locator('#composerInput').inputValue(),'');
    await page.locator('#composerInput').fill('本次发送内容');held=await holdPost('send-text');await page.locator('#sendButton').click();await held.seen;await page.locator('#composerInput').fill('发送期间继续输入的新草稿');await held.release();await page.waitForFunction(()=>!document.querySelector('[data-view-host]:not([hidden])').shadowRoot.getElementById('sendButton').disabled);
    assert.equal(await page.locator('#composerInput').inputValue(),'发送期间继续输入的新草稿');
    console.log('PASS text send response owns its customer and draft version');
  }
  if(mode==='send-image'){
    await readyChat();await page.locator('#composerInput').fill('客户甲图片说明');await page.locator('#imageInput').setInputFiles(png('customer-a.png'));await page.locator('#imagePreview').waitFor({state:'visible'});
    const held=await holdPost('send-image');await page.locator('#sendButton').click();await held.seen;await otherChat();await page.locator('#composerInput').fill('客户乙图片草稿');await page.locator('#imageInput').setInputFiles(png('customer-b.png'));await held.release();await page.waitForFunction(()=>!document.querySelector('[data-view-host]:not([hidden])').shadowRoot.getElementById('sendButton').disabled);
    assert.equal(await page.locator('#composerInput').inputValue(),'客户乙图片草稿');assert.equal(await page.locator('#imagePreview:visible').count(),1);assert.equal(await page.locator('#previewName').textContent(),'customer-b.png');
    await firstChat();assert.equal(await page.locator('#composerInput').inputValue(),'');assert.equal(await page.locator('#imagePreview:visible').count(),0);
    console.log('PASS image send response preserves another customer attachment');
  }
  if(mode==='note'){
    await readyChat();await page.locator('[data-inspector-action="noteButton"]').click();await page.locator('#noteInput').fill('第一版备注');const held=await holdPost('note');await page.locator('#saveNote').click();await held.seen;await page.locator('#noteInput').fill('后来继续输入的第二版备注');await held.release();await page.waitForFunction(()=>!document.querySelector('[data-view-host]:not([hidden])').shadowRoot.getElementById('saveNote').disabled);
    assert.equal(await page.locator('#noteDialog:visible').count(),1);assert.equal(await page.locator('#noteInput').inputValue(),'后来继续输入的第二版备注');assert.ok((await page.locator('#noteStatus').textContent()).includes('未保存'));
    await page.locator('#saveNote').click();await page.locator('#noteDialog').waitFor({state:'hidden'});
    for(const text of ['产品 Please send a quote','中文 品牌 do not contact me','型号 This is a sentence']){await page.locator('[data-inspector-action="noteButton"]').click();await page.locator('#noteInput').fill(text);await page.locator('#saveNote').click();await page.locator('#noteStatus').filter({hasText:'中文'}).waitFor();assert.equal(await page.locator('#noteDialog:visible').count(),1);await page.locator('#cancelNote').click();await page.locator('#noteDialog').waitFor({state:'hidden'});}
    console.log('PASS note save keeps newer edits; frontend rejects disguised foreign sentences');
  }
  if(mode==='logs'){
    const requests=[];let failNext=false;
    const item=(id,level='INFO')=>({id,session_name:'support',level,event:'test.event',message:'合成日志 '+id,created_at:Math.floor(Date.now()/1000)-100});
    await page.route('**/api/logs?*',async route=>{const q=new URL(route.request().url()).searchParams;requests.push(new URLSearchParams(q));if(failNext){failNext=false;return route.fulfill({status:503,json:{message:'模拟读取失败'}});}let logs=q.has('after_id')?[item(101)]:q.has('before_id')?[item(Number(q.get('before_id'))-10,q.get('level')||'INFO')]:q.get('level')==='ERROR'?[item(200,'ERROR'),item(190,'ERROR')]:[item(100),item(90)];await route.fulfill({json:{logs,next_cursor:logs.at(-1).id,has_more:true,generated_at:Math.floor(Date.now()/1000)}});});
    await page.goto(base+'/logs?session=support');await page.locator('.system-log-row').first().waitFor();const first=requests.at(-1);await page.locator('#logLevel').selectOption('ERROR');await page.locator('#logSearch').fill('尚未应用');await page.evaluate(()=>{const original=Date.now;Date.now=()=>original()+300000;});
    await page.locator('#loadOlderLogs').click();await page.locator('[data-log-id="80"]').waitFor();let q=requests.at(-1);assert.equal(q.has('level'),false,'pagination uses applied level');assert.equal(q.has('q'),false);assert.equal(q.get('from'),first.get('from'));assert.equal(q.get('to'),first.get('to'));
    failNext=true;await page.locator('#applyLogFilters').click();await page.locator('#systemLogNotice').filter({hasText:'读取失败'}).waitFor();assert.equal(await page.locator('.system-log-row').count(),3);assert.ok((await page.locator('#systemLogCount').textContent()).includes('3'));
    const before=requests.length;await page.locator('#logAutoRefresh').check();await page.waitForFunction(()=>true);for(let n=0;n<80&&requests.length===before;n++)await page.waitForTimeout(100);q=requests.at(-1);assert.equal(q.get('after_id'),'100');assert.equal(q.has('level'),false);assert.equal(q.has('q'),false);assert.equal(q.get('from'),first.get('from'));assert.ok(Number(q.get('to'))>=Number(first.get('to')));await page.locator('#logAutoRefresh').uncheck();
    await page.locator('#logSearch').fill('');await page.locator('#applyLogFilters').click();await page.locator('[data-log-id="200"]').waitFor();assert.equal(await page.locator('[data-log-level="INFO"]').count(),0);assert.ok(page.url().includes('level=ERROR'));
    console.log('PASS log cursor snapshot, unapplied filters, failed apply and live catchup remain consistent');
  }
  if(mode==='overview'){
    await page.route('**/api/overview?*',async route=>{const response=await route.fetch();const data=await response.json();data.recent_error_count=null;data.recent_errors=[];data.partial_errors.push({component:'logs',message:'异常日志读取失败'});await route.fulfill({json:data});});
    await page.goto(base+'/overview?scope=all');await page.locator('[data-overview-count="total"]').filter({hasText:'2'}).waitFor();assert.ok((await page.locator('#overviewErrors').textContent()).includes('暂不可用'));assert.ok(!(await page.locator('#overviewErrors').textContent()).includes('没有记录'));
    console.log('PASS failed anomaly module never claims no warnings or errors');
  }
  assert.deepEqual(errors,[]);
}finally{await page.unrouteAll({behavior:'ignoreErrors'});await browser.close();}
