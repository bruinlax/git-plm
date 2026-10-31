/* diffwidgets.test.js — the diff-form registry's pure half. No DOM:
 *     node web/diffwidgets.test.js            (exit 0 = every case holds)
 *     node web/diffwidgets.test.js --kinds    (the registry's forms per kind,
 *                                              as JSON, for tests/test_diff_forms.py)
 * Run by tests/test_diff_forms.py when node is on the host. */
'use strict';
const assert = require('assert');
const D = require('./diffwidgets.js');

if (process.argv.includes('--kinds')) {
  const out = {};
  for (const r of D.REG) for (const k of r.kinds || []) out[k] = r.forms.map(f => f.id);
  console.log(JSON.stringify(out));
  process.exit(0);
}

let n = 0;
const eq = (a, b, why) => { assert.deepStrictEqual(a, b, why); n++; };
const ids = (f) => D.formsFor(f).map(x => x.id);

const mesh = { kind: 'mesh', path: 'bezel.stl', headPath: 'bezel.stl', basePath: 'bezel.stl',
               status: 'modified', binary: true, patch: '', same_content: false };
// ---- the mesh row: Overlay when the geometry moved, Pointer when it did not
eq(D.lookup(mesh).id, 'mesh');
eq(ids(mesh), ['overlay', 'stats', 'pointer']);
eq(D.choose(mesh), 'overlay', 'geometry differs → overlay');
eq(D.choose({ ...mesh, same_content: true }), 'pointer', 'identical bytes → pointer');
eq(D.choose({ ...mesh, same_content: null }), 'overlay', 'unknown → overlay');
eq(D.choose({ ...mesh, path: 'a.3mf', headPath: 'a.3mf', basePath: 'a.3mf' }), 'pointer', 'no 3MF reader → pointer');
// ---- the page's query maps to a form
eq(D.choose(mesh, { form: 'stats' }), 'stats');
eq(D.choose(mesh, { form: 'stats', file: 'bezel.stl' }), 'stats');
eq(D.choose(mesh, { form: 'stats', file: 'other.stl' }), 'overlay', 'a form for ANOTHER file is not this one');
eq(D.choose(mesh, { form: 'nonsense' }), 'overlay');
eq(D.choose({ ...mesh, same_content: true }, { mesh: 'bezel.stl' }), 'overlay', '?mesh= asks for the overlay');
eq(D.choose(mesh, { mesh: 'other.stl' }), 'overlay');
// ---- the others
eq(D.choose({ kind: 'image', path: 'r.png' }), 'sidebyside');
eq(ids({ kind: 'image', path: 'r.png' }), ['sidebyside', 'swipe', 'onion', 'difference']);
eq(D.choose({ kind: 'manifest', path: 'plm.toml', patch: '@@ -1 +1 @@' }), 'fields');
eq(ids({ kind: 'manifest', path: 'plm.toml' }), ['fields', 'design', 'text']);
eq(D.choose({ kind: 'text', path: 'frame.scad', patch: '@@ -1 +1 @@\n-a\n+b' }), 'text');
eq(ids({ kind: 'text', path: 'frame.scad' }), ['text', 'split']);
eq(D.choose({ kind: 'markdown', path: 'README.md', patch: 'x' }), 'text');
eq(D.choose({ kind: 'submodule', path: 'parts/X-PRT-0001' }), 'pin');
eq(D.choose({ kind: 'pattern', path: 'a.flat.json', patch: 'x' }), 'text');
// unknown types: text when git reads the blob as text, else size + blob
eq(D.lookup({ kind: 'binary', path: 'x.bin' }).id, 'other');
eq(ids({ kind: 'binary', path: 'x.bin', binary: true }), ['size']);
eq(D.choose({ kind: 'binary', path: 'x.bin', binary: true }), 'size');
eq(ids({ kind: 'binary', path: '.gitattributes', binary: false, patch: '@@ -1 +1 @@' }), ['text', 'split', 'size']);
eq(D.choose({ kind: 'binary', path: '.gitattributes', binary: false, patch: '@@ -1 +1 @@' }), 'text');
eq(D.choose({ kind: 'pdf', path: 'd.pdf', binary: true }), 'size');
// ---- a row with no kind (an older server) is classified by its extension
eq(D.kindOf({ path: 'x.STL' }), 'mesh');
eq(D.kindOf({ headPath: 'sub/plm.toml' }), 'manifest');
eq(D.kindOf({ path: 'photo.jpeg' }), 'image');
eq(D.kindOf({ path: 'mystery' }), 'binary');
eq(D.kindOf({ path: 'x.stl', kind: 'binary' }), 'binary', 'the server\'s kind wins');
// ---- the header's words
eq(D.roleLabel({ kind: 'submodule' }), 'submodule pin');
eq(D.roleLabel({ kind: 'manifest' }), 'plm.toml');
eq(D.roleLabel({ kind: 'mesh', role: 'viz' }), 'viz');
eq(D.roleLabel({ kind: 'text' }), 'text');
eq(D.startsOpen(mesh), true);
eq(D.startsOpen({ kind: 'text', patch: 'x', additions: 100, deletions: 100 }), false);
eq(D.startsOpen({ kind: 'text', patch: 'x', additions: 3, deletions: 1 }), true);
eq(D.startsOpen({ kind: 'binary', binary: true }), false);
// ---- the registry contract
assert.throws(() => D.register({ id: 'x' }));
eq(D.REG[D.REG.length - 1].id, 'other', 'the fallback is last');
D.register({ id: 'gcode', kinds: [], match: (f) => /\.gcode$/.test(f.path || ''),
             forms: [{ id: 'layers', label: 'Layers', render() {} }], defaultForm: () => 'layers' });
