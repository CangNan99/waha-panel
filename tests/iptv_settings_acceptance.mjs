import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)('playwright');
const base=process.env.PANEL_UI_TEST_URL,cdp=process.env.PANEL_BROWSER_CDP;
if(!base?.startsWith('http://127.0.0.1:')||!cdp)throw new Error('Local fixture and agent-browser CDP required');
const browser=await chromium.connectOverCDP(cdp),page=await browser.contexts()[0].newPage();
page.setDefaultTimeout(4000);
let saved=null;
const publicConfig={enabled:false,pack:'all',daily_limit:20,duration_confirmed:false,api_key_configured:false,playback_server:'http://existing.example'};
const errors=[];page.on('pageerror',error=>errors.push(error.message));
try{
  await page.route('**/api/settings?*',async route=>{
    if(route.request().method()==='POST'){
      saved=route.request().postDataJSON();Object.assign(publicConfig,saved.iptv,{api_key_configured:true});delete publicConfig.api_key;delete publicConfig.clear_api_key;
      return route.fulfill({json:{ai_api_key_configured:true,iptv:publicConfig}});
    }
    const response=await route.fetch();const data=await response.json();data.iptv=publicConfig;return route.fulfill({json:data});
  });
  await page.route('**/api/iptv/trials?*',route=>route.fulfill({json:{items:[{id:'opaque-test-ref',state:'unknown',error_code:'upstream_unknown',created_at:1800000000,expires_at:0}]}}));
  await page.route('**/api/iptv/check?*',route=>route.fulfill({json:{account_enabled:true,packages:[]}}));
  await page.setViewportSize({width:390,height:844});
  await page.goto(base+'/settings?session=support');
  await page.locator('#iptvEnabled').waitFor();
  assert.equal(await page.getByLabel('播放服务器地址',{exact:true}).inputValue(),'http://existing.example');
  await page.getByLabel('播放服务器地址',{exact:true}).fill('https://playback.example:8443');
  await page.locator('#iptvEnabled').check();await page.locator('#iptvDurationConfirmed').check();
  await page.locator('#iptvKey').fill('ui-fixture-reseller-key');await page.locator('#iptvDailyLimit').fill('5');
  await page.locator('#saveButton').click();
  await page.locator('#settingsNotice').filter({hasText:'设置已保存'}).waitFor();
  assert.equal(saved.iptv.enabled,true);assert.equal(saved.iptv.pack,'all');assert.equal(saved.iptv.daily_limit,5);
  assert.equal(saved.iptv.duration_confirmed,true);assert.equal(saved.iptv.api_key,'ui-fixture-reseller-key');
  assert.equal(saved.iptv.playback_server,'https://playback.example:8443');
  assert.equal(await page.getByLabel('播放服务器地址',{exact:true}).inputValue(),'https://playback.example:8443');
  assert.equal(await page.locator('#iptvKey').inputValue(),'');
  assert.ok((await page.locator('#iptvKeyState').textContent()).includes('已配置'));
  await page.locator('#checkIptv').click();
  await page.locator('#iptvNotice').filter({hasText:'账号验证成功'}).waitFor();
  await page.locator('#iptvRecords').filter({hasText:'待核对'}).waitFor();
  assert.equal(errors.length,0,JSON.stringify(errors));
  console.log('PASS mobile IPTV settings, secret clearing, check and safe records');
}finally{await page.close();await browser.close();}
