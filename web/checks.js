/* checks.js — CI for physical things, in the browser (PLM_REVIEW_MODEL §2.6).
 *
 * Self-contained on purpose: this file carries its own CSS and hangs one global
 * off `window`, so `web/index.html` gains ONE <script> line and `web/app.js`
 * gains two one-line mount calls. Everything a check knows how to look like
 * lives here, and deleting the file deletes the feature.
 *
 * It borrows from app.js rather than re-implementing: `el`, `api`, `post`,
 * `mdToHtml`, `cmpPathQ`, `toast`. Those are globals in the same document and
 * they are the reason a check's `details_md` renders like every other document
 * in this UI instead of like a log line.
 *
 * ⭐ THE ONE IDEA ON THE PAGE. A check row states a FACT about a commit; the
 * VERDICT belongs to the maturity being asked for. So `pins-released` is the
 * same amber row on every page, and the header says "blocks a release" only
 * where a release is what is being asked. That is §2.3's table, rendered —
 * never a second opinion computed in the client.
 */
(function () {
  'use strict';

  /* Drawing-office marks. A status is a word AND a mark, never a colour alone:
   * this page is read by somebody who may be colour-blind and is definitely
   * looking at it on a phone in a garage. */
  const MARK = {
    pass:    { glyph: '✓', word: 'pass',    cls: 'ok' },
    fail:    { glyph: '✗', word: 'fail',    cls: 'bad' },
    warn:    { glyph: '!', word: 'warn',    cls: 'warn' },
    skipped: { glyph: '–', word: 'skipped', cls: 'skip' },
    error:   { glyph: '✗', word: 'error',   cls: 'bad' },
    queued:  { glyph: '·', word: 'queued',  cls: 'wait' },
    running: { glyph: '·', word: 'running', cls: 'wait' },
  };
  /* The word says what HAPPENED, not what colour it is. "green" tells a
   * colour-blind reader nothing and tells everybody else nothing they cannot
   * already see; "all pass" / "1 failed" is the sentence. */
  const ROLLUP = {
    green:   { cls: 'ok',   glyph: '✓', word: 'all pass',  long: 'every check passed' },
    amber:   { cls: 'warn', glyph: '!', word: 'warnings',  long: 'something warned or could not run' },
    red:     { cls: 'bad',  glyph: '✗', word: 'failed',    long: 'a check failed, or a required one did not pass' },
    pending: { cls: 'wait', glyph: '·', word: 'running…',  long: 'checks are still running' },
  };

  const CSS = `
.chk { --chk-ok:var(--st-released); --chk-warn:var(--st-prototype);
       --chk-bad:var(--st-obsolete); --chk-skip:var(--ink-3); }
.chk .row { display:grid; grid-template-columns:22px 1fr auto; gap:0 10px;
  padding:9px 14px; border-top:1px solid var(--rule); align-items:baseline; }
.chk .row:first-of-type { border-top:0; }
.chk .row.open { background:var(--paper); }
.chk .mk { font:700 14px/1.3 var(--mono); text-align:center; }
.chk .mk.ok { color:var(--chk-ok); } .chk .mk.bad { color:var(--chk-bad); }
.chk .mk.warn { color:var(--chk-warn); } .chk .mk.skip { color:var(--chk-skip); }
.chk .mk.wait { color:var(--ink-3); animation:chkpulse 1.1s ease-in-out infinite; }
@keyframes chkpulse { 0%,100% { opacity:.25 } 50% { opacity:1 } }
.chk .nm { font-weight:600; }
.chk .nm .req { font:600 9.5px/1 var(--ui); letter-spacing:.1em; margin-left:7px;
  text-transform:uppercase; color:var(--ink-3); border:1px solid var(--rule-2);
  border-radius:2px; padding:2px 4px; vertical-align:1px; }
.chk .nm .req.blocks { color:var(--chk-bad); border-color:var(--chk-bad); }
.chk .sum { grid-column:2; color:var(--ink-2); font-size:12.5px; margin-top:2px;
  overflow-wrap:anywhere; }
.chk .meta { grid-column:3; grid-row:1/3; text-align:right; white-space:nowrap;
  font:11px var(--mono); color:var(--ink-3); }
.chk .meta .more { display:block; margin-top:3px; background:none; border:0;
  padding:0; color:var(--blue); cursor:pointer; font:11px var(--mono); }
.chk .meta .more:hover { text-decoration:underline; }
/* a row's images — GitHub's output.images: the first one large, then a strip */
.chk .imgs { grid-column:1/-1; display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 2px; }
.chk .imgs figure { margin:0; flex:0 1 220px; min-width:0; }
.chk .imgs figure.lead { flex:1 1 100%; }
.chk .imgs img { display:block; width:100%; height:auto; border:1px solid var(--rule); background:#fff; }
.chk .imgs figcaption { font:11px var(--mono); color:var(--ink-3); margin-top:3px; overflow-wrap:anywhere; }
.chk .det { grid-column:1/-1; margin:8px 0 2px; padding:10px 14px;
  background:var(--sheet); border:1px solid var(--rule); border-radius:3px;
  font-size:13px; }
.chk .det > :first-child { margin-top:0; } .chk .det > :last-child { margin-bottom:0; }
.chk .det table { border-collapse:collapse; margin:8px 0; font:12px var(--mono); }
.chk .det th, .chk .det td { border:1px solid var(--rule); padding:3px 9px;
  text-align:left; }
.chk .det th { background:var(--paper); font:600 11px var(--ui);
  letter-spacing:.04em; text-transform:uppercase; color:var(--ink-3); }
.chk .det pre { background:var(--paper); border:1px solid var(--rule);
  border-radius:3px; padding:9px 11px; overflow-x:auto; font:11.5px/1.5 var(--mono);
  white-space:pre; }
.chk .det code { font:12px var(--mono); background:var(--paper); padding:1px 4px;
  border-radius:2px; }
.chk .det pre code { background:none; padding:0; }
.chk .pts { grid-column:1/-1; display:flex; flex-wrap:wrap; gap:6px;
  margin:6px 0 0; }
.chk .pts a, .chk .pts span.pt { font:11.5px var(--mono); color:var(--blue);
  border:1px solid var(--rule); border-radius:3px; padding:2px 7px;
  background:var(--sheet); }
.chk .pts span.pt { color:var(--ink-2); }
.chk .head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.chk .head .right { margin-left:auto; display:flex; gap:8px; align-items:center; }
.chk .note { color:var(--ink-3); font-size:12px; padding:0 14px 11px; }
.chk .blocked { border-top:1px solid var(--rule); margin-top:2px;
  padding:10px 14px 9px; font-size:12.5px; color:var(--chk-bad);
  background:var(--st-obsolete-bg); }
.chk .shaline { font:11px var(--mono); color:var(--ink-3);
  border-top:1px solid var(--rule); padding:8px 14px 9px; }

/* the rollup chip — on the proposal header, a Drafts row and a queue row */
/* a row with no room for the chip: the mark alone (word in the title) */
.chkmark { font:700 12.5px/1 var(--mono); }
.chkmark.ok { color:var(--st-released); } .chkmark.bad { color:var(--st-obsolete); }
.chkmark.warn { color:var(--st-prototype); } .chkmark.skip, .chkmark.wait { color:var(--ink-3); }
.chkchip { display:inline-flex; align-items:center; gap:6px; font:11.5px var(--mono);
  border:1px solid var(--rule-2); border-radius:3px; padding:1px 7px;
  background:var(--sheet); white-space:nowrap; vertical-align:1px;
  /* the card's h3 uppercases everything inside it; a stamp is not a heading */
  text-transform:none; letter-spacing:0; }
.chkchip .g { font-weight:700; }
.chkchip.ok   { border-color:var(--st-released);  background:var(--st-released-bg);
                color:var(--st-released); }
.chkchip.warn { border-color:var(--st-prototype); background:var(--st-prototype-bg);
                color:var(--st-prototype); }
.chkchip.bad  { border-color:var(--st-obsolete);  background:var(--st-obsolete-bg);
                color:var(--st-obsolete); }
.chkchip.wait { color:var(--ink-3); }
.chkchip a { color:inherit; }
.chkchip .tally { color:var(--ink-2); }
.chkchip.ok .tally, .chkchip.warn .tally, .chkchip.bad .tally { color:inherit; opacity:.8; }

/* Phone. The third column cannot survive at 430 px, so the row becomes three
 * stacked lines — and the ORDER matters: name, then what it found, then how
 * long it took. The first attempt let the duration land between the name and
 * the summary, which put the least important fact in the reader's path. */
@media (max-width:560px) {
  .chk .row { grid-template-columns:20px 1fr; padding:9px 12px; }
  .chk .mk   { grid-column:1; grid-row:1; }
  .chk .nm   { grid-column:2; grid-row:1; }
  .chk .sum  { grid-column:2; grid-row:2; }
  .chk .meta { grid-column:2; grid-row:3; text-align:left; margin-top:5px; }
  .chk .meta .more { display:inline; margin:0 0 0 12px; }
  .chk .det, .chk .pts, .chk .imgs { grid-column:1/-1; }
  .chk .det { padding:9px 11px; }
  .chk .det table { display:block; overflow-x:auto; max-width:100%; }
  .chk .head { gap:7px; }
}`;

  function css() {
    if (document.getElementById('chk-css')) return;
    const s = document.createElement('style');
    s.id = 'chk-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* `✓ 4 · ! 1 · ✗ 0` — the tally a reviewer scans before opening anything.
   * Only the statuses that HAPPENED are shown: a `· ✗ 0` on every green board
   * is noise, and a zero that is always there stops meaning zero. */
  function tally(counts) {
    const bits = [];
    for (const k of ['pass', 'warn', 'fail', 'error', 'skipped', 'running', 'queued']) {
      if (counts[k]) bits.push(`${MARK[k].glyph} ${counts[k]}`);
    }
    return bits.join(' · ') || 'nothing measured';
  }

  /* One check's status (`pass`, `warn`, …) or a rollup's state (`green`, …)
   * as its MARK — the list rows' dot, from the same two tables as the board. */
  function mark(spec) {
    const n = el('span', 'chkmark ' + spec.cls, spec.glyph);
    n.title = spec.word;
    return n;
  }
  const markNode = (status) => mark(MARK[status] || MARK.error);
  const stateMark = (state) => mark(ROLLUP[state] || ROLLUP.pending);

  function chipNode(rollup, href) {
    const spec = ROLLUP[rollup.state] || ROLLUP.pending;
    const n = el('span', 'chkchip ' + spec.cls);
    const running = rollup.counts.queued + rollup.counts.running > 0;
    const word = (rollup.state === 'red' && rollup.required_failed.length)
      ? 'blocks'
      // Nothing measured and nothing in flight is "not run", which is a
      // different sentence from "running…" and a very different one from
      // "failed". Say the one that is true.
      : (rollup.state === 'pending' && !running)
        ? (rollup.total ? 'incomplete' : 'not run')
        : spec.word;
    n.appendChild(el('span', 'g', word));
    if (rollup.total) n.appendChild(el('span', 'tally', tally(rollup.counts)));
    n.title = 'Checks (PLM_REVIEW_MODEL §2.6) — ' + spec.long
      + (rollup.required_failed.length
         ? `. ${rollup.required_failed.join(', ')} ${rollup.required_failed.length === 1 ? 'is' : 'are'} required for ${rollup.target_maturity || 'this target'}`
         : '')
      + (rollup.required_missing.length
         ? `. Waiting on ${rollup.required_missing.join(', ')}` : '');
    if (href) {
      const a = el('a'); a.href = href; a.appendChild(n);
      return a;
    }
    return n;
  }

  // -- data ------------------------------------------------------------------
  function url(number, sha, target) {
    return `/api/repo/${encodeURIComponent(number)}/checks?sha=`
      + encodeURIComponent(sha || '')
      + (target ? '&target=' + encodeURIComponent(target) : '');
  }

  async function fetchBoard(number, sha, target) {
    try { return await api(url(number, sha, target)); } catch { return null; }
  }

  // -- one row ---------------------------------------------------------------
  function rowNode(c, ctx) {
    const spec = MARK[c.status] || MARK.error;
    const row = el('div', 'row');
    row.appendChild(el('div', 'mk ' + spec.cls, spec.glyph)).title = spec.word;

    const nm = el('div', 'nm', c.label || c.name);
    nm.title = `${c.name} — ${c.help || ''}`;
    if (ctx.required && ctx.required.includes(c.name)) {
      const req = el('span', 'req', 'required');
      if (c.status !== 'pass') {
        req.classList.add('blocks');
        req.textContent = 'blocks';
      }
      req.title = `Policy requires ${c.name} for ${ctx.target || 'this target'}`
        + ` (PLM_REVIEW_MODEL §2.3). A required check that did not pass — `
        + `including one that could not run — is not permission to release.`;
      nm.appendChild(req);
    } else if (c.advisory) {
      const a = el('span', 'req', 'advisory');
      a.title = 'Never required by policy — worth fixing, never a gate.';
      nm.appendChild(a);
    }
    row.appendChild(nm);

    /* How long it took, said usefully. `0.00s` five times in a column is
     * noise; what a reader wants to know is whether this was free, whether it
     * came out of the render cache, or whether it really took a minute. */
    const meta = el('div', 'meta');
    const cached = c.facts && c.facts.cached;
    const t = c.duration_s;
    const when = cached ? 'cached'
      : t == null ? (c.cost || '')
        : t < 0.05 ? 'instant'
          : t < 1 ? `${t.toFixed(2)}s`
            : t < 90 ? `${t.toFixed(1)}s` : `${Math.round(t / 60)} min`;
    const wn = meta.appendChild(el('span', null, when));
    wn.title = cached
      ? 'Re-used the cached render for this (source blob, selector) — OpenSCAD '
        + 'was not run again.'
      : t != null ? `${t}s` : `cost: ${c.cost}`;
    row.appendChild(meta);
    row.appendChild(el('div', 'sum', c.summary || '(no summary)'));
    const imgs = imagesNode(c);
    if (imgs) row.appendChild(imgs);

    // Details: the check's own markdown, rendered the way every other document
    // in this UI is. Closed by default — the summary is meant to be enough.
    const hasDetail = !!(c.details_md || '').trim();
    const hasPoints = (c.points_at || []).length > 0;
    if (hasDetail || hasPoints) {
      const btn = el('button', 'more', 'details');
      let open = [];
      const toggle = (want) => {
        open.forEach((n) => n.remove());
        open = [];
        row.classList.toggle('open', want);
        btn.textContent = want ? 'hide' : 'details';
        if (!want) return;
        if (hasDetail) {
          const det = el('div', 'det');
          det.innerHTML = mdWithTables(c.details_md);
          open.push(row.appendChild(det));
        }
        const pts = pointsNode(c, ctx);
        if (pts) open.push(row.appendChild(pts));
      };
      btn.onclick = () => toggle(!open.length);
      meta.appendChild(btn);
      // A finding opens itself. Nobody clicks "details" on a board of five
      // green ticks, and everybody wants the red one already open.
      if (ctx.expand && (c.status === 'fail' || c.status === 'error')) toggle(true);
    }
    return row;
  }

  /* `images` — GitHub's `output.images` ({alt, image_url, caption}): what a
   * check SHOWS rather than says (physical-diff, §2.10). Always visible — a
   * picture you have to click "details" to find is a picture nobody sees. The
   * first is the summary image (large); each opens full size. */
  function imagesNode(c) {
    const ims = c.images || [];
    if (!ims.length) return null;
    const wrap = el('div', 'imgs');
    ims.forEach((im, i) => {
      const f = el('figure', i === 0 && ims.length > 1 ? 'lead' : null);
      const a = el('a'); a.href = im.image_url; a.target = '_blank'; a.rel = 'noopener';
      const img = el('img'); img.src = im.image_url; img.alt = im.alt || c.name; img.loading = 'lazy';
      a.appendChild(img);
      f.appendChild(a);
      if (im.caption) f.appendChild(el('figcaption', null, im.caption));
      wrap.appendChild(f);
    });
    return wrap;
  }

  /* `points_at` — the files a finding is ABOUT. A failed check is a
   * thread-shaped thing (§2.6), so the least it can do is put you in front of
   * the bytes. In a proposal we know the range, so the link opens the compare
   * view with that file open; on a bare ref there is nothing to compare it
   * against, so the path is stated rather than linked to a page that would
   * shrug. */
  function pointsNode(c, ctx) {
    const pts = c.points_at || [];
    if (!pts.length) return null;
    const wrap = el('div', 'pts');
    for (const p of pts) {
      const label = p.path + (p.line ? `:${p.line}` : '');
      if (ctx.base && ctx.head) {
        const a = el('a', null, label);
        a.href = cmpPathQ(ctx.number, ctx.base, ctx.head, { file: p.path });
        a.title = `Open ${p.path} in the compare view`;
        wrap.appendChild(a);
      } else {
        wrap.appendChild(el('span', 'pt', label));
      }
    }
    return wrap;
  }

  /* app.js's `mdToHtml` has no table support, and a measurement check is mostly
   * a table. Rather than grow the shared renderer (a file stage 3c is also in),
   * pipe tables out first and hand the rest to it unchanged — so a check's
   * prose, code fences and lists render exactly like every other markdown here.
   */
  function mdWithTables(src) {
    const out = [];
    const lines = String(src).split('\n');
    let i = 0, fence = false;
    let buf = [];
    const flush = () => { if (buf.length) { out.push(mdToHtml(buf.join('\n'))); buf = []; } };
    while (i < lines.length) {
      if (lines[i].startsWith('```')) fence = !fence;
      const isRow = !fence && /^\s*\|.*\|\s*$/.test(lines[i]);
      const isRule = i + 1 < lines.length
        && /^\s*\|[\s|:-]+\|\s*$/.test(lines[i + 1]);
      if (isRow && isRule) {
        flush();
        const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|')
          .map((x) => x.trim());
        const head = cells(lines[i]);
        i += 2;
        const body = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          body.push(cells(lines[i])); i += 1;
        }
        const cell = (t, v) => `<${t}>${inlineMd(v)}</${t}>`;
        out.push('<table><thead><tr>'
          + head.map((h) => cell('th', h)).join('') + '</tr></thead><tbody>'
          + body.map((r) => '<tr>' + r.map((v) => cell('td', v)).join('') + '</tr>').join('')
          + '</tbody></table>');
        continue;
      }
      buf.push(lines[i]); i += 1;
    }
    flush();
    return out.join('\n');
  }

  function inlineMd(s) {
    return escapeHtml(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  }

  // -- the card --------------------------------------------------------------
  /* One card, used on the proposal page and anywhere else a commit is the
   * subject. `ctx` = {number, sha, target, base, head, title}. */
  function card(ctx) {
    css();
    const box = el('div', 'doc chk');
    const h = el('h3');
    const head = el('div', 'head');
    head.appendChild(el('span', null, 'Checks'));
    const chipSlot = el('span');
    head.appendChild(chipSlot);
    const right = el('div', 'right');
    const rerun = el('button', 'btn tiny', 'Re-run');
    rerun.title = 'Measure this commit again. Instant checks are instant; the '
      + 'render is capped and cached by the source blob, so an unchanged model '
      + 'does not pay for OpenSCAD twice.';
    right.appendChild(rerun);
    head.appendChild(right);
    h.appendChild(head);
    box.appendChild(h);

    const list = el('div');
    box.appendChild(list);
    const foot = el('div', 'shaline');
    box.appendChild(foot);

    let timer = null, dead = false;
    const stop = () => { dead = true; if (timer) clearTimeout(timer); };
    // The card unmounts when the page re-renders; stop polling with it.
    const mo = new MutationObserver(() => {
      if (!box.isConnected) { stop(); mo.disconnect(); }
    });

    async function draw() {
      if (dead) return;
      const d = await fetchBoard(ctx.number, ctx.sha, ctx.target);
      if (dead || !box.isConnected) return stop();
      list.innerHTML = '';
      chipSlot.innerHTML = '';
      if (!d) {
        list.appendChild(el('div', 'note',
          'Checks could not be read for this commit.'));
        return;
      }
      chipSlot.appendChild(chipNode(d.rollup));
      const rctx = { ...ctx, required: d.rollup.required };
      if (!d.checks.length) {
        list.appendChild(el('div', 'note',
          'Nothing has been measured at this commit yet. Push again, or press '
          + 'Re-run — checks are keyed by the SHA, so a board belongs to '
          + 'exactly these bytes.'));
      }
      for (const c of d.checks) list.appendChild(rowNode(c, rctx));

      const r = d.rollup;
      let line = `${d.sha.slice(0, 10)} · ${d.checks.length} check`
        + `${d.checks.length === 1 ? '' : 's'}`;
      if (r.target_maturity) {
        line += ` · required for ${r.target_maturity}: `
          + (r.required.join(', ') || 'none');
      }
      foot.textContent = line;
      // ⚠️ THE POLL REDRAWS, SO THE BANNER HAS TO BE REMOVED FIRST. `draw()`
      // clears `list` and `chipSlot` but this banner lives on `box`, and a
      // PENDING board re-draws every 1.8 s — so it stacked up, one identical
      // red block per poll. Visible on a stage-7 proposal page because a board
      // with required checks that have not run stays pending for a while;
      // found in the browser, not in a test.
      for (const old of box.querySelectorAll(':scope > .blocked')) old.remove();
      if (r.required_failed.length || r.required_missing.length) {
        const w = el('div', 'blocked');
        const names = r.required_failed.concat(r.required_missing);
        w.textContent = `${names.join(', ')} `
          + `${names.length === 1 ? 'is' : 'are'} required for `
          + `${r.target_maturity}`
          + (r.required_missing.length && !r.required_failed.length
             ? ' and ' + (r.required_missing.length === 1 ? 'has' : 'have')
               + ' not finished yet.'
             : ' and did not pass.')
          + ' A push is never blocked by a check; this is what an approver is '
          + 'being asked to sign over.';
        box.insertBefore(w, foot);
      }
      if (r.state === 'pending') timer = setTimeout(draw, 1800);
    }

    rerun.onclick = async () => {
      rerun.disabled = true;
      try {
        const r = await post(
          `/api/repo/${encodeURIComponent(ctx.number)}/checks/run`,
          { sha: ctx.sha });
        toast(`queued ${r.queued.length} check(s) on ${r.sha.slice(0, 10)}`);
        setTimeout(draw, 300);
      } catch (e) {
        toast(e.message || 'could not queue the checks');
      } finally { rerun.disabled = false; }
    };

    queueMicrotask(() => {
      if (box.isConnected) mo.observe(box.parentNode || document.body,
                                      { childList: true, subtree: false });
      draw();
    });
    return box;
  }

  /* Replace a `threads · checks` placeholder with the real rollup, in place.
   * ⭐ ADDITIVE ON PURPOSE: app.js keeps rendering its placeholder and this
   * overwrites it when the answer arrives, so the hook in that file is ONE
   * added line and nothing is deleted from a file two branches are editing. */
  async function rollupInto(node, number, sha, opts) {
    if (!node || !sha) return;
    css();
    const o = opts || {};
    const d = await fetchBoard(number, sha, o.target);
    if (!d || !node.isConnected) return;
    node.textContent = '';
    node.className = (node.className || '').replace(/\bsoon\b/, '').trim();
    node.title = '';
    node.appendChild(chipNode(d.rollup, o.href));
    if (d.rollup.state === 'pending') {
      setTimeout(() => rollupInto(node, number, sha, opts), 2500);
    }
  }

  /* A revision with no commit in its repo has no commit to check. Same shape,
   * same place on the page, and the reason where the board would be — the whole
   * point of this feature is that a missing measurement says so. */
  function noGitNode(g) {
    css();
    const box = el('div', 'doc chk');
    box.innerHTML = '<h3>Checks</h3>';
    box.appendChild(el('div', 'note',
      'This revision has no commit in the repository — plm authored it rather '
      + 'than receiving it as a push, so there are no bytes at a SHA to '
      + 'measure. Checks are keyed by the commit on purpose: that is what '
      + 'makes "the checks were green" a statement about what shipped.'
      + (g.why ? ' (' + g.why + ')' : '')));
    return box;
  }

  /* The proposal page. Inserts the real card where §2.6 was promised, and
   * rewrites the "Threads · Checks" placeholder card to be about threads only —
   * DOM surgery from here rather than an edit in app.js, so the merge stays a
   * single added line. If the placeholder ever goes away, the card is appended
   * and nothing breaks. */
  function mountProposal(page, number, p) {
    const g = (p && p.git) || {};
    // The SHA a proposal is ABOUT is the revision's commit — the thing an
    // approval binds to (§2.2). On a revision plm authored rather than one that
    // arrived as a push, `git_commit` is a VAULT sha and resolves in no repo;
    // there is nothing to measure and saying so beats an empty board.
    const sha = (g.origin === false) ? '' : ((p.revision || {}).git_commit || '');
    const target = ((p.gate || {}).target_state || '').toLowerCase();
    const node = sha
      ? card({ number, sha, target, expand: true,
               base: g.base || '', head: g.head || '' })
      : noGitNode(g);
    // STAGE 13: the proposal page leaves a `.checks-slot` (threads are built
    // now, so there is no placeholder card left to share).
    const slot = page.querySelector('.checks-slot');
    if (slot) { slot.appendChild(node); return node; }
    const soon = page.querySelector('.doc.soon');
    if (soon) {
      page.insertBefore(node, soon);
      const h = soon.querySelector('h3');
      if (h) h.textContent = 'Threads';
      const note = soon.querySelector('.note');
      if (note) {
        note.textContent =
          'Review threads are not built yet. Anchored to a file line, a '
          + 'plm.toml key or a point on the mesh (PLM_REVIEW_MODEL §2.4), they '
          + 'slot in here. Checks are above — they are built.';
      }
    } else {
      page.appendChild(node);
    }
    return node;
  }

  window.CHECKS = { card, chipNode, markNode, stateMark, rollupInto, mountProposal, fetchBoard, tally };
})();
