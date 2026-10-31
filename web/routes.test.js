/* routes.test.js — the URL table's self-check. No DOM, no browser:
 *     node web/routes.test.js          (exit 0 = every case holds)
 * Run by tests/test_web_routes.py when node is on the host. */
'use strict';
const assert = require('assert');
const R = require('./routes.js');
let n = 0;
const eq = (a, b, why) => { assert.deepStrictEqual(a, b, why); n++; };

// ---- the hash era → paths (every old link keeps landing) -------------------
const HASH = [
  ['#/', '/'],
  ['#', '/'],
  ['#/registry', '/registry'],
  ['#/registry?project=inky-frame', '/registry?project=inky-frame'],
  ['#/approvals', '/approvals'],
  ['#/search/usb%20c', '/search?q=usb%20c'],
  ['#/project/inky-frame', '/projects/inky-frame'],
  ['#/change/CHG-380', '/pulls/380'],
  ['#/release-set/12', '/release-sets/12'],
  ['#/snip/abc_123', '/snip/abc_123'],
  ['#/snip/abc_123/info', '/snip/abc_123/info'],
  ['#/item/CID-ASM-0001', '/CID-ASM-0001'],
  ['#/item/CID-ASM-0001/bom', '/CID-ASM-0001/bom'],
  ['#/item/CID-ASM-0001/bom?ref=v0.2.0', '/CID-ASM-0001/bom?ref=v0.2.0'],
  ['#/item/CID-ASM-0001/context', '/CID-ASM-0001/context'],
  ['#/item/CID-ASM-0001/releases', '/CID-ASM-0001/releases'],
  ['#/item/CID-ASM-0001/ref/v0.1.0', '/CID-ASM-0001/tree/v0.1.0'],
  ['#/item/CID-ASM-0001/ref/draft%2Fv0.4.1', '/CID-ASM-0001/tree/draft/v0.4.1'],
  ['#/item/CID-ASM-0001/ref/draft%2Fv0.4.1/cad/sub%20dir', '/CID-ASM-0001/tree/draft/v0.4.1/cad/sub%20dir'],
  ['#/item/CID-ASM-0001/compare/v0.1.0...v0.2.0', '/CID-ASM-0001/compare/v0.1.0...v0.2.0'],
  ['#/item/CID-ASM-0001/compare/v0.1.0...draft%2Fv0.2.0?file=cad%2Fx.scad',
   '/CID-ASM-0001/compare/v0.1.0...draft/v0.2.0?file=cad%2Fx.scad'],
  ['#/item/CID-ASM-0001/compare/v0.1.0...v0.2.0?mesh=a.stl', '/CID-ASM-0001/compare/v0.1.0...v0.2.0?mesh=a.stl'],
  ['#/item/CID-ASM-0001/proposal/p17', '/CID-ASM-0001/pull/17'],
  ['#/item/CID-ASM-0001/proposal/233', '/CID-ASM-0001/proposal/233'],
  ['#/item/CID-ASM-0001/release/44', '/CID-ASM-0001/releases/44'],
  ['#/item/CID-ASM-0001/view/v0.1.0', '/CID-ASM-0001'],
  ['#/item/CID-ASM-0001/file/99', '/CID-ASM-0001'],
  ['#/nonsense/x', '/'],
];
for (const [h, p] of HASH) eq(R.fromHash(h), p, h);
eq(R.fromHash('#section'), null, 'an in-page anchor is not a route');
eq(R.fromHash(''), null, 'no hash');

// ---- paths → views ---------------------------------------------------------
const pick = (r, ...k) => Object.fromEntries(k.map(x => [x, r[x]]));
eq(R.parse('/').view, 'dash');
eq(R.parse('/registry').view, 'registry');
eq(R.parse('/registry', '?project=inky').q.project, 'inky');
eq(R.parse('/approvals').view, 'approvals');
eq(pick(R.parse('/search', '?q=usb%20c'), 'view', 'text'), { view: 'search', text: 'usb c' });
eq(pick(R.parse('/projects/inky-frame'), 'view', 'project'), { view: 'project', project: 'inky-frame' });
eq(pick(R.parse('/pulls/380'), 'view', 'id'), { view: 'change', id: '380' });
eq(R.parse('/pulls/CHG-380').id, '380', 'the legacy label is accepted');
eq(R.parse('/pulls').view, 'approvals', 'the list of pull requests');
// the pull request's tabs (GitHub's): Conversation is the bare path
eq(pick(R.parse('/pulls/380'), 'view', 'id', 'tab'), { view: 'change', id: '380', tab: 'conversation' });
for (const t of ['files', 'checks', 'commits'])
  eq(pick(R.parse('/pulls/380/' + t), 'view', 'id', 'tab'), { view: 'change', id: '380', tab: t }, t);
eq(R.parse('/pulls/380/wat').tab, 'conversation', 'an unknown tab is the conversation');
// the old page paths are the SERVER's 301 (plm/webroutes.py _LEGACY), not pages:
// an in-page link to one is a full load
for (const p of ['/changes/CHG-380', '/changes', '/item/CID-ASM-0001/bom', '/change-requests/CHG-1'])
  eq(R.isAppPath(p), false, p);