eq(D.REG[D.REG.length - 1].id, 'other', 'a later registration lands before the fallback');
eq(D.choose({ kind: 'text', path: 'a.gcode', patch: 'x' }), 'text', 'first registered match wins');
D.REG.splice(D.REG.findIndex(r => r.id === 'gcode'), 1);
// ---- split view pairs a deletion run with the addition run after it
const rs = D.diffRows('@@ -1,3 +1,3 @@\n a\n-b\n-c\n+B\n d');
eq(rs.map(r => r[0]), ['hunk', 'ctx', 'del', 'del', 'add', 'ctx']);
eq(rs[4][4], 2, 'head line numbers');
const sp = D.splitRows(rs);
eq(sp.length, 5);
eq([sp[2][0][1], sp[2][1][1]], ['b', 'B']);
eq([sp[3][0][1], sp[3][1]], ['c', null]);
// ---- plm.toml fields, one line
eq(D.fieldsLine([{ key: 'version', op: 'changed', from: '0.2.0', to: '0.2.1' },
                 { key: 'bom[20].qty', op: 'changed', from: 2, to: 4 },
                 { key: 'files.toolpath', op: 'removed', from: ['a.gcode'], to: null }]),
   'version "0.2.0" → "0.2.1"; bom[20].qty 2 → 4; files.toolpath − ["a.gcode"]');
// ---- LFS pointer text = what git sees
eq(D.pointerText({ blob: 'b', size: 131, lfs: { oid: 'f'.repeat(64), size: 900 } }),
   `version https://git-lfs.github.com/spec/v1\noid sha256:${'f'.repeat(64)}\nsize 900`);
