/* diffwidgets.js — THE DIFF-FORM REGISTRY (docs/PLM_REVIEW_MODEL.md §2.9).
 *
 * Geoff, 2026-10-16: "for each file that changed — if it's an stl or something
 * I should be able to see not only the text changes — but the overlay as a
 * different 'form' of that change." And, standing: file widgets follow a
 * REGISTRATION pattern — one registration per file type, clean and DRY in code
 * and UI; diff ANY file, not just the primary.
 *
 * A changed file can be read in several FORMS (a mesh: Overlay / Stats /
 * Pointer; an image: Side by side / Swipe / Onion skin / Difference; text:
 * Unified / Split; plm.toml: Fields / Design / Text …). Each FILE TYPE is one
 * registration, and every surface that shows a changed file — the change
 * page's Files changed tab, the compare page, the proposal page — draws its
 * rows through this registry. There is no second implementation anywhere.
 *
 * ── THE CONTRACT ───────────────────────────────────────────────────────────
 *
 *   PLM_DIFF.register({
 *     id:    'mesh',                         one registration per file type
 *     kinds: ['mesh'],                       the server kinds it owns
 *                                            (plm/roles.py MEDIA_KINDS — whose
 *                                            `diff` tuple must equal the form
 *                                            ids below; tests/test_diff_forms.py)
 *     match: (file) => bool,                 first registered match wins; the
 *                                            fallback is registered last
 *     forms: [{ id, label, title?,
 *               when?:   (file) => bool,     offered only when true
 *               render:  (container, ctx) => void | Promise }],
 *     defaultForm: (ctx) => formId,          per ROW, not per kind: a mesh
 *                                            whose bytes did not change opens
 *                                            on `pointer`, not `overlay`
 *   })
 *
 *   file  a compare ROW (`PLM_DIFF.rows(cmp)`): {key, path, kind, role, status,
 *         isPrimary, renamed, basePath, headPath, baseUrl, headUrl, sizeBase,
 *         sizeHead, additions, deletions, binary, patch, ident_base,
 *         ident_head, same_content, fields} — the server's compare row
 *         (plm/gitrepo.compare) with its two sides named.
 *   ctx   {file, number, cmp, base, head (the compared refs), members (a
 *         change: item → {pid, page, compare}), review (bool: thread hooks)}
 *
 *   PLM_DIFF.lookup(file)        → the registration
 *   PLM_DIFF.formsFor(file)      → the forms offered for this row
 *   PLM_DIFF.choose(file, q)     → the form to show: `q.form` when offered;
 *                                  else `overlay` when `q.mesh` names this file;
 *                                  else the registration's defaultForm. PURE —
 *                                  node runs it (web/diffwidgets.test.js).
 *   PLM_DIFF.row(file, ctx, o)   → one file row (header: status, path, role,
 *                                  ± or size delta, a toggle per form, View
 *                                  file, ⋯; body: the chosen form, rendered
 *                                  when the row first nears the viewport)
 *   PLM_DIFF.card(title, file, ctx) → a featured card (the compare page's
 *                                  "What changed in the design" / "BOM
 *                                  children"), the same forms in its header
 *
 * Adding a differ is ONE `register` call here and the form id in roles.py.
 *
 * ⭐ ONE WebGL CONTEXT. The Overlay form never builds its own renderer: it is
 * app.js's `overlayHost`, which docks the single dark room (#viewer) into the
 * row that asks for it and leaves a "Show the 3-D diff" button in the row it
 * left. A row TAKES the room when it has sat in view for a moment and nothing
 * else visible holds it — so the overlay follows you down a Files tab, and
 * there are never N contexts.
 *
 * Loaded before app.js (index.html); renders call app.js's helpers (`el`,
 * `overlayHost`, `cmpDesignBody`, `pinChange`, `mdToHtml`, `fileMenu`,
 * `previewHref`, `escapeHtml`, `fmtSize`) at RENDER time only, so node can
 * load this file and test the pure half.
 */