eq(R.parse('/changes/CHG-380').view, 'notfound');
// one parser for a pull request's number, and the labels people read
for (const s of ['CHG-380', 'chg380', '#380', '380', ' 380 ', 380]) eq(R.chgId(s), '380', String(s));
eq(R.chgId('RS-3'), 'RS-3', 'not a pull request: unchanged');
eq(R.ref('CHG-380'), '#380');
eq(R.titleRef(380), 'Pull request #380');
eq(R.proposalLabel(12), 'p12');
eq(R.PULLS, '/pulls');
// the item's Pull requests tab
eq(pick(R.parse('/CID-ASM-0001/pulls'), 'view', 'number', 'tab'), { view: 'item', number: 'CID-ASM-0001', tab: 'pulls' });
eq(pick(R.parse('/CID-ASM-0001/pull/380/files'), 'view', 'n', 'tab'), { view: 'pull', n: 380, tab: 'files' });
eq(R.parse('/CID-ASM-0001/pull/380').tab, 'conversation');
eq(pick(R.parse('/release-sets/12'), 'view', 'id'), { view: 'release-set', id: '12' });
eq(pick(R.parse('/snip/tok/info'), 'view', 'token', 'info'), { view: 'snip', token: 'tok', info: true });
eq(pick(R.parse('/CID-ASM-0001'), 'view', 'number', 'tab'), { view: 'item', number: 'CID-ASM-0001', tab: 'part' });
eq(pick(R.parse('/CID-ASM-0001/', '?ref=v0.2.0'), 'tab', 'ref'), { tab: 'part', ref: 'v0.2.0' });
for (const t of ['bom', 'releases', 'drafts', 'history', 'record', 'context'])
  eq(R.parse('/CID-ASM-0001/' + t).tab, t, t);
eq(pick(R.parse('/CID-ASM-0001/bom', '?ref=draft%2Fv0.4.1'), 'tab', 'ref'), { tab: 'bom', ref: 'draft/v0.4.1' });
eq(pick(R.parse('/CID-ASM-0001/tree/draft/v0.4.1/cad'), 'view', 'kind', 'rest'),
   { view: 'tree', kind: 'tree', rest: 'draft/v0.4.1/cad' });
eq(R.parse('/CID-ASM-0001/blob/v0.1.0/cad/x.scad').kind, 'blob');
eq(pick(R.parse('/CID-ASM-0001/compare/v0.1.0...draft/v0.2.0', '?file=a%2Fb'), 'view', 'base', 'head'),
   { view: 'compare', base: 'v0.1.0', head: 'draft/v0.2.0' });
eq(R.parse('/CID-ASM-0001/compare/v0.1.0...draft/v0.2.0', '?file=a%2Fb').q.file, 'a/b');
eq(pick(R.parse('/CID-ASM-0001/pull/380'), 'view', 'n'), { view: 'pull', n: 380 });
eq(pick(R.parse('/CID-ASM-0001/pull/p17'), 'view', 'pid'), { view: 'proposal', pid: 17 });
eq(pick(R.parse('/CID-ASM-0001/proposal/p17'), 'view', 'pid'), { view: 'proposal', pid: 17 });
eq(pick(R.parse('/CID-ASM-0001/proposal/233'), 'view', 'aid'), { view: 'round', aid: 233 });
eq(pick(R.parse('/CID-ASM-0001/releases/tag/v0.2.0'), 'view', 'tag'), { view: 'release', tag: 'v0.2.0' });
eq(pick(R.parse('/CID-ASM-0001/releases/44'), 'view', 'id'), { view: 'release', id: 44 });
eq(R.parse('/LFD-PRT-PRT-0001').view, 'item', 'the legacy two-class number');
eq(R.parse('/CID-ASM-0001/wat').view, 'notfound');
eq(R.parse('/registr').view, 'notfound');
eq(R.parse('/cid-asm-0001').view, 'notfound');

// ---- builders → parse round trip ---------------------------------------------
const P = R.P;
eq(P.item('CID-ASM-0001'), '/CID-ASM-0001');
eq(P.tab('CID-ASM-0001', 'part', 'v0.2.0'), '/CID-ASM-0001?ref=v0.2.0');
eq(P.tab('CID-ASM-0001', 'bom', 'draft/v0.4.1'), '/CID-ASM-0001/bom?ref=draft%2Fv0.4.1');
eq(P.tree('CID-ASM-0001', 'draft/v0.4.1', 'cad/sub dir'), '/CID-ASM-0001/tree/draft/v0.4.1/cad/sub%20dir');
eq(P.blob('CID-ASM-0001', 'v0.1.0', 'cad/x.scad'), '/CID-ASM-0001/blob/v0.1.0/cad/x.scad');
eq(P.compare('CID-ASM-0001', 'v0.1.0', 'draft/v0.2.0', { file: 'cad/x.scad', mesh: '' }),
   '/CID-ASM-0001/compare/v0.1.0...draft/v0.2.0?file=cad%2Fx.scad');