eq(D.sizeDelta({ sizeBase: 1024, sizeHead: 2048 }), '1.0 KB → 2.0 KB (+1.0 KB)');
// ---- STL facts: a unit cube, binary STL
function cubeStl(s, off) {
  const v = [[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]].map(p => p.map((x, i) => x * s + off[i]));
  const F = [[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]];
  const buf = new ArrayBuffer(84 + 50 * F.length);
  const dv = new DataView(buf);
  dv.setUint32(80, F.length, true);
  F.forEach((f, i) => f.forEach((vi, k) => v[vi].forEach((x, a) => dv.setFloat32(84 + i * 50 + 12 + k * 12 + a * 4, x, true))));
  return buf;
}
const c = D.stlFacts(cubeStl(10, [5, 0, 0]));
eq(c.triangles, 12);
eq(Math.round(c.volume_mm3), 1000);
eq(c.bbox_mm.map(Math.round), [10, 10, 10]);
eq(c.com_mm.map(x => Math.round(x * 100) / 100), [10, 5, 5]);
// ---- rows: a renamed-by-version pair is ONE row
const cmp = { files: [
  { path: 'a_v1.stl', status: 'deleted', kind: 'mesh', pair: 0, ident_base: { blob: '1' } },
  { path: 'a_v2.stl', status: 'added', kind: 'mesh', pair: 0, ident_head: { blob: '2' } },
  { path: 'frame.scad', status: 'modified', kind: 'text', patch: '@@', additions: 1, deletions: 1 }],
  pairs: [{ key: 'a.stl', kind: 'mesh', base_index: 0, head_index: 1,
            base: { path: 'a_v1.stl', url: '/b' }, head: { path: 'a_v2.stl', url: '/h' } }] };
const R = D.rows(cmp);
eq(R.length, 2);
eq([R[0].renamed, R[0].basePath, R[0].headPath, R[0].ident_base.blob, R[0].ident_head.blob],
   [true, 'a_v1.stl', 'a_v2.stl', '1', '2']);
eq(D.choose(R[0]), 'overlay');
eq(D.rows(cmp), R, 'memoised');

// ---- SIDES (§2.9 "Sides and colours"): one helper names both halves
// full ctx: the change page's review_base + member head over the compare
const chg = { sides: { old: { ref: 'draft/v0.4.0', sha: '07f61ab9d0c1', version: '0.4.0' },
                       new: { ref: 'draft/v0.4.1', sha: '9c1d2e0aa1b2', version: '0.4.1' } },
              cmp: { base: { ref: '07f61ab9d0c1', commit: '07f61ab9d0c1', version: '0.4.0' },
                     head: { ref: '9c1d2e0aa1b2', commit: '9c1d2e0aa1b2', version: '0.4.1' },
                     commits: [{ sha: '9c1d2e0aa1b2', date: '2026-10-13T09:12:00Z' }] } };
const S = D.sides(chg);
eq(D.sideLabel(S.old), 'old v0.4.0');
eq(D.sideLabel(S.new, 'short'), 'new v0.4.1');
eq(D.sideLabel(S.old, 'full', 'parts/assembly.stl'), 'old · v0.4.0 · draft/v0.4.0 @ 07f61ab · assembly.stl');
eq(D.sideLabel(S.new, 'full', 'assembly.stl'), 'new · v0.4.1 · draft/v0.4.1 @ 9c1d2e0 · 2026-10-13 · assembly.stl',
   'the head commit date comes from the range it is in');
// missing version: the word alone, and the full line still says where
const nov = D.sides({ cmp: { base: { ref: 'main', commit: 'abcdef123456' }, head: { ref: 'draft/x', commit: '1234567aaaa' } } });
eq(D.sideLabel(nov.old), 'old');
eq(D.sideLabel(nov.old, 'full'), 'old · main @ abcdef1');
// missing sha: the ref alone, no dangling "@"
const nosha = D.sides({ sides: { old: { ref: 'v0.3.0', version: '0.3.0' }, new: { ref: 'draft/v0.4.0', version: '0.4.0' } } });
eq(D.sideLabel(nosha.old, 'full'), 'old · v0.3.0', 'a tag equal to the version is not repeated');
eq(D.sideLabel(nosha.new, 'full', 'a.stl'), 'new · v0.4.0 · draft/v0.4.0 · a.stl');
// a ref that is only a sha is a commit, not a ref
const shaRef = D.sides({ cmp: { base: { ref: '07f61ab', commit: '07f61ab9d0c1', version: '1.0.0' }, head: {} } });
eq(D.sideLabel(shaRef.old, 'full'), 'old · v1.0.0 · 07f61ab');
eq(D.sideLabel(D.sides({}).new, 'full'), 'new', 'nothing known: the word');
// the compare page: two refs, versions from the trees
const pageCtx = { cmp: { base: { ref: 'v0.1.0', commit: 'aaaaaaa1111', version: '0.1.0' },
                         head: { ref: 'draft/v0.2.0', commit: 'bbbbbbb2222', version: '0.2.0' } } };
