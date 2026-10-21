// Uses an agent-browser managed Chromium session via its documented CDP URL.
// Run with NODE_PATH pointing at an existing Playwright installation; no install.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
const { chromium } = createRequire(import.meta.url)('playwright');
const base = process.env.PANEL_UI_TEST_URL;
const cdp = process.env.PANEL_BROWSER_CDP;
if (!base || !cdp || !base.startsWith('http://127.0.0.1:')) throw new Error('Set the isolated fixture URL and browser CDP URL.');
const browser = await chromium.connectOverCDP(cdp);
const context = browser.contexts()[0];
const page = context.pages()[0] || await context.newPage();
const mode = process.argv[2] || 'layout';
const output = process.env.PANEL_UI_EVIDENCE || '.superpowers/sdd/2026-10-15-panel-experience-improvements';
await mkdir(output, { recursive: true });
const errors = [];
page.on('pageerror', e => errors.push(e.message));
const check = async (name, run) => { await run(); console.log('PASS ' + name); };

try {
  if(mode==='edges'){
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({fail:'',delay_metadata:0,delay_media:0,latest:70,event_type:'heartbeat'})});
    await page.setViewportSize({width:1440,height:900});
    await page.route('**/api/chat/sessions/support/messages?*',route=>route.abort('failed'));
    await page.goto(base+'/sessions/support/chats?chat=customer-a');await page.locator('#messageStack').filter({hasText:'消息读取失败'}).waitFor();
    await page.unroute('**/api/chat/sessions/support/messages?*');await page.locator('#messageStack').getByRole('button',{name:'重试',exact:true}).click();await page.locator('.message-row').last().waitFor();
    await page.locator('#latestMessageButton').evaluate(button=>button.click());
    await page.locator('.message-row').last().evaluate(row=>{const translated=document.createElement('div');translated.textContent='模拟稍后显示的中文译文';translated.style.height='420px';row.append(translated);});
    await page.waitForFunction(()=>{const area=document.querySelector('[data-view-host]:not([hidden])').shadowRoot.getElementById('messageArea');return area.scrollHeight-area.scrollTop-area.clientHeight<5;});
    await page.locator('#imageInput').setInputFiles({name:'synthetic.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB/0AAAAASUVORK5CYII=','base64')});await page.locator('#imagePreview').waitFor({state:'visible'});
    await page.locator('#imageInput').setInputFiles([]);assert.equal(await page.locator('#imagePreview:visible').count(),1);await page.locator('#removeImage').click();await page.locator('#imagePreview').waitFor({state:'hidden'});
    await page.evaluate(()=>window.__edgeDocument='preserved');await page.route('**/api/ui/view?view=overview*',route=>route.abort('failed'));await page.locator('[data-shell-nav="overview"]').click();await page.locator('#shellError').waitFor({state:'visible'});assert.equal(await page.locator('#composerInput:visible').count(),1);assert.equal(await page.evaluate(()=>window.__edgeDocument),'preserved');await page.unroute('**/api/ui/view?view=overview*');await page.locator('[data-shell-nav="overview"]').click();await page.locator('#overviewScope').waitFor();
    await page.locator('[data-shell-nav="logs"]').click();await page.locator('.system-log-row').first().waitFor();await page.locator('.log-detail-button').first().click();await page.locator('#logDetailDialog').waitFor({state:'visible'});await context.grantPermissions(['clipboard-read','clipboard-write'],{origin:base});await page.locator('#copyLogDetail').click();await page.locator('#logCopyStatus').filter({hasText:'已复制'}).waitFor();const copied=await page.evaluate(()=>navigator.clipboard.readText());assert.ok(copied.includes('support')&&copied.includes('测试记录'));
    console.log('PASS failed message/navigation retry, translated-height growth, image cancellation/removal and log detail clipboard');
  }
  if(mode==='responsive'){
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({fail:'',fail_status:503,delay_metadata:0,delay_media:0,event_type:'heartbeat',latest:500})});
    for(const [width,height]of [[375,812],[768,1024],[1280,800],[1440,900],[1920,1080]]){
      await page.setViewportSize({width,height});
      for(const [view,url,ready]of [['dashboard','/?session=default','#qrButton'],['chat','/sessions/support/chats?chat=customer-a','#composerInput'],['settings','/settings?session=support','#persona'],['overview','/overview?scope=all','#overviewScope'],['logs','/logs?session=support','#logSession']]){
        await page.goto(base+url);await page.locator(ready).waitFor();await page.waitForTimeout(100);
        const geometry=await page.evaluate(()=>{const host=document.querySelector('[data-view-host]:not([hidden])');const body=host.shadowRoot.querySelector('.view-body');return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,host:host.clientWidth,body:body.scrollWidth};});
        assert.ok(geometry.documentWidth<=geometry.viewport+1&&geometry.body<=geometry.host+1,view+' '+width+': '+JSON.stringify(geometry));
        assert.equal(await page.locator('[data-shell-nav]').count(),5);
        if(width===375||width===1440)await page.screenshot({path:output+'/'+view+'-'+width+'-final.png'});
        console.log('PASS responsive '+view+' '+width);
      }
    }
    await page.setViewportSize({width:1440,height:900});await page.goto(base+'/sessions/support/chats?chat=customer-a');await page.locator('.message-row').first().waitFor();
    for(let n=0;n<10;n++){const loader=page.locator('.history-loader');if(!await loader.count())break;const before=await page.locator('.message-row').count();await loader.evaluate(button=>button.click());await page.waitForFunction(count=>{const root=document.querySelector('[data-view-host]:not([hidden])').shadowRoot;return root.querySelectorAll('.message-row').length>count;},before);}
    assert.equal(await page.locator('.message-row').count(),500);
    for(const theme of ['daylight','night']){await page.locator('#shellTheme').selectOption(theme);const stable=await page.locator('#messageArea').evaluate(area=>{const root=area.getRootNode(),wall=root.querySelector('.chat-wallpaper');area.scrollTop=0;const top=wall.getBoundingClientRect().top;area.scrollTop=area.scrollHeight;return Math.abs(top-wall.getBoundingClientRect().top)<1;});assert.ok(stable);await page.screenshot({path:output+'/chat-500-'+theme+'.png'});}
    for(const zoom of [1.25,1.5]){await page.setViewportSize({width:Math.round(1280/zoom),height:Math.round(800/zoom)});await page.goto(base+'/?session=default');await page.locator('#qrButton').click();await page.locator('#qrImageLayer img').waitFor();const fits=await page.locator('#qrImageLayer img').evaluate(image=>{const frame=image.closest('.qr-card').getBoundingClientRect(),r=image.getBoundingClientRect();return r.bottom<frame.bottom&&r.right<frame.right;});assert.ok(fits,'zoom layout '+zoom);}
    console.log('PASS 500-message wallpaper and 125/150 percent effective viewport layout');
  }
  if(mode==='errors'){
    await page.goto(base+'/overview?scope=all');await page.locator('[data-overview-count="total"]').filter({hasText:'2'}).waitFor();
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({fail:'/api/overview',fail_status:503})});
    await page.locator('#refreshOverview').click();await page.locator('#overviewNotice').filter({hasText:'数据已过期'}).waitFor();assert.equal(await page.locator('[data-overview-count="total"]').textContent(),'2');
    await page.reload();await page.locator('#overviewNotice').filter({hasText:'暂不可用'}).waitFor();assert.equal(await page.locator('[data-overview-count="total"]').textContent(),'暂不可用');
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({fail:''})});await page.locator('#refreshOverview').click();await page.locator('[data-overview-count="total"]').filter({hasText:'2'}).waitFor();
    await page.locator('[data-shell-nav="logs"]').click();await page.locator('.system-log-row').first().waitFor();
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({fail:'/api/logs',fail_status:401})});await page.locator('#refreshSystemLogs').click();await page.locator('#shellError').filter({hasText:'登录状态已失效'}).waitFor();assert.equal(await page.locator('[data-view-host]').count(),0);
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({fail:'',fail_status:503})});console.log('PASS stale/unknown data, retry and authentication cleanup');
  }
  if(mode==='lifecycle'){
    await page.goto(base+'/overview?scope=all');await page.locator('[data-overview-count="total"]').filter({hasText:'2'}).waitFor();
    const count=async path=>(await (await fetch(base+'/__fixture__/metrics')).json()).requests[path]||0;
    const overview=await count('/api/overview');await page.locator('[data-shell-nav="logs"]').click();await page.locator('.system-log-row').first().waitFor();await page.waitForTimeout(5300);assert.equal(await count('/api/overview'),overview);
    await page.locator('#logAutoRefresh').check();const logs=await count('/api/logs');await page.waitForTimeout(5300);assert.equal(await count('/api/logs'),logs+1);
    await page.locator('[data-shell-nav="dashboard"]').click();await page.locator('#qrButton').waitFor();const paused=await count('/api/logs');await page.waitForTimeout(5300);assert.equal(await count('/api/logs'),paused);
    console.log('PASS inactive overview/log timers pause and log timer remains single');
  }
  if(mode==='chinese'){
    await page.setViewportSize({width:1440,height:900});
    await page.goto(base+'/sessions/support/chats?chat=customer-a');
    await page.locator('#composerInput').waitFor();
    assert.equal(await page.locator('#shellTranslationSettings:visible').count(),1,'machine translation settings remain accessible');
    await page.locator('#shellTranslationSettings').click();await page.locator('#aliyunTranslationDialog').waitFor({state:'visible'});await page.locator('#closeAliyunTranslationDialog').click();await page.locator('#aliyunTranslationDialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('#normalizeNote').count(),1,'Chinese note preview control');
    await page.locator('[data-inspector-action="noteButton"]').click();
    await page.locator('#noteInput').fill('Customer wants iPhone 16, budget 800 USD');
    await page.locator('#saveNote').click();
    await page.locator('#noteStatus').filter({hasText:'中文'}).waitFor();
    await page.locator('#normalizeNote').click();
    await page.locator('#notePreview').waitFor({state:'visible'});
    assert.equal(await page.locator('#noteInput').inputValue(),'Customer wants iPhone 16, budget 800 USD');
    await page.locator('#useNotePreview').click();
    assert.ok((await page.locator('#noteInput').inputValue()).includes('客户'));
    await page.locator('#saveNote').click();
    await page.locator('#noteDialog').waitFor({state:'hidden'});
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({delay_note_save:1})});
    await page.locator('[data-inspector-action="noteButton"]').click();
    await page.locator('#noteInput').fill('客户甲新的中文备注');
    await page.locator('#saveNote').click();
    await page.keyboard.press('Escape');
    await page.locator('#noteDialog').waitFor({state:'hidden'});
    await page.locator('.chat-item').nth(1).click();
    await page.waitForTimeout(1200);
    assert.equal(await page.locator('#conversationName').textContent(),'客户乙','old note response cannot rename a different customer');
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({delay_note_save:0})});
    console.log('PASS Chinese note rejection, preview, confirmation and stale-response isolation');
  }
  if(mode==='system_labels'){
    await page.setViewportSize({width:1440,height:900});
    await page.route('**/api/chat/sessions/support/labels*', route => route.fulfill({
      status:200,
      contentType:'application/json',
      body:JSON.stringify({manual:[],ai:[],system:[{id:17,label:'IPTV测试过'}]})
    }));
    await page.goto(base+'/sessions/support/chats?chat=customer-a');
    await page.locator('.message-row').last().waitFor();
    await page.locator('#conversationLabels .chat-label.system').filter({hasText:'IPTV测试过'}).waitFor();
    await page.locator('#labelButton').click();
    await page.locator('#labelDialog').waitFor({state:'visible'});
    await page.locator('#systemLabels .label-row.system').filter({hasText:'IPTV测试过'}).waitFor();
    assert.equal(await page.locator('#systemLabels .label-row.system button').count(),0,'system labels have no edit/delete controls');
    assert.equal(await page.locator('#systemLabels').textContent().then(text=>text.includes('只读')),true,'system labels are visibly read-only');
    await page.locator('#closeLabelDialog').click();
    await page.locator('#labelDialog').waitFor({state:'hidden'});
    await page.unroute('**/api/chat/sessions/support/labels*');
    console.log('PASS IPTV system label appears in header and read-only dialog group');
  }
  if(mode==='mobile'){
    await page.setViewportSize({width:375,height:812});
    await page.goto(base+'/sessions/support/chats?chat=customer-a');
    await page.locator('.message-row').last().waitFor();
    await page.locator('#moreButton').click();await page.locator('[data-mobile-action="note"]').click();
    await page.locator('#noteInput').waitFor({state:'visible'});await page.locator('#noteInput').fill('手机端中文备注');await page.locator('#saveNote').click();await page.locator('#noteDialog').waitFor({state:'hidden'});
    await page.locator('#shellTranslationSettings').click();await page.locator('#aliyunTranslationDialog').waitFor({state:'visible'});await page.locator('#closeAliyunTranslationDialog').click();await page.locator('#aliyunTranslationDialog').waitFor({state:'hidden'});
    await page.locator('#composerInput').focus();await page.setViewportSize({width:375,height:512});await page.waitForTimeout(200);
    assert.ok(await page.locator('#messageArea').evaluate(area=>area.scrollHeight-area.scrollTop-area.clientHeight)<5,'keyboard-sized viewport keeps latest visible');
    await page.emulateMedia({reducedMotion:'reduce'});await page.locator('[data-shell-nav="overview"]').click();await page.locator('#overviewScope').waitFor();await page.locator('[data-shell-nav="chat"]').click();await page.locator('#composerInput').waitFor();await page.emulateMedia({reducedMotion:'no-preference'});
    console.log('PASS mobile more/note/settings flows, simulated keyboard viewport and reduced motion');
  }
  if(mode==='panels'){
    await page.setViewportSize({width:1440,height:900});
    await page.goto(base+'/overview?scope=all');
    await page.locator('#overviewScope').waitFor();
    assert.equal(await page.locator('#overviewScope').count(),1,'independent overview controls');
    await page.locator('[data-overview-count="total"]').filter({hasText:'2'}).waitFor();
    assert.equal(await page.locator('.session-status-row').count(),2);
    await page.locator('#overviewScope').selectOption('support');
    await page.locator('[data-overview-count="total"]').filter({hasText:'1'}).waitFor();
    assert.equal(await page.locator('.session-status-row').count(),1);
    await page.locator('[data-shell-nav="logs"]').click();
    await page.locator('.system-log-row').first().waitFor();
    await page.locator('#logLevel').selectOption('ERROR');
    await page.locator('#applyLogFilters').click();
    await page.waitForTimeout(150);
    const levels=await page.locator('.system-log-row [data-log-level]').allTextContents();
    assert.ok(levels.length>0&&levels.every(value=>value==='错误'));
    await page.locator('.log-detail-button').first().click();
    await page.locator('#logDetailDialog').waitFor({state:'visible'});
    await page.locator('#closeLogDetail').click();
    for(const view of ['overview','logs']){
      await page.locator('[data-shell-nav="'+view+'"]').click();
      await page.waitForTimeout(200);
      for(const theme of ['daylight','night']){
        await page.locator('#shellTheme').selectOption(theme);
        await page.screenshot({path:output+'/'+view+'-'+theme+'.png'});
      }
    }
    console.log('PASS independent overview scope, log filtering and detail');
  }
  if(mode==='messages'){
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({delay_metadata:2,delay_media:1.5,latest:80,event_type:'heartbeat'})});
    await page.setViewportSize({width:1440,height:900});
    await page.goto(base+'/sessions/support/chats?chat=customer-a');
    await page.locator('#composerInput').waitFor();
    await page.locator('.message-row').first().waitFor({timeout:1000});
    console.log('PASS message load is not blocked by metadata');
    await page.locator('.message-image').last().evaluate(image=>new Promise(resolve=>{if(image.complete)resolve();else{image.addEventListener('load',resolve,{once:true});image.addEventListener('error',resolve,{once:true});}}));
    await page.waitForTimeout(150);
    const gap=await page.locator('#messageArea').evaluate(area=>area.scrollHeight-area.scrollTop-area.clientHeight);
    assert.ok(gap<5,'latest message after delayed image: gap '+gap);
    console.log('PASS delayed image remains at latest');
    await page.locator('#composerInput').fill('客户甲草稿');
    await page.locator('.chat-item').nth(1).click();
    await page.locator('#composerInput').fill('客户乙草稿');
    await page.locator('.chat-item').first().click();
    assert.equal(await page.locator('#composerInput').inputValue(),'客户甲草稿');
    await page.locator('.chat-item').nth(1).click();
    assert.equal(await page.locator('#composerInput').inputValue(),'客户乙草稿');
    await page.locator('.message-row').last().waitFor();
    await page.locator('#messageArea').hover();await page.mouse.wheel(0,-650);await page.waitForTimeout(150);
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({delay_metadata:0,delay_media:0,latest:81,event_type:'message'})});
    await page.locator('#latestMessageButton').waitFor({state:'visible',timeout:5000});
    assert.ok(await page.locator('#messageArea').evaluate(area=>area.scrollHeight-area.scrollTop-area.clientHeight)>200,'live update preserves history reading');
    await page.locator('#latestMessageButton').click();
    await page.waitForTimeout(200);
    assert.ok(await page.locator('#messageArea').evaluate(area=>area.scrollHeight-area.scrollTop-area.clientHeight)<5);
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({event_type:'heartbeat'})});
    console.log('PASS draft isolation, live history reading and return to latest');
    await page.locator('#messageArea').evaluate(area=>{area.scrollTop=600;});
    await page.locator('.history-loader').evaluate(button=>button.click());
    await page.waitForFunction(()=>{const root=document.querySelector('[data-view-host]:not([hidden])').shadowRoot;return root.querySelectorAll('.message-row').length>50;});
    const historyCount=await page.locator('.message-row').count();
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({latest:82,event_type:'message'})});
    await page.waitForFunction(()=>{const root=document.querySelector('[data-view-host]:not([hidden])').shadowRoot;return [...root.querySelectorAll('.message-row')].at(-1)?.dataset.messageRef.endsWith('-82');});
    assert.ok(await page.locator('.message-row').count()>=historyCount,'loaded older history survives live refresh');
    await fetch(base+'/__fixture__/control',{method:'POST',body:JSON.stringify({event_type:'heartbeat'})});
    console.log('PASS loaded history remains after live refresh');
  }
  if (mode === 'shell') {
    await page.setViewportSize({width:1440,height:900});
    await page.goto(base+'/sessions/support/chats?chat=customer-a');
    assert.equal(await page.locator('[data-shell-nav="chat"]').count(), 1, 'persistent five-page navigation');
    await page.evaluate(() => {window.__documentMarker='same-document';});
    await page.locator('[data-shell-nav="chat"]').click();
    await page.locator('#composerInput').waitFor();
    await page.locator('.chat-item').first().click();
    await page.locator('#composerInput').fill('客户甲的未发送草稿');
    await page.locator('[data-shell-nav="settings"]').click();
    await page.locator('#persona').waitFor();
    await page.locator('#persona').fill('尚未保存的人设');
    await page.locator('#shellTheme').selectOption('night');
    for (let n=0;n<10;n++) {
      await page.locator('[data-shell-nav="dashboard"]').click();
      await page.locator('#qrButton').waitFor();
      await page.waitForTimeout(100);
      assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'night','page refresh preserves chosen theme');
      await page.locator('[data-shell-nav="chat"]').click();
      await page.locator('#composerInput').waitFor();
    }
    assert.equal(await page.locator('#composerInput').inputValue(),'客户甲的未发送草稿');
    await page.locator('[data-shell-nav="settings"]').click();
    assert.equal(await page.locator('#persona').inputValue(),'尚未保存的人设');
    assert.equal(await page.evaluate(()=>window.__documentMarker),'same-document');
    await page.goBack();
    await page.locator('#composerInput').waitFor();
    await page.goForward();
    await page.locator('#persona').waitFor();
    await page.locator('#saveButton').click();await page.locator('#settingsNotice').filter({hasText:'已保存'}).waitFor();
    assert.equal(await page.locator('#shellDirtyNotice').isVisible(),false,'saved settings clear unsaved state');
    console.log('PASS seamless navigation, draft, unsaved settings, back/forward');
  }
  if (mode === 'layout' || mode === 'wallpaper') {
    for (const [width, height] of mode === 'layout' ? [[375,812],[768,1024],[1280,800],[1440,900],[1920,1080]] : []) {
      await page.setViewportSize({width, height});
      await page.goto(base);
      await page.locator('#qrButton').click();
      await page.locator('#qrImageLayer img').waitFor();
      await page.waitForTimeout(500);
      await page.screenshot({path:output + '/dashboard-' + width + '.png',fullPage:true});
      await check(`QR fits ${width}`, async () => {
        const rects = await page.evaluate(() => {
          const root=document.querySelector('[data-view-host]:not([hidden])')?.shadowRoot||document;
          const frame = root.querySelector('#qrWrap').getBoundingClientRect();
          const image = root.querySelector('#qrImageLayer img').getBoundingClientRect();
          const card = root.querySelector('.qr-card').getBoundingClientRect();
          return {fw:frame.width, fh:frame.height, left:image.left-frame.left, top:image.top-frame.top,
                  right:frame.right-image.right, bottom:frame.bottom-image.bottom, iw:image.width, ih:image.height,
                  cardBottom:card.bottom-image.bottom};
        });
        assert.ok(rects.top >= -1 && rects.bottom >= -1 && rects.left >= -1 && rects.right >= -1, JSON.stringify(rects));
        assert.ok(Math.abs(rects.iw-rects.ih) < 2, JSON.stringify(rects));
        assert.ok(rects.cardBottom >= 16, JSON.stringify(rects));
      });
    }
    await page.setViewportSize({width:1440,height:900});
    await page.goto(base + '/sessions/default/chats?chat=customer-a');
    await page.locator('.message-row').first().waitFor();
    await page.waitForTimeout(300);
    const wallpaper = '.chat-wallpaper';
    await check('wallpaper stays over the scrolling viewport', async () => {
      const result = await page.evaluate(selector => {
        const root=document.querySelector('[data-view-host]:not([hidden])')?.shadowRoot||document;
        const area = root.querySelector('#messageArea');
        area.scrollTop = 0;
        const before = root.querySelector(selector).getBoundingClientRect().top;
        area.scrollTop = area.scrollHeight;
        const after = root.querySelector(selector).getBoundingClientRect().top;
        return {before, after};
      }, wallpaper);
      assert.ok(Math.abs(result.before-result.after) < 1, JSON.stringify(result));
    });
    for (const theme of ['daylight','night']) {
      await page.evaluate(value => document.documentElement.dataset.theme=value,theme);
      await page.screenshot({path:output + '/chat-' + theme + '.png'});
    }
  }
  assert.deepEqual(errors, [], 'runtime errors');
} finally {
  await browser.close();
}