eq(P.pull('CID-ASM-0001', 17), '/CID-ASM-0001/pull/17');
eq(P.pullP('CID-ASM-0001', 17), '/CID-ASM-0001/pull/p17');
eq(P.round('CID-ASM-0001', 233), '/CID-ASM-0001/proposal/233');
eq(P.release('CID-ASM-0001', 44, 'v0.2.0'), '/CID-ASM-0001/releases/tag/v0.2.0');
eq(P.release('CID-ASM-0001', 44), '/CID-ASM-0001/releases/44');
eq(P.change(380), '/pulls/380');
eq(P.change('CHG-380'), '/pulls/380');
eq(P.change('#380'), '/pulls/380');
eq(P.change(380, 'files'), '/pulls/380/files');
eq(P.change(380, 'conversation'), '/pulls/380');
eq(P.change(380, 'nope'), '/pulls/380');
eq(P.tab('CID-ASM-0001', 'pulls'), '/CID-ASM-0001/pulls');
eq(P.pull('CID-ASM-0001', 380, 'checks'), '/CID-ASM-0001/pull/380/checks');
eq(P.releaseSet('RS-12'), '/release-sets/12');
eq(P.search('usb c'), '/search?q=usb%20c');
eq(P.registry({ project: 'inky frame' }), '/registry?project=inky%20frame');
eq(P.project('inky-frame'), '/projects/inky-frame');
eq(P.snip('t', true), '/snip/t/info');
for (const p of [P.item('A-PRT-001'), P.tree('A-PRT-001', 'draft/v1', 'x'), P.compare('A-PRT-001', 'a', 'b'),
                 P.pull('A-PRT-001', 1), P.pull('A-PRT-001', 1, 'files'), P.change(1, 'checks'),
                 P.release('A-PRT-001', 1, 'v1'), P.change(1), P.releaseSet(1),
                 P.search('x'), P.project('x'), P.snip('x'), '/registry', '/approvals', R.PULLS, '/'])
  eq(R.isAppPath(p.split('?')[0]), true, p);
for (const p of ['/api/stats', '/auth/login', '/mcp', '/git/X-PRT-001.git', '/healthz', '/metrics',
                 '/app.js', '/vendor/three.min.js', '/favicon.svg', '/registr'])
  eq(R.isAppPath(p), false, p);

// ---- refs: longest first -------------------------------------------------------
const REFS = ['main', 'v0.1.0', 'draft/v0.4.1', 'draft/v0.4.1-b', 'feature/x/y'];
eq(R.splitRef('v0.1.0', REFS), { ref: 'v0.1.0', path: '' });
eq(R.splitRef('v0.1.0/cad/x.scad', REFS), { ref: 'v0.1.0', path: 'cad/x.scad' });
eq(R.splitRef('draft/v0.4.1/cad', REFS), { ref: 'draft/v0.4.1', path: 'cad' });
eq(R.splitRef('draft/v0.4.1-b/README.md', REFS), { ref: 'draft/v0.4.1-b', path: 'README.md' });
eq(R.splitRef('feature/x/y/z', REFS), { ref: 'feature/x/y', path: 'z' });
eq(R.splitRef('draft%2Fv0.4.1/cad/a%20b.scad', REFS), { ref: 'draft/v0.4.1', path: 'cad/a b.scad' });
eq(R.splitRef('0123abcd/cad', REFS), { ref: '0123abcd', path: 'cad' });
eq(R.splitRef('draft/v0.9.9/cad', []), { ref: 'draft/v0.9.9', path: 'cad' });
eq(R.splitRef('', REFS), { ref: '', path: '' });
// the tree round trip: every builder output splits back to what went in
for (const [ref, path] of [['draft/v0.4.1', 'cad/sub dir'], ['v0.1.0', ''], ['feature/x/y', 'a/b']]) {
  const rest = P.tree('N-PRT-001', ref, path).replace('/N-PRT-001/tree/', '');
  eq(R.splitRef(rest, REFS), { ref, path }, ref + ' ' + path);
}

// ---- titles ------------------------------------------------------------------
eq(R.title(R.parse('/CID-ASM-0001')), 'CID-ASM-0001 · plm');
eq(R.title(R.parse('/pulls/380')), 'Pull request #380 · plm');
eq(R.title(R.parse('/pulls/380/files')), 'Files changed · Pull request #380 · plm');
eq(R.title(R.parse('/pulls')), 'Pull requests · plm');
eq(R.title(R.parse('/CID-ASM-0001/pull/380/commits')), 'Commits · #380 · CID-ASM-0001 · plm');
eq(R.title(R.parse('/CID-ASM-0001'), 'GIT-FIRST POC'), 'CID-ASM-0001 · plm [GIT-FIRST POC]');

console.log(`routes.test.js: ${n} checks ok`);