const page = D.sides(pageCtx);
eq([D.sideLabel(page.old), D.sideLabel(page.new)], ['old v0.1.0', 'new v0.2.0']);
eq(D.sideLabel(page.old, 'full'), 'old · v0.1.0 @ aaaaaaa');
// a rename: each side names its own file
const rn = D.rowSides(pageCtx, { basePath: 'a_v1.stl', headPath: 'a_v2.stl' });
eq([D.sideLabel(rn.old, "full", rn.old.path), D.sideLabel(rn.new, "full", rn.new.path)],
   ['old · v0.1.0 @ aaaaaaa · a_v1.stl', 'new · v0.2.0 · draft/v0.2.0 @ bbbbbbb · a_v2.stl']);
// NO FORM shows the same header for both sides when the refs differ — even
// the worst case: the same version and file on both sides, two commits apart
const same = { cmp: { base: { ref: 'draft/v0.4.0', commit: '1111111aaaa', version: '0.4.0' },
                      head: { ref: 'draft/v0.4.0', commit: '2222222bbbb', version: '0.4.0' } } };
const rowsByKind = { mesh, image: { kind: 'image', path: 'r.png', basePath: 'r.png', headPath: 'r.png' },
  text: { kind: 'text', path: 'a.scad', basePath: 'a.scad', headPath: 'a.scad', patch: '@@' },
  markdown: { kind: 'markdown', path: 'R.md', basePath: 'R.md', headPath: 'R.md', patch: '@@' },
  binary: { kind: 'binary', path: 'x.bin', basePath: 'x.bin', headPath: 'x.bin', binary: true } };
for (const [k, f] of Object.entries(rowsByKind)) {
  for (const fm of D.formsFor(f)) {
    for (const ctx of [same, chg, pageCtx]) {
      const h = D.rowSides(ctx, f);
      for (const mode of ['short', 'full']) {
        const a = D.sideLabel(h.old, mode, h.old.path), b = D.sideLabel(h.new, mode, h.new.path);
        assert.notStrictEqual(a, b, `${k}/${fm.id}/${mode}: both sides read "${a}"`); n++;
      }
    }
  }
}
eq(D.sideLabel(D.sides(same).old, 'full', 'p.stl') !== D.sideLabel(D.sides(same).new, 'full', 'p.stl'), true);
// the Overlay's Bodies list: "old · assembly.stl" / "new · assembly.stl"
const ov = D.overlaySides(chg, { basePath: 'assembly.stl', headPath: 'assembly.stl', baseUrl: '/b', headUrl: '/h' }, 'X-1');
eq(ov.map(x => [x.side, x.which, x.label, x.short]),
   [['base', 'old', 'old · assembly.stl', 'old v0.4.0'], ['head', 'new', 'new · assembly.stl', 'new v0.4.1']]);
eq(ov[0].title, 'old · v0.4.0 · draft/v0.4.0 @ 07f61ab · assembly.stl');
eq(ov.map(x => x.color), ['#3E6FB0', '#CC7A12'], 'node reads the fallback tokens');

console.log(`diffwidgets.test.js: ${n} checks ok`);
