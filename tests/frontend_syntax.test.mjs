import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
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

test('chat management exposes engagement handlers without replacing dialogs during message refresh', () => {
  for (const name of ['loadLabels', 'saveManualLabel', 'loadSummary', 'generateSummary', 'loadFollowUps', 'createFollowUp', 'cancelFollowUp']) {
    assert.match(chatSource, new RegExp(`function ${name}\\(`));
  }
  assert.match(chatSource, /id="followUpDialog"/);
  assert.match(chatSource, /id="labelDialog"/);
  assert.match(chatSource, /id="summaryDialog"/);
  assert.match(chatSource, /messageStack.*replaceChildren/);
});
