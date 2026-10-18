// G-203: minimal smoke checks for the shipped UI page (node built-in runner, no npm).
// Run with: node --test desktop/app/ui/tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const indexHtml = path.resolve(here, '..', 'index.html');

test('desktop/app/ui/index.html exists and is non-empty', () => {
  assert.equal(existsSync(indexHtml), true, `missing ${indexHtml}`);
  assert.ok(readFileSync(indexHtml, 'utf8').trim().length > 0, 'index.html is empty');
});

test('index.html has no inline <script> without a src attribute', () => {
  // Strip HTML comments so a commented-out example does not trip the check.
  const html = readFileSync(indexHtml, 'utf8').replace(/<!--[\s\S]*?-->/g, '');
  const tags = html.match(/<script\b[^>]*>/gi) ?? [];
  const inline = tags.filter((tag) => !/\ssrc\s*=/i.test(tag));
  assert.deepEqual(inline, [], `inline <script> tags break the CSP: ${inline.join(', ')}`);
});
