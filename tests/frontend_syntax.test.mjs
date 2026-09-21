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
  assert.match(chatSource, /恢复 AI 回复/);
  assert.doesNotMatch(chatSource, /takeover-actions \.state-pill\{display:none/);
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