(function (root) {
  'use strict';

  const REG = [];
  const NODE = typeof window === 'undefined';

  /* ------------------------------------------------------------ pure half */
  const basename = (p) => String(p || '').split('/').pop();
  const extOf = (p) => { const b = basename(p).toLowerCase(); const i = b.lastIndexOf('.');
                         return i < 0 ? '' : b.slice(i + 1); };
  // The server classifies (plm/roles.py kind_of, from the bytes when it can);
  // the extension is only consulted for a row that came without a kind.
  const EXT_KIND = { stl: 'mesh', '3mf': 'mesh', obj: 'mesh', ply: 'mesh',
    png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', svg: 'image',
    md: 'markdown', scad: 'text', toml: 'text', txt: 'text', json: 'text', py: 'text',
    csv: 'text', yaml: 'text', yml: 'text' };
  function kindOf(f) {
    if (f.kind) return f.kind;
    if (basename(f.headPath || f.basePath || f.path) === 'plm.toml') return 'manifest';
    return EXT_KIND[extOf(f.headPath || f.basePath || f.path)] || 'binary';
  }
  const pathOf = (f) => f.headPath || f.basePath || f.path || f.key || '';
  const isStl = (f) => ['stl'].includes(extOf(pathOf(f)));
  const hasPatch = (f) => !f.binary && !!f.patch;

  function register(spec) {
    if (!spec || !spec.id || typeof spec.match !== 'function' || !(spec.forms || []).length)
      throw new Error('PLM_DIFF.register needs {id, match, forms}');
    const at = REG.findIndex(r => r.id === spec.id);
    if (at >= 0) REG.splice(at, 1, spec); else REG.push(spec);
    // the fallback always stays last
    const fb = REG.findIndex(r => r.fallback);
    if (fb >= 0 && fb !== REG.length - 1) REG.push(REG.splice(fb, 1)[0]);
    return spec;
  }
  function lookup(f) { return REG.find(r => r.match(f)) || REG[REG.length - 1]; }
  function formsFor(f) {
    const r = lookup(f);
    const out = r.forms.filter(x => !x.when || x.when(f));
    return out.length ? out : r.forms.slice(0, 1);
  }
  function choose(f, q = {}) {
    const forms = formsFor(f).map(x => x.id);
    const p = pathOf(f);
    const named = q.file && (q.file === p || q.file === f.basePath || q.file === f.key);
    if (q.form && forms.includes(q.form) && (!q.file || named)) return q.form;
    if (q.mesh && (q.mesh === p || q.mesh === f.basePath) && forms.includes('overlay'))
      return 'overlay';
    const r = lookup(f);
    const d = r.defaultForm ? r.defaultForm({ file: f }) : forms[0];
    return forms.includes(d) ? d : forms[0];
  }
  /* What a row IS in the review's words — source / mesh / viz / doc /
   * plm.toml / submodule pin — from the manifest role when there is one. */
  function roleLabel(f) {
    const k = kindOf(f);
    if (k === 'submodule') return 'submodule pin';
    if (k === 'manifest') return 'plm.toml';
    return f.role || k;
  }
  /* Should the row start open? Small text and anything with a picture: yes.
   * A big patch and an opaque binary: one click away. */
  const OPEN_LINES = 140;
  function startsOpen(f) {
    const k = kindOf(f);
    if (['mesh', 'image', 'manifest', 'submodule'].includes(k)) return true;
    if (hasPatch(f)) return (f.additions || 0) + (f.deletions || 0) <= OPEN_LINES;
    return false;
  }

  /* The compare payload → one row per logical file, in git's order, with a
   * renamed-by-version PAIR (`floor_plate_v2.stl` ↔ `_v3.stl`) as one row. */
  function rowFromFile(f) {
    return { ...f, key: f.path, role: f.role || '', isPrimary: !!f.is_primary,
      status: f.status, renamed: false, reason: '',
      basePath: f.status === 'added' ? '' : f.path,
      headPath: f.status === 'deleted' ? '' : f.path,
      baseUrl: f.base_url, headUrl: f.head_url,
      sizeBase: f.size_base, sizeHead: f.size_head };
  }
  function rowFromPair(p, files) {
    const src = (p.base_index != null && files[p.base_index])
      || (p.head_index != null && files[p.head_index]) || {};
    return { ...p, key: p.key, role: p.role, isPrimary: !!p.is_primary,
      status: 'modified', renamed: true, reason: p.reason,
      basePath: (p.base || {}).path || '', headPath: (p.head || {}).path || '',
      baseUrl: (p.base || {}).url || null, headUrl: (p.head || {}).url || null,
      sizeBase: (p.base || {}).size, sizeHead: (p.head || {}).size,
      additions: 0, deletions: 0,
      // ⚠️ A PAIRED MESH NEVER FALLS INTO THE TEXT PATH: the two halves are a
      // separate add and delete to git, so neither carries a usable patch.
      binary: p.kind !== 'text', patch: '',
      ident_base: (files[p.base_index] || {}).ident_base || null,
      ident_head: (files[p.head_index] || {}).ident_head || null,
      pairSrc: src };
  }
  function rows(cmp) {
    if (cmp._rows) return cmp._rows;
    const files = cmp.files || [], pairs = cmp.pairs || [];
    const folded = new Set();
    for (const p of pairs) {
      if (p.base_index != null) folded.add(p.base_index);
      if (p.head_index != null) folded.add(p.head_index);
    }
    const out = [], placed = new Set();
    files.forEach((f, i) => {
      if (folded.has(i)) {
        const pi = f.pair;
        if (pi == null || placed.has(pi)) return;
        placed.add(pi);
        out.push(rowFromPair(pairs[pi], files));
        return;
      }
      out.push(rowFromFile(f));
    });
    pairs.forEach((p, i) => {
      if (!placed.has(i) && p.base_index == null && p.head_index == null) {
        placed.add(i); out.push(rowFromPair(p, files));
      }
    });
    Object.defineProperty(cmp, '_rows', { value: out, enumerable: false, configurable: true });
    return out;
  }

  /* `@@` hunks → [cls, text, glyph, oldLine, newLine]. The two line numbers are
   * what a review thread anchors to (stage 13). */
  function diffRows(patch) {
    const out = [];
    let started = false, o = 0, n = 0;
    for (const raw of String(patch || '').split('\n')) {
      if (!started) { if (raw.startsWith('@@')) started = true; else continue; }
      if (raw.startsWith('@@')) {
        const m = raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
        if (m) { o = +m[1]; n = +m[2]; }
        out.push(['hunk', raw, '', 0, 0]);
      } else if (raw.startsWith('+')) out.push(['add', raw.slice(1), '+', 0, n++]);
      else if (raw.startsWith('-')) out.push(['del', raw.slice(1), '−', o++, 0]);
      else if (raw.startsWith('\\') || raw.startsWith('…')) out.push(['meta', raw, '', 0, 0]);
      else out.push(['ctx', raw.startsWith(' ') ? raw.slice(1) : raw, '', o++, n++]);
    }
    return out;
  }
  /* Unified rows → side-by-side pairs [[left|null, right|null], …]: a run of
   * deletions faces the run of additions that follows it, line for line. */
  function splitRows(rs) {
    const out = [];
    let i = 0;
    while (i < rs.length) {
      const r = rs[i];
      if (r[0] === 'del' || r[0] === 'add') {
        const dels = [], adds = [];
        while (i < rs.length && rs[i][0] === 'del') dels.push(rs[i++]);
        while (i < rs.length && rs[i][0] === 'add') adds.push(rs[i++]);
        for (let k = 0; k < Math.max(dels.length, adds.length); k++)
          out.push([dels[k] || null, adds[k] || null]);
      } else { out.push([r, r]); i++; }
    }
    return out;
  }
  /* A size, the way the rest of plm writes one. */
  const fmtBytes = (n) => n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + ' MB'
    : n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';
  function sizeDelta(f) {
    const a = f.sizeBase == null ? f.size_base : f.sizeBase;
    const b = f.sizeHead == null ? f.size_head : f.sizeHead;
    if (a == null && b == null) return 'size unknown';
    if (a == null) return `new, ${fmtBytes(b)}`;
    if (b == null) return `was ${fmtBytes(a)}, removed`;
    if (a === b) return `${fmtBytes(a)}, same size`;
    const d = b - a;
    return `${fmtBytes(a)} → ${fmtBytes(b)} (${d > 0 ? '+' : '−'}${fmtBytes(Math.abs(d))})`;
  }
  /* What git sees of one side: the LFS pointer text, or the blob. */
  function pointerText(ident) {
    if (!ident) return '';
    if (ident.lfs) return `version https://git-lfs.github.com/spec/v1\noid sha256:${ident.lfs.oid}\nsize ${ident.lfs.size}`;
    return `blob ${ident.blob}  (${ident.size == null ? '?' : ident.size} bytes, stored in git — not LFS)`;
  }
  /* One value of plm.toml, printed. */
  const tomlVal = (v) => v === null || v === undefined ? '—'
    : typeof v === 'string' ? JSON.stringify(v) : Array.isArray(v) ? JSON.stringify(v)
    : String(v);
  function fieldsLine(fields) {
    return (fields || []).map(d => d.op === 'added' ? `${d.key} + ${tomlVal(d.to)}`
      : d.op === 'removed' ? `${d.key} − ${tomlVal(d.from)}`
      : `${d.key} ${tomlVal(d.from)} → ${tomlVal(d.to)}`).join('; ');
  }

  /* STL → the facts a reviewer asks about. Binary or ASCII; the signed-
   * tetrahedron volume (plm/checks.py mesh_facts' formula) and the volume-
   * weighted centroid of the same tetrahedra = the centre of mass at uniform
   * density. */
  function stlFacts(buf) {
    const dv = new DataView(buf);
    const tris = [];
    const bin = buf.byteLength >= 84 && buf.byteLength === 84 + 50 * dv.getUint32(80, true);
    if (bin) {
      const n = dv.getUint32(80, true);
      for (let i = 0; i < n; i++) {
        const o = 84 + i * 50 + 12;
        const v = [];
        for (let k = 0; k < 9; k++) v.push(dv.getFloat32(o + k * 4, true));
        tris.push(v);
      }
    } else {
      const txt = new TextDecoder().decode(new Uint8Array(buf));
      const re = /vertex\s+(\S+)\s+(\S+)\s+(\S+)/g;
      let m, cur = [];
      while ((m = re.exec(txt))) {
        cur.push(+m[1], +m[2], +m[3]);
        if (cur.length === 9) { tris.push(cur); cur = []; }
      }
    }
    if (!tris.length) throw new Error('no triangles');
    const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    let v6 = 0, cx = 0, cy = 0, cz = 0, area = 0;
    for (const t of tris) {
      for (let k = 0; k < 9; k++) { const a = k % 3; if (t[k] < lo[a]) lo[a] = t[k]; if (t[k] > hi[a]) hi[a] = t[k]; }
      const [ax, ay, az, bx, by, bz, qx, qy, qz] = t;
      const d = ax * (by * qz - bz * qy) + ay * (bz * qx - bx * qz) + az * (bx * qy - by * qx);
      v6 += d; cx += d * (ax + bx + qx); cy += d * (ay + by + qy); cz += d * (az + bz + qz);
      const ux = bx - ax, uy = by - ay, uz = bz - az, wx = qx - ax, wy = qy - ay, wz = qz - az;
      area += 0.5 * Math.hypot(uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx);
    }
    const com = v6 ? [cx / (4 * v6), cy / (4 * v6), cz / (4 * v6)] : null;
    return { triangles: tris.length, volume_mm3: Math.abs(v6) / 6, area_mm2: area,
             bbox_mm: [0, 1, 2].map(i => hi[i] - lo[i]), min_mm: lo, max_mm: hi,
             com_mm: com, inverted: v6 < 0 };
  }

  /* ── SIDES: what the two halves of a diff are called (§2.9 "Sides and
   * colours"). Geoff, 2026-10-16: "when it shows the 2 parts - it should show
   * either what commit - or version etc they are … so i can tell", then "i
   * just want to know what's kinda old and new effectively at first - and get
   * to that info if needed". So every surface says OLD and NEW (never
   * base/head — those are git's words), plus the version when there is one;
   * the ref, commit, date and file are one step away (a tooltip, the legend's
   * ⓘ). ONE helper formats both; every form calls it.
   *
   *   sides(ctx)                → {old, new}: {which, word, version, ref, sha, date}
   *     from ctx.sides.{old,new} (what the page KNOWS — the change page's
   *     review_base and member head) over ctx.cmp.{base,head} (what git
   *     resolved). A ref that is only a sha is not shown as a ref.
   *   sideLabel(side, 'short')  → "old v0.4.0"   ("old" when no version)
   *   sideLabel(side, 'full', path)
   *                             → "old · v0.4.0 · draft/v0.4.0 @ 07f61ab · 2026-10-13 · assembly.stl"
   *     (a tag equal to the version is not repeated: "new · v0.4.1 @ 9c1d2e0") */
  const SHA_RE = /^[0-9a-f]{7,40}$/i;
  function sides(ctx) {
    const c = ctx || {};
    if (c._sides) return c._sides;
    const out = {};
    for (const [which, gk] of [['old', 'base'], ['new', 'head']]) {
      const o = (c.sides || {})[which] || {};
      const g = (c.cmp || {})[gk] || {};
      const sha = String(o.sha || o.commit || g.commit || '');
      let ref = String(o.ref || g.ref || '').replace(/^refs\/(heads|tags)\//, '');
      if (SHA_RE.test(ref) || (sha && ref && sha.startsWith(ref))) ref = '';
      const version = String(o.version || g.version || '').replace(/^v/, '');
      let date = o.date || g.date || '';
      if (!date && sha) {
        const cm = ((c.cmp || {}).commits || []).find(x => x.sha === sha);
        if (cm && cm.date) date = cm.date;
      }
      out[which] = { which, word: which, version, ref, sha,
                     date: date ? String(date).slice(0, 10) : '' };
    }
    if (c && typeof c === 'object' && !Object.isFrozen(c))
      Object.defineProperty(c, '_sides', { value: out, enumerable: false, configurable: true });
    return out;
  }
  function sideLabel(side, mode = 'short', path = '') {
    if (!side) return '';
    const v = side.version ? `v${side.version}` : '';
    if (mode !== 'full') return v ? `${side.word} ${v}` : side.word;
    const sh = String(side.sha || '').slice(0, 7);
    const tagIsVersion = side.ref && side.ref === v;
    const at = side.ref && sh ? `${side.ref} @ ${sh}` : side.ref || sh;
    return [side.word, tagIsVersion ? '' : v, at, side.date, basename(path)]
      .filter(Boolean).join(' · ');
  }
  /* The two sides of a ROW: the old side names the file as it was, the new
   * side as it is (a rename reads "old · …_v2.stl" / "new · …_v3.stl"). */
  function rowSides(ctx, f) {
    const s = sides(ctx);
    f = f || (ctx || {}).file || {};
    return { old: { ...s.old, path: f.basePath || '' }, new: { ...s.new, path: f.headPath || '' } };
  }

  const PURE = { kindOf, roleLabel, startsOpen, choose, formsFor, lookup, rows,
                 diffRows, splitRows, sizeDelta, pointerText, fieldsLine, stlFacts,
                 basename, sides, sideLabel, rowSides };

  /* ============================================================ the DOM half */
  const CSS = `
.dfile .dfhead .forms { flex:none; }
.dfile .dfhead .forms .seg button { padding:2px 8px; font-size:11px; }
.dfile .dfhead .vf { font:11px var(--ui); flex:none; white-space:nowrap; }
.dfile .dfhead .samecontent { font:10.5px var(--mono); color:var(--ink-3); flex:none; }
.dfile .dfbodywrap { border-top:1px solid var(--rule); }
.dfile .dfbodywrap:empty:not([style*="min-height"]) { display:none; }
.dfsplit { overflow:auto; max-height:560px; background:var(--sheet); }
.dfsplit table { border-collapse:collapse; width:100%; font:12px/1.55 var(--mono); table-layout:fixed; }
.dfsplit td { padding:0 8px; white-space:pre; overflow:hidden; text-overflow:ellipsis;
  vertical-align:top; border-right:1px solid var(--rule); }
.dfsplit td.no { width:42px; color:var(--ink-3); text-align:right; font-size:11px; user-select:none; }
.dfsplit td.del { background:var(--del-wash); color:var(--plm-removed-ink); }
.dfsplit td.add { background:var(--add-wash); color:var(--plm-added-ink); }
.dfsplit th { position:sticky; top:0; z-index:1; text-align:left; padding:4px 8px;
  border-bottom:1px solid var(--rule); font-weight:normal; }
.dfsplit th.old { background:var(--plm-old-bg); box-shadow:inset 0 -2px 0 var(--plm-old); }
.dfsplit th.new { background:var(--plm-new-bg); box-shadow:inset 0 -2px 0 var(--plm-new); }
.dfsplit tr.hunk td { background:var(--blue-bg); color:var(--blue); font-size:11.5px; }
.dfsplit td.empty { background:#F6F7F4; }
.dfields { width:100%; border-collapse:collapse; font-size:12.5px; }
.dfields td { border-top:1px solid var(--rule); padding:5px 12px; vertical-align:top; }
.dfields tr:first-child td { border-top:0; }
.dfields td.k { font:12px var(--mono); white-space:nowrap; width:1%; }
.dfields td.op { font:600 10px var(--ui); letter-spacing:.08em; text-transform:uppercase;
  color:var(--ink-3); width:1%; white-space:nowrap; }
.dfields td.v { font:12px var(--mono); overflow-wrap:anywhere; }
.dfields .del { color:var(--plm-removed-ink); text-decoration:line-through; text-decoration-color:var(--plm-removed); }
.dfields .add { color:var(--plm-added-ink); }
.dfields .to { color:var(--ink-3); margin:0 6px; }
.dfsum { padding:8px 12px; font:12px var(--mono); color:var(--ink-2); background:#FBFCFA;
  border-bottom:1px solid var(--rule); overflow-wrap:anywhere; }
.dfptr { display:grid; grid-template-columns:1fr; font:12px/1.55 var(--mono); }
.dfptr .side { padding:6px 12px; white-space:pre-wrap; overflow-wrap:anywhere; }
.dfptr .side.base { background:var(--plm-old-bg); box-shadow:inset 3px 0 0 var(--plm-old); }
.dfptr .side.head { background:var(--plm-new-bg); box-shadow:inset 3px 0 0 var(--plm-new); }
.dfptr .side .sidetag { display:flex; margin-bottom:2px; }
.dfstats { width:100%; border-collapse:collapse; font:12px var(--mono); }
.dfstats th, .dfstats td { border-top:1px solid var(--rule); padding:5px 12px; text-align:right; }
.dfstats th { font:600 10px/1.2 var(--ui); letter-spacing:.08em; text-transform:uppercase;
  color:var(--ink-3); background:#FBFCFA; }
.dfstats td:first-child, .dfstats th:first-child { text-align:left; font-family:var(--ui); }
.dfstats td.up { color:var(--plm-added); } .dfstats td.down { color:var(--plm-removed); }
.dfstats th .sidetag { font-size:11px; letter-spacing:0; text-transform:none; }
.dfstats th.old { box-shadow:inset 0 -2px 0 var(--plm-old); }
.dfstats th.new { box-shadow:inset 0 -2px 0 var(--plm-new); }
.dfdiffimg { padding:10px 12px; }
.dfdiffimg canvas { max-width:100%; height:auto; display:block; border:1px solid var(--rule);
  background:#fff; image-rendering:pixelated; }
.dfdiffimg .lbl { display:block; margin-bottom:6px; }
.dfdiffimg .plmlegend { margin-bottom:6px; }
.dfpin { display:flex; flex-wrap:wrap; gap:4px 10px; align-items:baseline; padding:8px 12px;
  font:12.5px var(--mono); }
.dfpin .arr { color:var(--ink-3); }
.dfpin a.mem { font-weight:600; }
.dfpin .dim { color:var(--ink-3); font-size:11.5px; }
.dfovwait { padding:10px 12px; font-size:12px; color:var(--ink-2); }
@media (max-width: 700px) {
  .dfile .dfhead { flex-wrap:wrap; }
  .dfile .dfhead .p { flex-basis:100%; order:-1; white-space:normal; overflow-wrap:anywhere; }
}`;
  function css() {
    if (NODE || document.getElementById('dfw-css')) return;
    const s = document.createElement('style');
    s.id = 'dfw-css'; s.textContent = CSS;
    document.head.appendChild(s);
  }

  const E = (tag, cls, text) => {               // app.js's `el`, without depending on load order
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const note = (host, text) => host.appendChild(E('div', 'dfnote', text));

  /* ── the colour TOKENS, read once from index.html's :root. three.js and a
   * canvas need the value itself; the fallbacks are only for a page that
   * forgot the stylesheet (and node). */
  const TOK_FALLBACK = { '--plm-old': '#3E6FB0', '--plm-new': '#CC7A12',
                         '--plm-removed': '#CF222E', '--plm-added': '#1A7F37' };
  const TOK = {};
  function tok(name) {
    if (TOK[name]) return TOK[name];
    let v = '';
    if (!NODE) try { v = getComputedStyle(document.documentElement).getPropertyValue(name).trim(); } catch (_) { /* */ }
    return (TOK[name] = v || TOK_FALLBACK[name] || '#888888');
  }
  const sideColor = (which) => tok(which === 'old' || which === 'base' ? '--plm-old' : '--plm-new');

  /* A side TAG: ■ old v0.4.0 — the full line is its tooltip. */
  function sideTag(side, path, text) {
    const t = E('span', 'sidetag ' + side.which);
    t.append(E('span', 'sw'), E('span', 't', text || sideLabel(side, 'short')));
    t.title = sideLabel(side, 'full', path || side.path);
    return t;
  }
  /* THE LEGEND — one line, the same on every surface:
   *   ■ old v0.4.0  ■ new v0.4.1  ■ removed  ■ added  ⓘ
   * ⓘ opens the two full lines (ref @ commit · date · file). o.changes:false
   * drops removed/added where a form draws no change colours. */
  function legend(ctx, f, o = {}) {
    const S = rowSides(ctx, f);
    const box = E('span', 'plmlegend');
    const tx = o.text || {};
    box.append(sideTag(S.old, S.old.path, tx.old), sideTag(S.new, S.new.path, tx.new));
    if (o.changes !== false) for (const k of ['removed', 'added']) {
      const c = E('span', 'chg');
      c.append(E('span', 'sw ' + k), E('span', null, k));
      c.title = k === 'removed' ? 'volume, lines or pixels that went away'
                                : 'volume, lines or pixels that appeared';
      box.appendChild(c);
    }
    const more = E('button', 'more', 'ⓘ');
    more.type = 'button';
    more.title = 'which revision each side is: ref, commit, date, file';
    more.setAttribute('aria-expanded', 'false');
    const det = E('span', 'plmdetail');
    det.hidden = true;
    for (const s of [S.old, S.new]) {
      const line = E('span', s.which);
      line.append(E('b', null, '■ '), document.createTextNode(sideLabel(s, 'full', s.path)));
      det.appendChild(line);
    }
    more.onclick = (e) => { e.stopPropagation(); det.hidden = !det.hidden;
                            more.setAttribute('aria-expanded', String(!det.hidden)); };
    box.append(more, det);
    return box;
  }

  function diffBody(rs) {
    const body = E('div', 'dfbody');
    const SOFT = 1200;
    let at = 0;
    const paint = () => {
      const frag = document.createDocumentFragment();
      const end = Math.min(rs.length, at + SOFT);
      for (; at < end; at++) {
        const [cls, text, g] = rs[at];
        const line = E('div', 'dline ' + cls);
        line.append(E('span', 'g', g), E('span', 't', text || ' '));
        frag.appendChild(line);
      }
      body.appendChild(frag);
      const old = body.querySelector('.dfmore');
      if (old) old.remove();
      if (at < rs.length) {
        const more = E('button', 'dfmore', `show the next ${Math.min(SOFT, rs.length - at)} of `
          + `${rs.length - at} remaining lines`);
        more.onclick = paint;
        body.appendChild(more);
      }
    };
    paint();
    return body;
  }

  /* Every form's last resort: open each side on its own. */
  function openLinks(host, f) {   // the old / new words, as everywhere
    const p = E('div', 'pad');
    for (const [label, url, path] of [['old', f.baseUrl, f.basePath], ['new', f.headUrl, f.headPath]]) {
      if (!url) continue;
      const a = E('a', 'btn tiny', `open ${label}`);
      a.href = url; a.target = '_blank'; a.title = path; a.style.marginRight = '6px';
      p.appendChild(a);
    }
    if (p.children.length) host.appendChild(p);
  }

  function noTextChange(host, f) {
    note(host, f.renamed
      ? 'Paired by name, but git has no patch for the two halves — they are a separate add '
        + 'and delete in the tree. Open either side to read it.'
      : (f.status === 'added' || f.status === 'deleted') ? 'Empty file.'
      : 'No textual change — mode or metadata only.');
    openLinks(host, f);
  }

  /* -- text ----------------------------------------------------------------- */
  const UNIFIED = { id: 'text', label: 'Unified', title: 'The unified diff — click a line to start a thread',
    when: hasPatch,
    render(host, ctx) {
      const f = ctx.file, rs = diffRows(f.patch);
      if (!rs.length) { noTextChange(host, f); return; }
      const body = diffBody(rs);
      host.appendChild(body);
      // STAGE 13: on a review surface a line is a place to anchor a thread
      if (ctx.review !== false && root.REVIEW_UI && REVIEW_UI.ctx) REVIEW_UI.mountDiff(body, rs, f);
    } };
  const SPLIT = { id: 'split', label: 'Split', title: 'Old on the left, new on the right',
    when: hasPatch,
    render(host, ctx) {
      const f = ctx.file, rs = diffRows(f.patch);
      if (!rs.length) { noTextChange(host, f); return; }
      const wrap = E('div', 'dfsplit');
      const t = E('table');
      const cg = E('colgroup');
      for (const w of ['42px', '', '42px', '']) { const c = E('col'); if (w) c.style.width = w; cg.appendChild(c); }
      t.appendChild(cg);
      const S = rowSides(ctx, f);
      const hr = E('tr');
      for (const s of [S.old, S.new]) {
        const th = E('th', s.which); th.colSpan = 2;
        th.appendChild(sideTag(s, s.path));
        hr.appendChild(th);
      }
      const thead = E('thead'); thead.appendChild(hr); t.appendChild(thead);
      for (const [l, r] of splitRows(rs).slice(0, 4000)) {
        const tr = E('tr');
        if (l && l === r && l[0] === 'hunk') {
          tr.className = 'hunk';
          const td = E('td', null, l[1]); td.colSpan = 4; tr.appendChild(td);
        } else if (l && l === r && l[0] === 'meta') {
          const td = E('td', 'no', ''); tr.appendChild(td);
          const tx = E('td', null, l[1]); tx.colSpan = 3; tr.appendChild(tx);
        } else {
          const cell = (x, side) => {
            const cls = !x ? 'empty' : x[0] === 'del' ? 'del' : x[0] === 'add' ? 'add' : '';
            tr.append(E('td', 'no ' + cls, x ? String(side === 'l' ? x[3] : x[4]) : ''),
                      E('td', cls, x ? (x[1] || ' ') : ''));
          };
          cell(l, 'l'); cell(r, 'r');
        }
        t.appendChild(tr);
      }
      wrap.appendChild(t);
      host.appendChild(wrap);
    } };
  const SIZE = { id: 'size', label: 'Size', title: 'Binary — the size and the blob are what git honestly knows',
    render(host, ctx) {
      const f = ctx.file;
      const n = E('div', 'dfnote');
      n.append(document.createTextNode('Binary — no line diff. '), E('b', null, sizeDelta(f)));
      if (f.same_content === true) n.append(document.createTextNode(' · the bytes are identical'));
      n.appendChild(E('div', 'why', 'plm shows meshes and images; for everything else the size '
        + 'and the blob are all git honestly knows.'));
      host.appendChild(n);
      if (f.ident_base || f.ident_head) host.appendChild(pointerNode(f, ctx));
      openLinks(host, f);
    } };

  /* -- mesh ----------------------------------------------------------------- */
  function pointerNode(f, ctx) {
    const box = E('div', 'dfptr');
    const S = rowSides(ctx, f);
    for (const [side, id, sd] of [['base', f.ident_base, S.old], ['head', f.ident_head, S.new]]) {
      if (!id) continue;
      const s = E('div', 'side ' + side);
      s.append(sideTag(sd, sd.path), document.createTextNode(pointerText(id)));
      box.appendChild(s);
    }
    return box;
  }
  let OV_WATCH = null;          // rows waiting to take the one 3-D room
  function watchOverlay(mount, host) {
    if (!('IntersectionObserver' in root)) { host.open(); return; }
    if (!OV_WATCH) {
      const seen = new Map();
      OV_WATCH = { seen, io: new IntersectionObserver((ents) => {
        for (const e of ents) {
          const h = seen.get(e.target);
          if (!h) continue;
          clearTimeout(h.t);
          h.vis = e.intersectionRatio >= 0.35;
          if (!h.vis) continue;
          h.t = setTimeout(() => {
            if (!document.body.contains(e.target)) { OV_WATCH.io.unobserve(e.target); seen.delete(e.target); return; }
            // take the room only when its holder is not itself on screen
            const cur = typeof OVL !== 'undefined' ? OVL : null;   // app.js's one room
            if (cur === h.host) return;
            const curVis = cur && [...seen.values()].some(x => x.host === cur && x.vis);
            if (!curVis) h.host.open();
          }, 380);
        }
      }, { threshold: [0, 0.35, 0.7] }) };
    }
    OV_WATCH.seen.set(mount, { host, vis: false, t: 0 });
    OV_WATCH.io.observe(mount);
  }
  /* The overlay's two bodies, named the way the Bodies list and the legend
   * name them: "old · assembly.stl" / "new · assembly.stl", the full line in
   * the tooltip. app.js's compare-page geometry card builds its sides here too. */
  function overlaySides(ctx, f, num) {
    const S = rowSides(ctx, f);
    const out = [];
    for (const [side, url, s] of [['base', f.baseUrl, S.old], ['head', f.headUrl, S.new]]) {
      if (!url) continue;
      out.push({ side, which: s.which, color: sideColor(s.which), path: s.path, url, number: num,
                 label: `${s.word} · ${basename(s.path)}`, title: sideLabel(s, 'full', s.path),
                 short: sideLabel(s, 'short') });
    }
    return out;
  }
  function overlayReading(sides) {
    return sides.length === 2
      ? `Blue is ${sides[0].short}, ghosted; amber is ${sides[1].short}. Where they differ, red is `
        + 'volume that was removed and green is volume that was added.'
      : `Only the ${sides[0].which} side has this mesh — shown on its own.`;
  }
  const OVERLAY = { id: 'overlay', label: 'Overlay',
    title: 'The two revisions in one scene: red is volume that was removed, green volume that was added; '
      + 'amber is the new body where it did not move, blue the old one, ghosted',
    render(host, ctx) {
      const f = ctx.file;
      if (!isStl(f)) {
        note(host, `${extOf(pathOf(f)).toUpperCase() || 'This'} mesh: the 3-D overlay reads STL; `
          + 'use Pointer or open either side.');
        openLinks(host, f);
        return;
      }
      const num = ctx.number;
      const sides = overlaySides(ctx, f, num);
      if (!sides.length) { note(host, 'No fetchable mesh on either side.'); return; }
      const read = E('div', 'ovread');
      read.textContent = f.same_content === true ? 'No geometry change — the bytes are identical '
        + '(the git blob moved, the object did not). Both bodies coincide.'
        : overlayReading(sides);
      host.appendChild(read);
      const h = overlayHost(host, `${num}  ·  ${basename(pathOf(f))}`, sides,
                            { autoOpen: false, review: ctx.review !== false,
                              legend: () => legend(ctx, f) });
      watchOverlay(h.mount, h);
    } };

  async function checkFacts(number, sha, path) {
    if (!number || !sha) return null;
    try {
      const b = await api(`/api/repo/${encodeURIComponent(number)}/checks?sha=${encodeURIComponent(sha)}`);
      const g = (b.checks || []).find(c => c.name === 'geometry-facts' && c.facts
                                           && c.facts.path === path);
      return g ? g.facts : null;
    } catch (_) { return null; }
  }
  async function fetchFacts(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    const len = +(r.headers.get('content-length') || 0);
    if (len > 96 * 1024 * 1024) throw new Error('over 96 MB — not measured in the browser');
    return stlFacts(await r.arrayBuffer());
  }
  const STATS = { id: 'stats', label: 'Stats',
    title: 'Volume, bounding box, triangles and centre of mass, old vs new',
    render(host, ctx) {
      const f = ctx.file;
      if (!isStl(f)) { note(host, 'Measured for STL only (the same reader as the geometry-facts check).'); return; }
      const wait = E('div', 'dfovwait', 'measuring both sides…');
      host.appendChild(wait);
      const cmp = ctx.cmp || {};
      const side = async (path, url, sha) => {
        if (!url) return null;
        // the geometry-facts check already measured the primary at that sha;
        // the centre of mass is not one of its facts, so the browser computes
        // everything when it has to read the mesh anyway
        try { return { ...(await fetchFacts(url)), src: 'browser' }; }
        catch (e) {
          const c = await checkFacts(ctx.number, sha, path);
          return c ? { ...c, src: 'geometry-facts check' } : { error: e.message };
        }
      };
      Promise.all([side(f.basePath, f.baseUrl, (cmp.base || {}).commit),
                   side(f.headPath, f.headUrl, (cmp.head || {}).commit)]).then(([a, b]) => {
        wait.remove();
        const t = E('table', 'dfstats');
        const hr = E('tr');
        const S = rowSides(ctx, f);
        hr.appendChild(E('th', null, ''));
        for (const s of [S.old, S.new]) { const th = E('th', s.which); th.appendChild(sideTag(s, s.path)); hr.appendChild(th); }
        const dth = E('th', null, 'Δ'); dth.title = 'new − old: green grew, red shrank';
        hr.appendChild(dth);
        t.appendChild(hr);
        const n1 = (x, d = 1) => x == null || isNaN(x) ? '—' : Number(x).toFixed(d);
        const vec = (v) => v ? v.map(x => n1(x)).join(' × ') : '—';
        const pt = (v) => v ? '(' + v.map(x => n1(x)).join(', ') + ')' : '—';
        const row = (k, va, vb, dv, fmt = n1) => {
          const tr = E('tr');
          tr.appendChild(E('td', null, k));
          tr.appendChild(E('td', null, va));
          tr.appendChild(E('td', null, vb));
          const d = E('td', dv > 0 ? 'up' : dv < 0 ? 'down' : '', dv == null ? '—'
            : (dv > 0 ? '+' : dv < 0 ? '−' : '±') + fmt(Math.abs(dv)));
          tr.appendChild(d);
          t.appendChild(tr);
        };
        const A = a && !a.error ? a : null, B = b && !b.error ? b : null;
        const dlt = (x, y) => (x != null && y != null) ? y - x : null;
        row('Volume, cm³', n1(A && A.volume_mm3 / 1000, 3), n1(B && B.volume_mm3 / 1000, 3),
            dlt(A && A.volume_mm3 / 1000, B && B.volume_mm3 / 1000), (x) => x.toFixed(3));
        row('Triangles', A ? String(A.triangles) : '—', B ? String(B.triangles) : '—',
            dlt(A && A.triangles, B && B.triangles), (x) => String(x));
        row('Bounding box, mm', vec(A && A.bbox_mm), vec(B && B.bbox_mm), null);
        for (const [i, ax] of [[0, 'X'], [1, 'Y'], [2, 'Z']])
          row(`  ${ax} extent`, n1(A && A.bbox_mm[i]), n1(B && B.bbox_mm[i]),
              dlt(A && A.bbox_mm[i], B && B.bbox_mm[i]));
        row('Centre of mass, mm', pt(A && A.com_mm), pt(B && B.com_mm), null);
        if (A && B && A.com_mm && B.com_mm) {
          const d = Math.hypot(B.com_mm[0] - A.com_mm[0], B.com_mm[1] - A.com_mm[1], B.com_mm[2] - A.com_mm[2]);
          row('  moved by', '', '', d, (x) => x.toFixed(2) + ' mm');
        }
        host.appendChild(t);
        const src = [a, b].map((x, i) => x ? `${i ? 'new' : 'old'}: ${x.error ? 'could not read — ' + x.error : x.src}` : '')
          .filter(Boolean).join(' · ');
        note(host, `Measured by ${src}. Centre of mass assumes uniform density; volume is exact `
          + 'only for a closed mesh.');
      });
    } };
  const POINTER = { id: 'pointer', label: 'Pointer',
    title: 'What git sees: the LFS pointer (or the blob) on each side',
    render(host, ctx) {
      const f = ctx.file;
      host.appendChild(E('div', 'dfsum', f.same_content === true
        ? 'No geometry change — the content is byte-identical; only what git stores moved.'
        : f.same_content === false ? `The content changed: ${sizeDelta(f)}.`
        : `Content comparison not available — ${sizeDelta(f)}.`));
      if (!f.ident_base && !f.ident_head) { note(host, 'This compare carried no blob facts for the row.'); return; }
      host.appendChild(pointerNode(f, ctx));
    } };

  /* -- images --------------------------------------------------------------- */
  function imageStage(host, f, mode, ctx) {
    const S = rowSides(ctx || {}, f);
    if (!f.baseUrl && !f.headUrl) { note(host, 'No fetchable image on either side.'); return; }
    if (!f.baseUrl || !f.headUrl) {
      const only = E('div', 'imgone ' + (f.headUrl ? 'added' : 'removed'));
      const img = E('img');
      img.src = previewHref(f.headUrl || f.baseUrl);
      img.alt = f.headPath || f.basePath;
      const lb = E('div', 'lbl');
      lb.append(sideTag(f.headUrl ? S.new : S.old, f.headPath || f.basePath),
                document.createTextNode(f.headUrl ? ' only — added' : ' only — removed'));
      only.append(lb, img);
      host.appendChild(only);
      return;
    }
    const stage = E('div', 'imgstage');
    const wrapB = E('div', 'lay base'), wrapH = E('div', 'lay head');
    const imgB = E('img'), imgH = E('img');
    imgB.src = previewHref(f.baseUrl); imgH.src = previewHref(f.headUrl);
    imgB.alt = f.basePath; imgH.alt = f.headPath;
    if (mode === '2up') { wrapB.appendChild(sideTag(S.old, S.old.path)); wrapH.appendChild(sideTag(S.new, S.new.path)); }
    wrapB.append(imgB); wrapH.append(imgH);
    stage.append(wrapB, wrapH);
    const sizeTo = (img) => { if (img.naturalWidth) stage.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`; };
    imgH.onload = () => { if (mode !== '2up') sizeTo(imgH); };
    imgB.onload = () => { if (mode !== '2up' && !imgH.naturalWidth) sizeTo(imgB); };
    const bar = E('div', 'ovbar');
    const ctl = E('span', 'op');
    const slider = E('input');
    slider.type = 'range'; slider.min = 0; slider.max = 100; slider.value = 50;
    ctl.append(E('span', null, mode === 'swipe' ? 'swipe' : 'new opacity'), slider);
    const apply = () => {
      const v = Number(slider.value);
      if (mode === 'swipe') {
        wrapH.style.clipPath = `inset(0 0 0 ${v}%)`; wrapH.style.opacity = '1';
        stage.style.setProperty('--split', v + '%');
      } else if (mode === 'onion') { wrapH.style.clipPath = 'none'; wrapH.style.opacity = String(v / 100); }
    };
    slider.oninput = apply;
    if (mode !== '2up') bar.appendChild(ctl);
    // side by side: each picture wears its own tag; swipe / onion: the legend
    // says which is where (old left|under, new right|over)
    if (mode !== '2up') bar.appendChild(legend(ctx || {}, f, { changes: false,
      text: { old: `${sideLabel(S.old)} ${mode === 'swipe' ? 'left' : 'under'}`,
              new: `${sideLabel(S.new)} ${mode === 'swipe' ? 'right' : 'over'}` } }));
    stage.classList.toggle('two-up', mode === '2up');
    stage.classList.toggle('swiping', mode === 'swipe');
    if (mode === '2up') { wrapH.style.clipPath = 'none'; wrapH.style.opacity = '1'; } else apply();
    stage.addEventListener('pointermove', (e) => {
      if (mode !== 'swipe' || e.buttons !== 1) return;
      const r = stage.getBoundingClientRect();
      slider.value = String(Math.round(((e.clientX - r.left) / r.width) * 100));
      apply();
    });
    host.append(bar, stage);
    const foot = E('div', 'imgfoot');
    foot.append(E('span', null, `${basename(f.basePath)} → ${basename(f.headPath)}`),
                E('span', 'dim', sizeDelta(f)));
    host.appendChild(foot);
  }
  /* Bytes → an <img> we may read pixels from: fetched same-origin and handed
   * over as a blob URL, so a preview host elsewhere (PLM_PREVIEW_ORIGIN) can
   * never taint the canvas, and an SVG is rasterised with scripting off. */
  async function loadPixels(url, path) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    let blob = await r.blob();
    if (extOf(path) === 'svg') blob = new Blob([await blob.text()], { type: 'image/svg+xml' });
    const u = URL.createObjectURL(blob);
    const img = new Image();
    img.src = u;
    try { await img.decode(); } finally { setTimeout(() => URL.revokeObjectURL(u), 0); }
    return img;
  }
  const DIFFERENCE = { id: 'difference', label: 'Difference',
    title: 'Pixel difference: unchanged pixels faded, changed pixels in the added colour',
    render(host, ctx) {
      const f = ctx.file;
      if (!f.baseUrl || !f.headUrl) { imageStage(host, f, '2up', ctx); return; }
      const box = E('div', 'dfdiffimg');
      const lg = legend(ctx, f, { changes: false });
      const ch = E('span', 'chg');
      ch.append(E('span', 'sw added'), E('span', null, 'differs'));
      lg.insertBefore(ch, lg.querySelector('.more'));
      box.appendChild(lg);
      const lbl = E('span', 'lbl', 'computing the pixel difference…');
      box.appendChild(lbl);
      host.appendChild(box);
      Promise.all([loadPixels(f.baseUrl, f.basePath), loadPixels(f.headUrl, f.headPath)]).then(([a, b]) => {
        const W = Math.max(a.naturalWidth, b.naturalWidth), H = Math.max(a.naturalHeight, b.naturalHeight);
        const scale = Math.min(1, 1600 / Math.max(W, H));
        const w = Math.max(1, Math.round(W * scale)), h = Math.max(1, Math.round(H * scale));
        const draw = (img) => {
          const c = document.createElement('canvas'); c.width = w; c.height = h;
          const g = c.getContext('2d', { willReadFrequently: true });
          g.fillStyle = '#fff'; g.fillRect(0, 0, w, h);
          g.drawImage(img, 0, 0, img.naturalWidth * scale, img.naturalHeight * scale);
          return g.getImageData(0, 0, w, h);
        };
        const A = draw(a), B = draw(b);
        const out = document.createElement('canvas'); out.width = w; out.height = h;
        const og = out.getContext('2d');
        const D = og.createImageData(w, h);
        let changed = 0;
        const hx = tok('--plm-added').replace('#', '');
        const HI = hx.length === 6 ? [0, 2, 4].map(k => parseInt(hx.slice(k, k + 2), 16)) : [26, 127, 55];
        for (let i = 0; i < A.data.length; i += 4) {
          const d = Math.abs(A.data[i] - B.data[i]) + Math.abs(A.data[i + 1] - B.data[i + 1])
                  + Math.abs(A.data[i + 2] - B.data[i + 2]) + Math.abs(A.data[i + 3] - B.data[i + 3]);
          if (d > 24) {
            changed++;
            const k = Math.min(1, d / 255);
            // the --plm-added hue, stronger the more the pixel moved
            for (let c = 0; c < 3; c++) D.data[i + c] = Math.round(255 - (255 - HI[c]) * (0.55 + 0.45 * k));
            D.data[i + 3] = 255;
          } else {
            const y = 0.299 * B.data[i] + 0.587 * B.data[i + 1] + 0.114 * B.data[i + 2];
            const v = Math.round(255 - (255 - y) * 0.22);
            D.data[i] = D.data[i + 1] = D.data[i + 2] = v; D.data[i + 3] = 255;
          }
        }
        og.putImageData(D, 0, 0);
        const pct = (100 * changed / (w * h));
        lbl.textContent = changed
          ? `${pct < 0.01 ? '<0.01' : pct.toFixed(2)}% of pixels differ (green)`
            + (a.naturalWidth !== b.naturalWidth || a.naturalHeight !== b.naturalHeight
              ? ` · size ${a.naturalWidth}×${a.naturalHeight} → ${b.naturalWidth}×${b.naturalHeight}` : '')
          : 'No pixel differs — the picture is the same.';
        box.appendChild(out);
      }).catch((e) => { lbl.textContent = 'Could not compare the pixels: ' + e.message; openLinks(box, f); });
    } };

  /* -- plm.toml -------------------------------------------------------------- */
  const FIELDS = { id: 'fields', label: 'Fields', title: 'The keys of plm.toml that changed, by name',
    render(host, ctx) {
      const f = ctx.file, fs = f.fields || [];
      if (!fs.length) {
        note(host, f.path || f.headPath ? 'No key of plm.toml changed (formatting only).'
          : 'plm.toml did not change between these refs.');
        return;
      }
      const t = E('table', 'dfields');
      for (const d of fs) {
        const tr = E('tr');
        tr.dataset.key = d.key;
        tr.appendChild(E('td', 'k', d.key));
        tr.appendChild(E('td', 'op', d.op));
        const v = E('td', 'v');
        if (d.op !== 'added') v.appendChild(E('span', 'del', tomlVal(d.from)));
        if (d.op === 'changed') v.appendChild(E('span', 'to', '→'));
        if (d.op !== 'removed') v.appendChild(E('span', 'add', tomlVal(d.to)));
        tr.appendChild(v);
        t.appendChild(tr);
      }
      host.appendChild(t);
    } };
  const DESIGN = { id: 'design', label: 'Design', title: 'plm.toml read as what it means — redlines, BOM, attributes',
    render(host, ctx) { cmpDesignBody(host, ctx.number, ctx.cmp || {}); } };

  /* -- a BOM pin ------------------------------------------------------------- */
  function pinLabel(side) {
    if (!side) return '—';
    const ref = side.ref || '';
    const sh = String(side.commit || '').slice(0, 7);
    if (/^v\d/.test(ref)) return ref;
    if (ref.startsWith('draft/')) return `draft @ ${sh}`;
    return ref ? `${ref} @ ${sh}` : sh;
  }
  const PIN = { id: 'pin', label: 'Pin', title: 'The gitlink: which revision of the child this pins',
    render(host, ctx) {
      const f = ctx.file, cmp = ctx.cmp || {};
      const subs = (cmp.submodules || []).filter(s => !pathOf(f) || s.path === pathOf(f)
                                                      || s.path === f.key);
      if (!subs.length) { note(host, 'No BOM child moved between these refs.'); return; }
      for (const s of subs) {
        const line = E('div', 'dfpin');
        const child = s.number || basename(s.path);
        const mem = (ctx.members || {})[child];
        const who = E('a', 'mem', child);
        who.href = PLM_ROUTES.P.item(child);
        line.append(who, document.createTextNode(':'), E('span', null, pinLabel(s.from)),
                    E('span', 'arr', '→'), E('span', null, pinLabel(s.to)));
        if (mem) {
          const a = E('a', null, `(member #${mem.pid})`); a.href = mem.page;
          line.appendChild(a);
          if (mem.compare) { const c = E('a', null, 'compare ⇄'); c.href = mem.compare; line.appendChild(c); }
          line.appendChild(E('span', 'dim', 're-pinned to its tag when the pull request merges'));
        } else if (s.from && s.to && s.from.commit && s.to.commit) {
          const c = E('a', null, 'compare ⇄');
          c.href = PLM_ROUTES.P.compare(child, s.from.ref || s.from.commit, s.to.ref || s.to.commit);
          line.appendChild(c);
        }
        host.appendChild(line);
        // the child's own commit subjects between the two pins (git diff --submodule=log)
        if ((s.commits || []).length || s.note) {
          const pc = pinChange(s);
          const hd = pc.querySelector('.hd');
          if (hd) hd.remove();            // the line above already says from → to
          host.appendChild(pc);
        }
      }
    } };

  /* -- markdown --------------------------------------------------------------- */
  const RENDERED = { id: 'rendered', label: 'Rendered', title: 'Both sides rendered, side by side',
    render(host, ctx) {
      const f = ctx.file;
      const box = E('div', 'md2up');
      const S = rowSides(ctx, f);
      for (const [side, url, path, sd] of [['base', f.baseUrl, f.basePath, S.old], ['head', f.headUrl, f.headPath, S.new]]) {
        const col = E('div', 'md2col ' + side);
        const lb = E('div', 'lbl');
        lb.appendChild(sideTag(sd, path));
        col.appendChild(lb);
        const doc = E('div', 'mddoc');
        doc.textContent = url ? 'loading…' : 'not on this side';
        col.appendChild(doc);
        box.appendChild(col);
        if (url) fetch(url).then(r => r.ok ? r.text() : Promise.reject(new Error(r.status)))
          .then(t => { doc.innerHTML = mdToHtml(t); })
          .catch(e => { doc.textContent = 'could not read: ' + e.message; });
      }
      host.appendChild(box);
    } };

  /* ---------------------------------------------------- THE REGISTRATIONS */
  register({ id: 'submodule', kinds: ['submodule'], match: (f) => kindOf(f) === 'submodule',
    forms: [PIN], defaultForm: () => 'pin' });
  register({ id: 'manifest', kinds: ['manifest'], match: (f) => kindOf(f) === 'manifest',
    forms: [FIELDS, DESIGN, { ...UNIFIED, when: undefined }],
    defaultForm: () => 'fields' });
  register({ id: 'mesh', kinds: ['mesh'], match: (f) => kindOf(f) === 'mesh',
    forms: [OVERLAY, STATS, POINTER],
    defaultForm: ({ file }) => file.same_content === true ? 'pointer'
      : isStl(file) ? 'overlay' : 'pointer' });
  register({ id: 'image', kinds: ['image'], match: (f) => kindOf(f) === 'image',
    forms: [{ id: 'sidebyside', label: 'Side by side', render: (h, c) => imageStage(h, c.file, '2up', c) },
            { id: 'swipe', label: 'Swipe', render: (h, c) => imageStage(h, c.file, 'swipe', c) },
            { id: 'onion', label: 'Onion skin', render: (h, c) => imageStage(h, c.file, 'onion', c) },
            DIFFERENCE],
    defaultForm: () => 'sidebyside' });
  register({ id: 'markdown', kinds: ['markdown'], match: (f) => kindOf(f) === 'markdown',
    forms: [{ ...UNIFIED, when: undefined }, { ...SPLIT, when: undefined }, RENDERED],
    defaultForm: () => 'text' });
  register({ id: 'text', kinds: ['text', 'pattern'],
    match: (f) => ['text', 'pattern'].includes(kindOf(f)),
    forms: [{ ...UNIFIED, when: undefined }, { ...SPLIT, when: undefined }],
    defaultForm: () => 'text' });
  // Unknown types: text if git reads the blob as text (the server sends a
  // patch), else size + blob. Registered LAST — `register` keeps it there.
  register({ id: 'other', kinds: ['binary', 'cad', 'pdf', 'archive'], fallback: true,
    match: () => true,
    forms: [UNIFIED, SPLIT, SIZE],
    defaultForm: ({ file }) => hasPatch(file) ? 'text' : 'size' });

  /* ------------------------------------------------------------ one ROW */
  function seg(forms, cur, pick) {
    const s = E('div', 'seg');
    const btns = {};
    for (const fm of forms) {
      const b = E('button', fm.id === cur ? 'on' : null, fm.label);
      b.type = 'button';
      if (fm.title) b.title = fm.title;
      b.onclick = (e) => { e.stopPropagation(); for (const k in btns) btns[k].classList.toggle('on', k === fm.id); pick(fm.id); };
      btns[fm.id] = b;
      s.appendChild(b);
    }
    s.set = (id) => { for (const k in btns) btns[k].classList.toggle('on', k === id); };
    return s;
  }
  function renderForm(host, f, id, ctx) {
    const fm = lookup(f).forms.find(x => x.id === id) || formsFor(f)[0];
    // The one 3-D room may be docked in this body (Overlay → Stats): hand it
    // back to <body> first, or clearing the body detaches #viewer for good and
    // the render loop throws every frame.
    if (!NODE && typeof OVL !== 'undefined' && OVL && OVL.mount && host.contains(OVL.mount)) OVL.release();
    host.innerHTML = '';
    try {
      const p = fm.render(host, { ...ctx, file: f });
      if (p && p.catch) p.catch(e => note(host, 'Could not render: ' + e.message));
    } catch (e) { note(host, 'Could not render: ' + e.message); }
  }
  let LAZY = null;
  function lazy(node, fn) {
    if (!('IntersectionObserver' in root)) { fn(); return; }
    if (!LAZY) {
      const fns = new Map();
      LAZY = { fns, io: new IntersectionObserver((ents) => {
        for (const e of ents) {
          if (!e.isIntersecting) continue;
          const go = fns.get(e.target);
          fns.delete(e.target); LAZY.io.unobserve(e.target);
          if (go) go();
        }
      }, { rootMargin: '600px 0px' }) };
    }
    LAZY.fns.set(node, fn);
    LAZY.io.observe(node);
  }

  /* ctx: {number, cmp, members?, review?}; o: {q (the page's ?file=&form=&mesh=),
   * open (force)} */
  function row(f, ctx, o = {}) {
    css();
    const q = o.q || {};
    const k = kindOf(f);
    const forms = formsFor(f);
    let cur = choose(f, q);
    const box = E('div', 'dfile');
    const p0 = pathOf(f);
    box.dataset.file = p0;
    box.dataset.form = cur;
    const head = E('div', 'dfhead');
    const tw = E('span', 'tw', '▸');
    head.appendChild(tw);
    head.appendChild(E('span', 'st st-' + (f.renamed ? 'renamed' : f.status),
                       f.renamed ? 'renamed' : f.status));
    const p = E('span', 'p');
    if (f.renamed && basename(f.basePath) !== basename(f.headPath)) {
      p.append(E('span', 'nm', f.key));
      p.appendChild(E('span', 'was', `${basename(f.basePath)} → ${basename(f.headPath)}`));
    } else p.appendChild(E('span', 'nm', p0));
    p.title = [f.basePath, f.headPath].filter(Boolean).join('  →  ');
    head.appendChild(p);
    const rc = E('span', 'rolechip', roleLabel(f));
    rc.title = k === 'submodule' ? 'a gitlink — the BOM pins a revision of a child repo'
      : f.role ? `manifest role: ${f.role}` : `kind: ${k}`;
    head.appendChild(rc);
    if (f.isPrimary) {
      const pc = E('span', 'rolechip primary', 'primary');
      pc.title = 'The geometry of record — [files] primary in plm.toml';
      head.appendChild(pc);
    }
    const n = E('span', 'n');
    if (k === 'submodule') n.appendChild(E('span', 'dim', 'pin bump'));
    else if (!f.binary) n.append(E('span', 'plus', `+${f.additions || 0}`), document.createTextNode(' '),
                                  E('span', 'minus', `−${f.deletions || 0}`));
    else n.appendChild(E('span', 'dim', sizeDelta(f)));
    head.appendChild(n);
    if (f.same_content === true) head.appendChild(E('span', 'samecontent', k === 'mesh' ? 'no geometry change' : 'same bytes'));
    const body = E('div', 'dfbodywrap');
    const sw = forms.length > 1 ? seg(forms, cur, (id) => { cur = id; box.dataset.form = id; toggle(true, true); }) : null;
    if (sw) { const w = E('span', 'forms'); w.appendChild(sw); head.appendChild(w); }
    const ref = ctx.cmp && ctx.cmp.head ? (f.status === 'deleted' ? ctx.cmp.base.ref : ctx.cmp.head.ref) : '';
    const atPath = f.status === 'deleted' ? f.basePath : f.headPath;
    if (ref && atPath && k !== 'submodule' && ctx.number) {
      const vf = E('a', 'vf', 'View file');
      vf.href = PLM_ROUTES.P.blob(ctx.number, ref, atPath);
      vf.title = `${atPath} at ${ref}`;
      vf.onclick = (e) => e.stopPropagation();
      head.appendChild(vf);
    } else if (k === 'submodule') {
      const s = ((ctx.cmp || {}).submodules || []).find(x => x.path === p0);
      if (s && s.to) {
        const vf = E('a', 'vf', 'View child');
        vf.href = PLM_ROUTES.P.tree(s.number, s.to.ref || s.to.commit);
        vf.onclick = (e) => e.stopPropagation();
        head.appendChild(vf);
      }
    }
    if (typeof fileMenu === 'function' && k !== 'submodule') {
      const side = { ...f, path: atPath || p0, name: basename(atPath || p0),
                     url: f.headUrl || f.baseUrl, size: f.sizeHead ?? f.sizeBase };
      if (typeof fileInfo === 'function') fileInfo(side);
      head.appendChild(fileMenu(side, { number: ctx.number, ref }));
    }
    box.append(head, body);
    let open = false, drawn = '';
    // Until a body is drawn it holds roughly its own height, so rows drawn
    // lazily above you do not shove the row you scrolled to off the screen.
    const guess = () => k === 'mesh' ? 660 : k === 'image' ? 360 : k === 'submodule' ? 60
      : hasPatch(f) ? Math.min(560, 22 + 19 * ((f.additions || 0) + (f.deletions || 0) + 6)) : 60;
    const draw = () => {
      if (drawn === cur) return;
      drawn = cur; body.style.minHeight = '';
      renderForm(body, f, cur, ctx);
    };
    const toggle = (force, now) => {
      open = force === undefined ? !open : force;
      tw.textContent = open ? '▾' : '▸';
      body.style.display = open ? '' : 'none';
      if (!open) return;
      if (now) draw(); else { if (drawn !== cur) body.style.minHeight = guess() + 'px'; lazy(box, draw); }
    };
    head.onclick = (ev) => { if (!ev.target.closest('.fmwrap, a, button, .forms')) toggle(undefined, true); };
    const wanted = q.file && (q.file === p0 || q.file === f.basePath || q.file === f.key)
      || (q.mesh && (q.mesh === p0 || q.mesh === f.basePath));
    box.open = (id) => { if (id && forms.some(x => x.id === id)) { cur = id; if (sw) sw.set(id); } toggle(true, true); };
    if (wanted) {
      toggle(true, true);
      queueMicrotask(() => { box.scrollIntoView({ block: 'start' }); box.classList.add('wanted'); });
    } else toggle(o.open !== undefined ? o.open : startsOpen(f));
    return box;
  }

  /* A featured card: the same registration, its forms in the card header. */
  function card(title, f, ctx) {
    css();
    const c = E('div', 'doc');
    const h = E('h3', null, title);
    c.appendChild(h);
    const body = E('div');
    c.appendChild(body);
    const forms = formsFor(f);
    let cur = choose(f, {});
    if (forms.length > 1) {
      const r = E('span', 'right');
      r.appendChild(seg(forms, cur, (id) => { cur = id; renderForm(body, f, cur, ctx); }));
      h.appendChild(r);
    }
    renderForm(body, f, cur, ctx);
    return c;
  }

  const api = { register, lookup, formsFor, choose, row, card, rows, kindOf, roleLabel,
                startsOpen, diffRows, splitRows, sizeDelta, pointerText, fieldsLine,
                stlFacts, imageStage, sides, sideLabel, rowSides, sideTag, legend, tok,
                sideColor, overlaySides, overlayReading, REG, PURE };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PLM_DIFF = api;
})(typeof window !== 'undefined' ? window : this);
