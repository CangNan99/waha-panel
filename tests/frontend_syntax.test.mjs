import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import { checkInlineScripts } from '../scripts/check_frontend_syntax.mjs';

const appSource = await readFile(new URL('../panel/app.py', import.meta.url), 'utf8');
const chatSource = await readFile(new URL('../panel/chat_page.py', import.meta.url), 'utf8');
const scripts = [...appSource.matchAll(/<script>\s*([\s\S]*?)\s*<\/script>/g)];

test('every inline script in panel/app.py has valid JavaScript syntax', () => {
  assert.doesNotThrow(() => checkInlineScripts(appSource), 'all inline scripts should parse');
  assert.ok(scripts.length >= 3, 'expected the panel templates to contain inline scripts');
});

test('administrator save handlers close their forEach and addEventListener calls', () => {
  assert.ok(
    appSource.includes("saveAdmin(button.closest('.admin-row'))));"),
    'administrator save handler should close closest, saveAdmin, addEventListener, and forEach',
  );
});

test('chat management inline JavaScript has valid syntax', () => {
  assert.doesNotThrow(() => checkInlineScripts(chatSource, 'panel/chat_page.py'));
});

test('stale QR requests cannot update a newly selected session', async () => {
  const source = scripts.find(([, body]) => body.includes('async function selectSession(name)') && body.includes('async function loadQr()'))?.[1];
  assert.ok(source, 'expected the multi-session inline script');
  const declarations = source
    .replace(/^\s*<!-- SPONSOR_SCRIPT -->\s*$/m, '')
    .slice(0, source.indexOf("$('themeSelect').addEventListener"));
  const qrWrap = {
    _innerHTML: '',
    dataset: {},
    setAttribute(name, value) { this[name] = value; },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(value) { this._innerHTML = value; delete this.child; },
    replaceChildren(...children) { this.child = children[0]; this._innerHTML = ''; },
  };
  const qrImageLayer = { child: undefined, replaceChildren(...children) { this.child = children[0]; } };
  const qrFrosted = { dataset: {}, setAttribute() {}, textContent: '' };
  const pairingCode = { dataset: {}, hidden: true, setAttribute() {}, replaceChildren() {} };
  const elements = new Map([
    ['globalNotice', { textContent: '' }],
    ['logs', { innerHTML: '' }],
    ['qrButton', { disabled: false }],
    ['pairingButton', { disabled: false, setAttribute() {} }],
    ['qrWrap', qrWrap],
    ['qrImageLayer', qrImageLayer],
    ['qrFrosted', qrFrosted],
    ['qrTitle', { textContent: '' }],
    ['qrDetail', { textContent: '' }],
    ['pairingCode', pairingCode],
  ]);
  const deferred = () => {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
  };
  const first = deferred();
  const second = deferred();
  const staleError = deferred();
  const current = deferred();
  const qrResponses = {
    default: [first.promise, staleError.promise],
    sales: [second.promise, current.promise],
  };
  const fetch = (url) => {
    if (url.startsWith('/api/status?')) return new Promise(() => {});
    if (url.includes('/default/qr?')) return qrResponses.default.shift();
    if (url.includes('/sales/qr?')) return qrResponses.sales.shift();
    throw new Error(`unexpected fetch ${url}`);
  };
  class TestURL extends URL {
    static revoked = [];
    static createObjectURL(blob) { return `blob:${blob.id}`; }
    static revokeObjectURL(url) { TestURL.revoked.push(url); }
  }
  const context = vm.createContext({
    console,
    document: {
      createElement: (tag) => tag === 'img' ? { src: '', alt: '', decode: async () => {} } : {},
      getElementById: (id) => elements.get(id) || { addEventListener() {} },
      querySelectorAll: () => [],
    },
    encodeURIComponent,
    fetch,
    Headers,
    history: { replaceState() {} },
    localStorage: { getItem: () => null, setItem() {} },
    location: { href: 'http://panel.local/?session=default', search: '?session=default' },
    URL: TestURL,
    URLSearchParams,
    requestAnimationFrame: (callback) => callback(),
  });
  vm.runInContext(`${declarations}\nglobalThis.qrTest = { loadQr, selectSession, setSelectedSession };`, context);

  const firstLoad = context.qrTest.loadQr();
  void context.qrTest.selectSession('sales');
  const secondLoad = context.qrTest.loadQr();
  first.resolve(imageResponse('default'));
  await firstLoad;

  assert.equal(elements.get('qrImageLayer').child, undefined, 'session A must not replace session B loading state');
  assert.ok(TestURL.revoked.includes('blob:default'), 'stale QR object URL must be released');
  assert.equal(elements.get('qrButton').disabled, true, 'session A must not release session B button ownership');

  second.resolve(imageResponse('sales'));
  await secondLoad;
  assert.equal(elements.get('qrImageLayer').child.src, 'blob:sales');
  assert.equal(elements.get('qrButton').disabled, false);

  context.qrTest.setSelectedSession('default');
  const staleErrorLoad = context.qrTest.loadQr();
  context.qrTest.setSelectedSession('sales');
  const currentLoad = context.qrTest.loadQr();
  staleError.resolve({
    ok: false,
    headers: { get: () => 'application/json' },
    json: async () => ({ message: 'stale default failure' }),
  });
  await staleErrorLoad;
  assert.doesNotMatch(qrWrap.innerHTML, /二维码获取失败|stale default failure/);
  assert.equal(elements.get('qrButton').disabled, true, 'stale error must not release current button ownership');

  current.resolve(imageResponse('sales-current'));
  await currentLoad;
  assert.equal(elements.get('qrImageLayer').child.src, 'blob:sales-current');
  assert.ok(TestURL.revoked.includes('blob:sales'), 'replaced QR object URL must be released');
  assert.equal(elements.get('qrButton').disabled, false);

  assert.match(declarations, /if \(!names\.includes\(selected\)\) setSelectedSession\(/);
  assert.match(declarations, /setSelectedSession\(remaining\[0\]\?\.name \|\| 'default'\)/);
  assert.match(declarations, /setSelectedSession\(data\.name \|\| data\.session_name/);
  assert.equal((declarations.match(/\bselected\s*=(?!=)/g) || []).length, 2, 'only initialization and setSelectedSession may assign selected');

  function imageResponse(id) {
    return {
      ok: true,
      headers: { get: () => 'image/png' },
      blob: async () => ({ id }),
    };
  }
});

test('chat management exposes engagement handlers without replacing dialogs during message refresh', () => {
  for (const name of ['loadLabels', 'saveManualLabel', 'loadSummary', 'generateSummary', 'loadFollowUps', 'createFollowUp', 'cancelFollowUp']) {
    assert.match(chatSource, new RegExp(`function ${name}\\(`));
  }
  assert.match(chatSource, /id="followUpDialog"/);
  assert.match(chatSource, /id="labelDialog"/);
  assert.match(chatSource, /id="summaryDialog"/);
  assert.match(chatSource, /messageStack.*replaceChildren/);
});

test('chat message refresh reuses keyed rows and preserves active scrolling', () => {
  assert.match(chatSource, /function syncMessageRows\(/);
  assert.match(chatSource, /messageNodeByRef/);
  assert.match(chatSource, /messageScrollGeneration/);
  assert.match(chatSource, /const scrollGeneration=messageScrollGeneration/);
  assert.match(chatSource, /const scrollChanged=scrollGeneration!==messageScrollGeneration/);
  assert.match(chatSource, /else if\(open&&!scrollChanged\)scrollToBottom\(\)/);
  assert.doesNotMatch(chatSource, /area\.scrollTop=Math\.min\(previousScrollTop/);
});

test('chat management guards engagement responses across chat switches', () => {
  assert.match(chatSource, /requestedChatRef/);
  assert.match(chatSource, /requestGeneration/);
  assert.match(chatSource, /data-task-id/);
  assert.match(chatSource, /updated_at/);
  assert.doesNotMatch(chatSource, /Math\.floor\(Date\.now\(\)\/1000\)/);
});

test('chat engagement cleanup releases only the request-owned pending control', () => {
  assert.match(chatSource, /pendingActions/);
  assert.match(chatSource, /engagementRequestId/);
  assert.match(chatSource, /pendingActions\.label===requestToken/);
  assert.match(chatSource, /pendingActions\.followUpCreate===requestToken/);
  assert.match(chatSource, /pendingActions\.summary===requestToken/);
  assert.match(chatSource, /resetEngagementPending/);
});

test('chat list renders note first and caps labels at four', () => {
  assert.match(chatSource, /item\.note \|\| item\.name \|\| item\.display_id/);
  assert.match(chatSource, /slice\(0, 4\)/);
  assert.match(chatSource, /label_overflow/);
  assert.match(chatSource, /conversationMoreDialog/);
});

test('mobile primary takeover actions remain visible', () => {
  assert.match(chatSource, /人工接管/);
  assert.match(chatSource, /人工接管：开启/);
  assert.doesNotMatch(chatSource, /takeover-actions \.state-pill\{display:none/);
});

test('mobile conversation hides only the global header and keeps the recent-chat view intact', () => {
  assert.match(chatSource, /\.app\.has-chat > \.topbar\{display:none/);
  assert.match(chatSource, /\.app\.has-chat\{grid-template-rows:minmax\(0,1fr\)/);
  assert.match(chatSource, /\.app:not\(\.has-chat\) \.chat-pane\.mobile-view-layer/);
  assert.match(chatSource, /function setMobileView\(view\)\s*\{[\s\S]*?app\.classList/);
});

test('chat avatars are reused across metadata refreshes instead of being rebuilt', () => {
  assert.match(chatSource, /node\.dataset\.avatarUrl/);
  assert.match(chatSource, /sameUrl&&sameFallback/);
  assert.match(chatSource, /image\.dataset\.avatarUrl=avatarUrl/);
  assert.match(chatSource, /node\.dataset\.avatarFailed/);
  assert.match(chatSource, /button\.querySelector\('\.avatar'\)\|\|element\('div','avatar'\)/);
});

test('session control uses a protected current-account avatar proxy', () => {
  assert.match(appSource, /id="identityAvatar"/);
  assert.match(appSource, /function renderIdentityAvatar\(item\)/);
  assert.match(appSource, /avatar_url/);
  assert.match(appSource, /def session_avatar\(/);
  assert.match(appSource, /send_media\(\*self\.state\.session_avatar/);
});

test('translation cards identify the message speaker and send role to the API', () => {
  assert.match(chatSource, /message\.from_me\?'agent':'customer'/);
  assert.match(chatSource, /消息来源/);
  assert.match(chatSource, /speaker_intent_zh/);
  assert.match(chatSource, /客服表达目的/);
});

test('chat motion uses restrained transitions with a reduced-motion fallback', () => {
  assert.match(chatSource, /dialog\[data-motion="opening"\][^\n]*animation:dialog-in 200ms/);
  assert.match(chatSource, /dialog\[data-motion="closing"\][^\n]*animation:dialog-out 200ms/);
  assert.match(chatSource, /toast\.closing[^\n]*200ms/);
  assert.match(chatSource, /mobile-view-layer[^\n]*180ms/);
  assert.match(chatSource, /prefers-reduced-motion:reduce/);
  assert.match(chatSource, /function closeWithMotion\(/);
  assert.match(chatSource, /restoreTarget\?\.focus\(\)/);
});

test('settings page exposes bounded context slider and four media switches', () => {
  assert.match(appSource, /id="contextPerSide"[^>]*min="5"[^>]*max="50"/);
  for (const name of ['image', 'video', 'audio', 'file']) {
    assert.match(appSource, new RegExp('autoReplyMedia_' + name));
  }
  assert.match(appSource, /auto_reply_context_per_side/);
  assert.match(appSource, /auto_reply_media_types/);
});

test('visual theme contract keeps only daylight and night', () => {
  assert.match(appSource, /THEMES = \("daylight", "night"\)/);
  for (const source of [appSource, chatSource]) {
    assert.doesNotMatch(source, /<option value="paper">|data-theme="paper"|--paper\b|var\(--paper\)/);
    assert.match(source, /<option value="daylight">白天<\/option>/);
    assert.match(source, /<option value="night">夜间<\/option>/);
  }
  assert.match(appSource, /const themeNames = \{daylight:'白天', ?night:'夜间'\}/);
  assert.match(chatSource, /function applyTheme\(value\)\{const theme=\['daylight','night'\]\.includes\(value\)\?value:'daylight'/);
});

test('visual redesign keeps Bento overrides isolated to the page templates', () => {
  assert.match(appSource, /Bento visual system: multi-session dashboard/);
  assert.match(appSource, /Bento visual system: automation settings/);
  assert.match(chatSource, /Bento visual system: chat workspace/);
});

test('QR reveal and pairing states are explicit and resource-safe', () => {
  assert.match(appSource, /setQrState\('loading'/);
  assert.match(appSource, /setQrState\('ready'/);
  assert.match(appSource, /setQrState\('error'/);
  assert.match(appSource, /await image\.decode\(\)/);
  assert.match(appSource, /URL\.revokeObjectURL/);
  assert.match(appSource, /ownsRequest\(\)/);
  assert.match(appSource, /420ms ease-out/);
  assert.match(appSource, /legacyQrImageLayer/);
  assert.match(appSource, /requestPairingCode/);
  assert.match(appSource, /setPairingState\(['"]loading/);
  assert.match(appSource, /setPairingState\(['"]ready/);
  assert.match(appSource, /setPairingState\(['"]error/);
  assert.match(appSource, /aria-busy/);
});

test('connected sessions prevent new QR requests and discard a pending QR response', async () => {
  const source = scripts.find(([, body]) => body.includes('async function selectSession(name)'))?.[1];
  const declarations = source.replace(/^\s*<!-- SPONSOR_SCRIPT -->\s*$/m, '')
    .slice(0, source.indexOf("$('themeSelect').addEventListener"));
  const nodes = new Map();
  const getNode = id => {
    if (!nodes.has(id)) nodes.set(id, {
      dataset: {}, disabled: false, textContent: '', children: [], setAttribute() {},
      replaceChildren(...children) { this.children = children; },
    });
    return nodes.get(id);
  };
  let requests = 0;
  let pendingResponse;
  const releasedUrls = [];
  class TestURL extends URL {
    static createObjectURL() { return 'blob:pending-demo-qr'; }
    static revokeObjectURL(url) { releasedUrls.push(url); }
  }
  const context = vm.createContext({
    document: { getElementById: getNode, createElement: () => ({ decode: async () => {} }) },
    location: { search: '?session=default' },
    URL: TestURL, URLSearchParams,
    requestAnimationFrame: callback => callback(),
    fetch() {
      requests++;
      if (pendingResponse) return pendingResponse;
      throw new Error('connected session must not fetch QR');
    },
  });
  vm.runInContext(`${declarations}\nglobalThis.testConnection = {
    setStatus(state) { statusData = { sessions: [{ name: 'default', state }] }; },
    loadQr, syncConnectionPresentation
  };`, context);
  for (const state of ['WORKING', 'CONNECTED']) {
    context.testConnection.setStatus(state);
    await context.testConnection.loadQr();
    assert.equal(requests, 0);
    assert.equal(getNode('qrButton').disabled, true);
    assert.equal(getNode('qrWrap').dataset.state, 'connected');
    assert.match(getNode('qrTitle').textContent, /已连接/);
  }
  context.testConnection.setStatus('SCAN_QR_CODE');
  context.testConnection.syncConnectionPresentation();
  assert.equal(getNode('qrButton').disabled, false);
  assert.equal(getNode('qrWrap').dataset.state, 'idle');

  let resolveResponse;
  pendingResponse = new Promise(resolve => { resolveResponse = resolve; });
  const inFlight = context.testConnection.loadQr();
  assert.equal(getNode('qrWrap').dataset.state, 'loading');
  context.testConnection.setStatus('WORKING');
  context.testConnection.syncConnectionPresentation();
  resolveResponse({ ok: true, headers: { get: () => 'image/png' }, blob: async () => ({}) });
  await inFlight;
  assert.equal(requests, 1);
  assert.equal(getNode('qrWrap').dataset.state, 'connected');
  assert.equal(getNode('qrButton').disabled, true);
  assert.equal(getNode('qrImageLayer').children.length, 0);
  assert.deepEqual(releasedUrls, ['blob:pending-demo-qr']);
});

test('customer inspector updates both layouts safely when selection and reply mode change', () => {
  const helper = chatSource.match(/  function renderCustomerDetails\(item\)\{[\s\S]*?\n  \}/)?.[0];
  assert.ok(helper, 'expected a customer inspector renderer');
  const fields = ['name', 'identifier', 'kind', 'reply', 'translation', 'note'];
  const nodes = fields.flatMap(field => [0, 1].map(() => ({
    dataset: { customerField: field }, textContent: '',
    set innerHTML(_value) { throw new Error('customer content must remain plain text'); },
  })));
  const controls = new Map(['noteButton', 'labelButton', 'summaryButton', 'followUpButton', 'inspectorAvatar', 'customerInspector']
    .map(id => [id, { disabled: false, dataset: {} }]));
  const state = { connected: true };
  const profileActions = [
    { disabled: false, dataset: { inspectorAction: 'noteButton' } },
    { disabled: false, dataset: { inspectorAction: 'replyMode' } },
    { disabled: false, dataset: { inspectorAction: 'openAssistantSettings' } },
  ];
  const context = vm.createContext({
    state,
    $: id => controls.get(id),
    displayName: item => item?.note || item?.name || item?.display_id || '未知客户',
    translationIsEnabled: () => false,
    setAvatar() {},
    document: { querySelectorAll: selector => selector === '[data-customer-field]' ? nodes : selector === '[data-inspector-action]' ? profileActions : [] },
  });
  vm.runInContext(`${helper}\nglobalThis.renderDetails = renderCustomerDetails;`, context);
  const item = { name: '客户 A', display_id: '演示号码 A', note: '<img src=x onerror=alert(1)>', takeover_state: 'HUMAN_TAKEOVER' };
  context.renderDetails(item);
  const values = field => nodes.filter(node => node.dataset.customerField === field).map(node => node.textContent);
  assert.deepEqual(values('note'), [item.note, item.note]);
  assert.deepEqual(values('reply'), ['人工接管中', '人工接管中']);
  context.renderDetails({ name: '客户 B', display_id: '演示号码 B', is_group: true, takeover_state: 'AI_ELIGIBLE' });
  assert.deepEqual(values('identifier'), ['演示号码 B', '演示号码 B']);
  assert.deepEqual(values('note'), ['暂无备注', '暂无备注']);
  assert.deepEqual(values('kind'), ['群聊', '群聊']);
  state.connected = false;
  context.renderDetails({ name: '客户 B' });
  assert.deepEqual(values('reply'), ['会话未连接', '会话未连接']);
  context.renderDetails(null);
  assert.equal(controls.get('noteButton').disabled, true);
  assert.ok(profileActions.slice(0, 2).every(action => action.disabled), 'profile shortcuts must be disabled without a customer');
  assert.equal(profileActions[2].disabled, false, 'global AI settings must remain available without a customer');
  context.renderDetails({ name: '客户 C' });
  assert.ok(profileActions.every(action => !action.disabled), 'profile shortcuts must follow the selected customer');
});

test('overview metrics use session data and navigation follows the selected session', () => {
  const helper = appSource.match(/    function renderOverviewMetrics\(\) \{[\s\S]*?\n    \}/)?.[0];
  assert.ok(helper, 'expected a real-data overview renderer');
  const nodes = new Map();
  const getNode = id => {
    if (!nodes.has(id)) nodes.set(id, { textContent: '', href: '', attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } });
    return nodes.get(id);
  };
  let current = { name: '销售 inbox', updated_at: 1234 };
  const context = vm.createContext({
    statusData: { sessions: [{ state: 'WORKING' }, { state: 'CONNECTED' }, { state: 'STOPPED' }] },
    selected: 'default', $: getNode, currentItem: () => current,
    formatTime: value => value ? 'time:' + value : '—', encodeURIComponent,
  });
  vm.runInContext(helper + '\nglobalThis.renderMetrics = renderOverviewMetrics;', context);
  context.renderMetrics();
  assert.equal(getNode('sessionTotalMetric').textContent, '3');
  assert.equal(getNode('connectedTotalMetric').textContent, '2');
  assert.equal(getNode('overviewUpdatedMetric').textContent, 'time:1234');
  assert.equal(getNode('chatLink').href, '/sessions/' + encodeURIComponent(current.name) + '/chats');
  assert.equal(getNode('settingsLink').href, '/settings?session=' + encodeURIComponent(current.name));
  current = { name: 'sales', updated_at: 5678 };
  context.renderMetrics();
  assert.equal(getNode('chatLink').href, '/sessions/sales/chats');
  current = null;
  context.statusData = { sessions: [] };
  context.renderMetrics();
  assert.equal(getNode('sessionTotalMetric').textContent, '0');
  assert.equal(getNode('connectedTotalMetric').textContent, '0');
  assert.equal(getNode('overviewUpdatedMetric').textContent, '—');
  assert.equal(getNode('chatLink').href, '#');
  assert.equal(getNode('chatLink').attributes['aria-disabled'], 'true');
});
