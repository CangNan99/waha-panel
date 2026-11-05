import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../panel/chat_page.py', import.meta.url), 'utf8');

test('chat page exposes the five manual IPTV card durations', () => {
  assert.match(source, /id="manualIptvSubscription"/);
  for (const label of ['测试', '1个月', '3个月', '6个月', '12个月']) {
    assert.match(source, new RegExp(label));
  }
  assert.match(source, /iptv\/manual-card/);
});

test('loading more chats survives a refresh at the current loaded window', async () => {
  assert.match(source, /chatLoadedLimit/);
  const start = source.indexOf('async function loadChats(options={}){');
  const end = source.indexOf('function setActiveChat');
  assert.ok(start >= 0 && end > start);
  const code = source.slice(start, end);
  const state = {
    chats: [], selected: null, chatOffset: 0, chatHasMore: false,
    chatLoading: false, chatReloadQueued: false, chatLoadedLimit: 30,
    unreadOnly: false,
  };
  const elements = new Map([
    ['chatSearch', {value: ''}], ['chatCount', {textContent: ''}], ['navChatCount', {textContent: ''}],
  ]);
  const requests = [];
  const sandbox = {
    state,
    URLSearchParams,
    Map,
    Boolean,
    Number,
    Array,
    location: {search: ''},
    $: id => elements.get(id) || {value: '', textContent: ''},
    apiBase: '/mock',
    renderChats() {},
    renderConversationIdentity() {},
    renderListError() {},
    toast() {},
    selectChat: async () => {},
    request: async url => {
      const query = new URLSearchParams(url.split('?')[1]);
      const limit = Number(query.get('limit'));
      const offset = Number(query.get('offset'));
      requests.push({limit, offset});
      const items = Array.from({length: Math.min(limit, 95 - offset)}, (_, index) => ({
        chat_ref: 'chat-' + (offset + index),
      }));
      return {items, next_offset: offset + items.length, has_more: offset + items.length < 95};
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  await sandbox.loadChats();
  await sandbox.loadChats({append: true});
  await sandbox.loadChats({preserveLoaded: true});
  assert.deepEqual(requests, [
    {limit: 30, offset: 0},
    {limit: 30, offset: 30},
    {limit: 60, offset: 0},
  ]);
  assert.equal(state.chats.length, 60);
});

test('refreshing after more than one hundred chats preserves the loaded window in pages', async () => {
  const start = source.indexOf('async function loadChats(options={}){');
  const end = source.indexOf('function setActiveChat');
  const code = source.slice(start, end);
  const state = {
    chats: [], selected: null, chatOffset: 0, chatHasMore: false,
    chatLoading: false, chatReloadQueued: false, chatLoadedLimit: 30,
    unreadOnly: false,
  };
  const elements = new Map([
    ['chatSearch', {value: ''}], ['chatCount', {textContent: ''}], ['navChatCount', {textContent: ''}],
  ]);
  const requests = [];
  const sandbox = {
    state,
    URLSearchParams,
    Map,
    Boolean,
    Number,
    Array,
    location: {search: ''},
    $: id => elements.get(id) || {value: '', textContent: ''},
    apiBase: '/mock',
    renderChats() {},
    renderConversationIdentity() {},
    renderListError() {},
    toast() {},
    selectChat: async () => {},
    request: async url => {
      const query = new URLSearchParams(url.split('?')[1]);
      const limit = Number(query.get('limit'));
      const offset = Number(query.get('offset'));
      requests.push({limit, offset});
      const items = Array.from({length: Math.min(limit, 200 - offset)}, (_, index) => ({
        chat_ref: 'chat-' + (offset + index),
      }));
      return {items, next_offset: offset + items.length, has_more: offset + items.length < 200};
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  await sandbox.loadChats();
  for (let page = 0; page < 4; page += 1) await sandbox.loadChats({append: true});
  await sandbox.loadChats({preserveLoaded: true});
  assert.deepEqual(requests, [
    {limit: 30, offset: 0},
    {limit: 30, offset: 30},
    {limit: 30, offset: 60},
    {limit: 30, offset: 90},
    {limit: 30, offset: 120},
    {limit: 100, offset: 0},
    {limit: 50, offset: 100},
  ]);
  assert.equal(state.chatLoadedLimit, 150);
  assert.equal(state.chats.length, 150);
});
