/* review.js — THREADS in the browser (stage 13, PLM_REVIEW_MODEL §2.4).
 *
 * A thread is a comment with an ANCHOR: the proposal itself, a line of a file,
 * a plm.toml key, or a point on the mesh — each at a commit. This file is the
 * human half: start a thread from a diff line, a redline row or a point clicked
 * on the 3-D overlay; read them in one panel (open / resolved / outdated);
 * reply, resolve, apply a suggested change; and "show me what they saw" — the
 * camera the reviewer was looking through, restored in model space.
 *
 * Self-contained like checks.js: its own CSS, one `window.REVIEW_UI` global.
 * It borrows app.js's helpers (`el`, `api`, `post`, `when`, `whoChip`,
 * `refusalBox`, `toast`, `decisionBody`, the `V` viewer, `vDot`, `vSprite`,
 * `vKill`) — they are globals in the same document, and using them is why a
 * thread's pin is the same surveyor's mark the measure tool draws, not a second
 * marker style that would imply a difference that does not exist.
 *
 * ONE CONTEXT, A SCOPE. `REVIEW_UI.begin({scope, …})` takes a LIST of subjects
 * (`[["proposal", 310]]` today). Every panel reads `/api/threads?<scope>`, so a
 * stage-14 CHANGE page mounts the same panel over N proposals; the one thing a
 * scope with several items must add is `subjectFor(number)` (which proposal a
 * new thread on that item's diff belongs to) — stubbed below as the first.
 *
 * Ink (PLM_MARKUP_UX §2.6, kept): redline #d2503b is a REQUEST; amber dashed is
 * "as drawn, on bytes that have since changed" (OUTDATED) — never re-projected.
 */
(function () {
  'use strict';

  const RED = '#d2503b', AMBER = '#c2b48a';

  const CSS = `
:root { --rv-red:${RED}; --rv-amber:#9A6200; }
.rv-chip { display:inline-block; font:600 9.5px/1 var(--ui); letter-spacing:.1em;
  text-transform:uppercase; border:1px solid var(--rule-2); border-radius:2px;
  padding:2px 5px; color:var(--ink-2); background:var(--sheet); vertical-align:1px; }
.rv-chip.block { color:#fff; background:var(--rv-red); border-color:var(--rv-red); }
.rv-chip.out { color:var(--rv-amber); border-color:var(--rv-amber); border-style:dashed; }
.rv-chip.res { color:var(--st-released); border-color:var(--st-released); }
.rv-chip.open { color:var(--rv-red); border-color:var(--rv-red); }
.rv-chip.sug { color:var(--blue); border-color:var(--blue); }
.rv-chip.adv { color:var(--st-superseded); border-color:var(--st-superseded); }
.rv-chip.on { text-transform:none; letter-spacing:0; font:600 10.5px var(--mono); color:var(--ink-2); }
.rv-thr { border-top:1px solid var(--rule); padding:10px 12px 11px; }
.rv-thr:first-child { border-top:0; }
.rv-thr.resolved { background:#FAFBF9; }
.rv-thr.resolved .rv-cm { opacity:.78; }
.rv-thr .hd { display:flex; flex-wrap:wrap; gap:5px 7px; align-items:center; }
.rv-thr .hd .tid { font:600 11px var(--mono); color:var(--ink-3); }
.rv-thr .hd .ttl { font-weight:600; min-width:0; flex:0 1 auto; }
.rv-thr .hd .by { font-size:12px; color:var(--ink-2); }
.rv-thr .hd .when { margin-left:auto; font-size:11px; }
.rv-thr .anc { font:11.5px var(--mono); color:var(--ink-2); margin:4px 0 2px;
  word-break:break-all; }
.rv-thr .anc a { color:var(--blue); cursor:pointer; }
.rv-thr .anc .q { display:block; color:var(--ink-3); white-space:pre-wrap;
  border-left:2px solid var(--rule-2); padding-left:7px; margin-top:3px; }
.rv-thr .why { font-size:11.5px; color:var(--rv-amber); margin-top:2px; }
.rv-cm { display:grid; grid-template-columns:auto 1fr auto; gap:0 8px;
  padding:5px 0 0; font-size:13px; align-items:baseline; }
.rv-cm .who { font-weight:600; font-size:12px; }
.rv-cm .b { word-break:break-word; min-width:0; }
.rv-cm .b p { margin:0 0 4px; } .rv-cm .b p:last-child { margin:0; }
.rv-cm .b code { font:12px var(--mono); background:var(--paper); padding:0 3px; }
.rv-cm .when { font-size:11px; }
.rv-sug { margin-top:7px; font:12px var(--mono); background:var(--blue-bg);
  border:1px solid #cfd9f2; padding:6px 8px; display:flex; flex-wrap:wrap;
  gap:6px 10px; align-items:center; }
.rv-sug .was { text-decoration:line-through; color:var(--plm-removed-ink); }
.rv-sug .now { color:var(--plm-added-ink); font-weight:600; }
.rv-acts { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; align-items:center; }
.rv-acts input, .rv-acts textarea { font:13px var(--ui); }
.rv-addr { font-size:12px; color:var(--ink-2); margin-top:5px; }
.rv-addr .sha { font:11.5px var(--mono); }
.rv-comp { border:1px solid var(--rv-red); background:#FFFBFA; padding:9px 10px;
  margin:6px 0; white-space:normal; font:13px/1.45 var(--ui); color:var(--ink); }
.rv-comp .lbl { font:600 10px/1.2 var(--ui); letter-spacing:.12em; color:var(--rv-red);
  text-transform:uppercase; margin-bottom:5px; }
.rv-comp .anc { font:11.5px var(--mono); color:var(--ink-2); margin-bottom:6px;
  word-break:break-all; }
.rv-comp textarea { width:100%; min-height:62px; font:13px/1.45 var(--ui);
  border:1px solid var(--rule-2); padding:6px 7px; resize:vertical; }
.rv-comp .row { display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center;
  margin-top:7px; font-size:12px; }
.rv-comp .row input[type=text] { font:12px var(--mono); padding:3px 6px;
  border:1px solid var(--rule-2); width:9em; }
.rv-filt { display:flex; flex-wrap:wrap; gap:6px; padding:8px 12px;
  border-bottom:1px solid var(--rule); align-items:center; }
.rv-filt button.on { background:var(--ink); color:#fff; border-color:var(--ink); }
.rv-filt .n { font:11px var(--mono); margin-left:4px; opacity:.8; }
.rv-empty { padding:12px; color:var(--ink-3); font-size:12.5px; }
/* diff gutter: a line you can comment on */
.dfbody.rv-on .dline { grid-template-columns:22px auto; cursor:text; }
.dfbody.rv-on .dline .g { cursor:pointer; }
.dfbody.rv-on .dline:not(.hunk):not(.meta):hover .g::before { content:'+';
  color:#fff; background:var(--rv-red); border-radius:2px; padding:0 4px;
  font-weight:700; }
.dfbody.rv-on .dline:not(.hunk):not(.meta):hover .g .gx { display:none; }
.dfbody .dline.rv-has .g { box-shadow: inset 3px 0 0 var(--rv-red); }
.rv-inline { white-space:normal; position:sticky; left:0; width:var(--rv-w, 100%);
  max-width:100vw; border-top:1px solid var(--rule); border-bottom:1px solid var(--rule);
  background:var(--paper); padding:2px 8px; }
.rv-inline .rv-thr { background:var(--sheet); border:1px solid var(--rule); margin:6px 0; }
/* redline rows */
.rlrow .rv-k { margin-left:6px; border:0; background:none; cursor:pointer;
  font:600 11px var(--mono); color:var(--ink-3); padding:0 3px; }
.rlrow .rv-k:hover, .rlrow .rv-k.has { color:var(--rv-red); }
.rlrow + .rv-inline, .rv-inline.rl { position:static; width:auto; }
/* the 3-D pin tool */
.ovbar button.rv-pin.on, .ovbar .rv-pin.on:hover { background:var(--rv-red); border-color:var(--rv-red); color:#fff; }
.rv-pinhint { font-size:12px; color:var(--rv-red); padding:6px 12px 0; }
.rv-pinlist { padding:4px 12px 8px; display:flex; flex-wrap:wrap; gap:6px; }
.rv-pinlist button { font:11.5px var(--mono); }
.rv-pinlist button.out { border-style:dashed; color:var(--rv-amber); }
/* the proposal page's header + decision bar (app.js propHeader / propDecideP) */
.pstate { font:600 9.5px/1 var(--ui); letter-spacing:.1em; text-transform:uppercase;
  border:1px solid var(--rule-2); border-radius:2px; padding:3px 6px; }
.pstate.ps-open { color:var(--blue); border-color:var(--blue); }
.pstate.ps-rejected { color:var(--st-obsolete); border-color:var(--st-obsolete); }
.pstate.ps-merged { color:var(--st-released); border-color:var(--st-released); }
.pstate.ps-withdrawn { color:var(--ink-3); }
a.thrcount { font:600 11.5px var(--ui); color:${RED}; text-decoration:none;
  border-bottom:1px dotted ${RED}; }
a.thrcount.blocks::before { content:'■ '; }
.signoffs { border:1px solid var(--rule); margin:0 0 10px; }
.signoffs .lbl { font:600 10px/1.2 var(--ui); letter-spacing:.12em; color:var(--ink-3);
  text-transform:uppercase; padding:6px 9px; border-bottom:1px solid var(--rule); }
.sorow { display:flex; flex-wrap:wrap; gap:4px 8px; align-items:baseline;
  padding:6px 9px; border-top:1px solid var(--rule); font-size:12.5px; }
.sorow:first-of-type { border-top:0; }
.sorow .mk { font:700 13px var(--mono); width:16px; text-align:center; }
.sorow .mk.v-approve { color:var(--st-released); }
.sorow .mk.v-reject { color:var(--st-obsolete); }
.sorow .sha { font:11.5px var(--mono); color:var(--ink-2); }
.sorow .when { margin-left:auto; font-size:11px; }
.sorow .b { flex-basis:100%; color:var(--ink-2); white-space:pre-wrap; padding-left:24px; }
.sorow.dismissed > b, .sorow.dismissed > span:not(.dism):not(.when) {
  text-decoration:line-through; color:var(--ink-3); }
.sorow .dism { font:600 10.5px var(--ui); color:#9A6200; }
.thrwarn { border-left:3px solid ${RED}; background:#FFF6F4; padding:7px 10px;
  margin:0 0 10px; font-size:12.5px; }
.thrwarn.blocks { font-weight:600; }
a.pnumlink { font:600 11.5px var(--mono); color:var(--blue); }
.thrbadge { font:11.5px var(--ui); color:var(--ink-3); }
.thrbadge.open { color:${RED}; font-weight:600; }
.thrbadge.blocks::before { content:'■ '; }
@media (max-width: 560px) {
  .rv-cm { grid-template-columns:1fr auto; }
  .rv-cm .b { grid-column:1 / -1; }
  .rv-comp .row input[type=text] { width:7em; }
}
`;

  const R = {
    ctx: null,          // { scope, number, pid, head, base, headRef, baseRef, threads }
    listeners: new Set(),
    ovHost: null,       // the geometry card's overlay host, for "show me"
    pinMode: false,
  };

  function injectCss() {
    if (document.getElementById('rv-css')) return;
    const s = document.createElement('style');
    s.id = 'rv-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }
  injectCss();

  const scopeQs = (scope) => scope.map(([t, id]) => `${t}=${id}`).join('&');
  const short = (s) => String(s || '').slice(0, 10);

  /* ---------------------------------------------------------------- context */
  R.begin = function (ctx) {
    R.ctx = { threads: [], ...ctx };
    R.listeners.clear();
    R.ovHost = null;
    R.pinMode = false;
    return R.reload();
  };
  R.end = function () { R.ctx = null; R.listeners.clear(); R.ovHost = null; };
  R.on = function (fn) { R.listeners.add(fn); return () => R.listeners.delete(fn); };
  R.reload = async function () {
    const c = R.ctx;
    if (!c) return;
    try {
      const d = await api('/api/threads?' + scopeQs(c.scope));
      if (R.ctx !== c) return;
      c.threads = d.threads || [];
      c.counts = d.counts || {};
    } catch (e) { c.threads = []; c.error = e.message; }
    for (const fn of [...R.listeners]) { try { fn(c.threads); } catch (_) {} }
  };
  /* Which subject a NEW thread on this item belongs to. One proposal on a
   * proposal page; the CHANGE itself for a change page's "Start a thread". */
  R.subjectFor = function () {
    const c = R.ctx;
    return c.subjectFor ? c.subjectFor() : { type: c.scope[0][0], id: c.scope[0][1] };
  };

  /* ⭐ STAGE 14 — THE LENS. A change page mounts these same components over N
   * members (no forked code): `ctx.members` maps an item number to its
   * {pid, head, base}, and the member section a hook is mounted in carries
   * `data-rv-number`. R.lens(node) answers "which proposal is this diff, this
   * redline, this mesh about?" — its head/base for anchors, its subject for a
   * new thread, and which threads are its own. With no members (a proposal or a
   * release page) the lens IS the context, so every stage-13 page is
   * unchanged. */
  R.lens = function (node) {
    const c = R.ctx;
    if (!c) return null;
    const host = node && node.closest ? node.closest('[data-rv-number]') : null;
    const m = host && c.members ? c.members[host.dataset.rvNumber] : null;
    if (m) return { number: host.dataset.rvNumber, head: m.head, base: m.base,
                    pid: m.pid, subject: { type: 'proposal', id: m.pid } };
    if (c.members) return { number: '', head: '', base: '', pid: 0,
                            subject: null, unplaced: true };
    return { number: c.number, head: c.head, base: c.base, pid: c.pid,
             subject: null };
  };
  /* a lens's own threads: on a change page, only that member's proposal's */
  const own = (L, ts) => (!L || !L.subject) ? (L && L.unplaced ? [] : ts)
    : ts.filter(t => t.subject && t.subject.type === 'proposal'
                     && t.subject.id === L.pid);

  /* -------------------------------------------------------------- composer */
  /* One composer for every anchor. `anchor` is what the server stores; `label`
   * is the human sentence for it ("bracket.stl · point (40.0, 10.0, 12.0)"). */
  R.composer = function ({ anchor, label, suggest = null, onDone, onCancel,
                           title = 'New thread', subject = null }) {
    const box = el('div', 'rv rv-comp');
    box.appendChild(el('div', 'lbl', title));
    if (label) box.appendChild(el('div', 'anc', label));
    const ta = el('textarea');
    ta.placeholder = anchor.kind === 'mesh'
      ? 'What should change here? ("bring this face in 2 mm", "add a Ø3.4 hole")'
      : 'What did you see?';
    box.appendChild(ta);
    const row = el('div', 'row');
    let sugIn = null;
    if (suggest) {
      const l = el('label');
      l.append(document.createTextNode('suggest '));
      sugIn = el('input');
      sugIn.type = 'text';
      sugIn.placeholder = suggest.placeholder || 'new value';
      if (suggest.value != null) sugIn.value = suggest.value;
      l.appendChild(sugIn);
      l.title = suggest.why || 'A value the author can apply as ONE commit';
      row.appendChild(l);
    }
    const bl = el('label');
    const cb = el('input'); cb.type = 'checkbox';
    bl.append(cb, document.createTextNode(' blocking'));
    bl.title = 'Hold a RELEASED approval or a promotion until this is resolved. '
      + 'An approver can still ship over it — by resolving it with a reason.';
    row.appendChild(bl);
    const go = el('button', 'btn primary tiny', 'Comment');
    const no = el('button', 'btn tiny', 'Cancel');
    row.append(go, no);
    box.appendChild(row);
    const out = el('div');
    box.appendChild(out);
    no.onclick = () => { box.remove(); if (onCancel) onCancel(); };
    go.onclick = async () => {
      const body = ta.value.trim();
      if (!body) { ta.focus(); return; }
      go.disabled = true;
      out.innerHTML = '';
      const s = subject || R.subjectFor();
      const req = { subject_type: s.type, subject_id: s.id, body, anchor,
                    blocking: cb.checked };
      // a suggestion that says what the line already says is not one
      if (sugIn && sugIn.value.trim()
          && sugIn.value !== (suggest.value == null ? '' : String(suggest.value))) {
        req.suggestion = anchor.kind === 'line' ? { text: sugIn.value }
                                                : { value: sugIn.value.trim() };
      }
      try {
        const t = await post('/api/threads', decisionBody(req));
        toast(`thread ${t.id} opened` + (t.blocking ? ' — BLOCKING' : ''));
        box.remove();
        await R.reload();
        if (onDone) onDone(t);
      } catch (e) {
        go.disabled = false;
        out.appendChild(refusalBox(e));
      }
    };
    setTimeout(() => ta.focus({ preventScroll: true }), 0);
    return box;
  };

  /* ------------------------------------------------------------ one thread */
  function anchorLine(t) {
    const a = t.anchor || {};
    const st = t.status || {};
    if (a.kind === 'line') {
      const now = st.line_now && st.line_now !== a.line ? ` (now line ${st.line_now})` : '';
      return `${a.path}:${a.line}${now} · ${a.side || 'head'} @ ${short(a.sha)}`;
    }
    if (a.kind === 'key') return `plm.toml  ${a.key} @ ${short(a.sha)}`;
    if (a.kind === 'mesh') {
      const f = (v) => (v || []).map(x => Number(x).toFixed(1)).join(', ');
      return `${a.path}${a.side === 'base' ? ' (base)' : ''} · point (${f(a.p)}) `
        + `n (${f(a.n)}) mm, model space @ ${short(a.sha)}`;
    }
    if (a.kind === 'image') return `${a.path} · region @ ${short(a.sha)}`;
    return '';
  }

  R.threadCard = function (t, opts = {}) {
    const box = el('div', 'rv rv-thr ' + t.state + (t.outdated ? ' outdated' : ''));
    box.dataset.thread = t.id;
    const hd = el('div', 'hd');
    hd.appendChild(el('span', 'tid', `T${t.id}`));
    hd.appendChild(el('span', 'rv-chip ' + (t.state === 'open' ? 'open' : 'res'),
      t.state === 'open' ? 'open' : 'resolved'));
    if (t.blocking) hd.appendChild(el('span', 'rv-chip block', 'blocking'));
    if (t.outdated) {
      const o = el('span', 'rv-chip out', 'outdated');
      o.title = (t.status && t.status.why) || 'changed since';
      hd.appendChild(o);
    }
    if (t.suggestion) hd.appendChild(el('span', 'rv-chip sug', 'suggestion'));
    if (t.advisory) {
      const c = (typeof whoChip === 'function' && whoChip('bot'))
        || el('span', 'rv-chip adv', 'bot');
      c.title = 'Raised by an agent — advisory. Bots propose, humans decide.';
      hd.appendChild(c);
    }
    if (R.ctx && R.ctx.members && t.subject) {
      // a change page shows N members' threads in one panel: say whose
      const lbl = t.subject.type === 'change' ? 'the pull request'
        : `${PLM_ROUTES.proposalLabel(t.subject.id)} ${t.subject.number || ''}`.trim();
      const on = el('span', 'rv-chip on', lbl);
      on.title = t.subject.type === 'change' ? 'On the pull request as a whole'
        : `On proposal ${PLM_ROUTES.proposalLabel(t.subject.id)} (${t.subject.number})`;
      hd.appendChild(on);
    }
    hd.appendChild(el('span', 'ttl', t.title || ''));
    box.appendChild(hd);
    if (t.anchor && t.anchor.kind !== 'subject' && !opts.inline) {
      const anc = el('div', 'anc');
      const a = el('a', null, anchorLine(t));
      a.onclick = () => R.goto(t);
      anc.appendChild(a);
      if (t.anchor.kind === 'line' && t.anchor.text != null) {
        anc.appendChild(el('span', 'q', t.anchor.text || ' '));
      }
      box.appendChild(anc);
    }
    if (t.outdated && t.status && t.status.why) {
      box.appendChild(el('div', 'why', '⚠ ' + t.status.why
        + (t.anchor.kind === 'mesh' ? ' — the pin is shown AS DRAWN, not re-found.' : '')));
    }
    (t.comments || []).forEach((c, i) => {
      // the opening comment IS the title when it is one line — not twice
      if (i === 0 && (c.body || '').trim() === (t.title || '').trim()) {
        hd.appendChild(el('span', 'by', `— ${c.actor}${c.kind === 'bot' ? ' ⚙' : ''}`));
        hd.appendChild(when(c.created_at));
        return;
      }
      const r = el('div', 'rv-cm');
      const w = el('span', 'who', c.actor || '—');
      if (c.kind === 'bot') w.textContent += ' ⚙';
      const b = el('span', 'b md');
      // a comment is markdown (bold, `code`, lists) — escaped first by mdToHtml
      b.innerHTML = typeof mdToHtml === 'function' ? mdToHtml(c.body || '')
                                                   : escapeHtml(c.body || '');
      r.append(w, b, when(c.created_at));
      box.appendChild(r);
    });
    if (t.suggestion) {
      const s = el('div', 'rv-sug');
      s.appendChild(el('b', null, 'suggested'));
      const was = t.anchor.kind === 'key'
        ? (t.anchor.value && typeof t.anchor.value === 'object'
           ? t.anchor.value.value : t.anchor.value)
        : t.anchor.text;
      if (was != null) s.appendChild(el('span', 'was', String(was)));
      s.appendChild(el('span', null, '→'));
      s.appendChild(el('span', 'now', String(t.suggestion.value ?? t.suggestion.text)));
      if (t.suggestion_commit) {
        s.appendChild(el('span', 'dim', `applied as ${short(t.suggestion_commit)}`));
      } else if (t.state === 'open' && R.ctx && R.ctx.canApply) {
        const ap = el('button', 'btn tiny', 'Apply as one commit');
        ap.title = 'Commit exactly this change on the draft branch';
        ap.onclick = () => act(box, ap, () => post(`/api/threads/${t.id}/apply`,
          decisionBody({})), (r) => toast(`applied as ${short(r.commit)}`));
        s.appendChild(ap);
      }
      box.appendChild(s);
    }
    if (t.addressed_by) {
      const ad = el('div', 'rv-addr');
      ad.append(document.createTextNode('addressed by '),
                el('span', 'sha', short(t.addressed_by)));
      if (t.addressed_by_actor) ad.append(document.createTextNode(` (${t.addressed_by_actor})`));
      if (t.state === 'open') ad.append(document.createTextNode(
        ` — awaiting ${t.author}, who raised it`));
      box.appendChild(ad);
    }
    if (t.state === 'resolved') {
      box.appendChild(el('div', 'rv-addr', `resolved by ${t.resolved_by || '—'}`
        + (t.resolution ? ` — “${t.resolution}”` : '')));
    }
    const acts = el('div', 'rv-acts');
    if (t.anchor && t.anchor.kind === 'mesh' && t.anchor.camera) {
      const sh = el('button', 'btn tiny', 'Show me what they saw');
      sh.title = 'Restore the reviewer\'s camera on the 3-D overlay';
      sh.onclick = () => R.showMesh(t);
      acts.appendChild(sh);
    }
    if (t.state === 'open') {
      const rp = el('button', 'btn tiny', 'Reply');
      const rs = el('button', 'btn tiny', 'Resolve');
      acts.append(rp, rs);
      rp.onclick = () => openReply(box, t, 'reply');
      rs.onclick = () => openReply(box, t, 'resolve');
    } else {
      const ro = el('button', 'btn tiny', 'Reopen');
      ro.onclick = () => act(box, ro, () => post(`/api/threads/${t.id}/reopen`,
        decisionBody({})));
      acts.appendChild(ro);
    }
    box.appendChild(acts);
    return box;
  };

  async function act(box, btn, fn, then) {
    btn.disabled = true;
    const old = box.querySelector('.qrefusal, .refusal');
    if (old) old.remove();
    try {
      const r = await fn();
      if (then) then(r);
      await R.reload();
    } catch (e) {
      btn.disabled = false;
      box.appendChild(refusalBox(e));
    }
  }

  function openReply(box, t, mode) {
    const prev = box.querySelector('.rv-comp');
    if (prev) prev.remove();
    const c = el('div', 'rv-comp');
    c.appendChild(el('div', 'lbl', mode === 'reply' ? 'Reply' : 'Resolve'));
    const ta = el('textarea');
    ta.placeholder = mode === 'reply' ? 'Answer the thread'
      : (t.blocking ? 'Why — required to ship over a blocking thread you did not raise'
                    : 'Why (optional)');
    c.appendChild(ta);
    const row = el('div', 'row');
    let sha = null;
    if (mode === 'resolve') {
      const l = el('label');
      l.append(document.createTextNode('addressed by '));
      sha = el('input'); sha.type = 'text'; sha.placeholder = 'commit sha';
      l.appendChild(sha);
      row.appendChild(l);
    }
    const go = el('button', 'btn primary tiny', mode === 'reply' ? 'Reply' : 'Resolve');
    const no = el('button', 'btn tiny', 'Cancel');
    row.append(go, no);
    c.appendChild(row);
    no.onclick = () => c.remove();
    go.onclick = () => act(c, go, () => mode === 'reply'
      ? post(`/api/threads/${t.id}/reply`, decisionBody({ body: ta.value.trim() }))
      : post(`/api/threads/${t.id}/resolve`, decisionBody({
          reason: ta.value.trim(), addressed_by: (sha.value || '').trim() })),
      (r) => toast(mode === 'reply' ? 'replied'
        : r.state === 'resolved' ? `T${t.id} resolved` : `T${t.id}: addressed — ${t.author} decides`));
    box.appendChild(c);
    setTimeout(() => ta.focus({ preventScroll: true }), 0);
  }

  /* ---------------------------------------------------------- the panel */
  const FILTERS = [['open', 'Open'], ['outdated', 'Outdated'],
                   ['resolved', 'Resolved'], ['all', 'All']];
  R.panel = function () {
    const card = el('div', 'doc rv');
    card.id = 'rvpanel';
    const h = el('h3', null, 'Threads');
    const right = el('span', 'right');
    h.appendChild(right);
    card.appendChild(h);
    const filt = el('div', 'rv-filt');
    card.appendChild(filt);
    const list = el('div');
    card.appendChild(list);
    let mode = 'open';
    const btns = {};
    for (const [k, label] of FILTERS) {
      const b = el('button', 'btn tiny' + (k === mode ? ' on' : ''), label);
      b.appendChild(el('span', 'n', ''));
      b.onclick = () => { mode = k; draw(); };
      btns[k] = b;
      filt.appendChild(b);
    }
    const start = el('button', 'btn tiny', '+ Start a thread');
    start.style.marginLeft = 'auto';
    start.title = (R.ctx && R.ctx.members ? 'A conversation on the whole CHANGE. '
      : 'A conversation on the whole proposal. ') + 'To point at a line, a '
      + 'plm.toml value or a spot on the part, use the diff, the redlines or '
      + 'the 3-D overlay below.';
    start.onclick = () => {
      if (card.querySelector('.rv-comp.top')) return;
      const c = R.composer({ anchor: { kind: 'subject' }, title: 'New thread',
                             onDone: () => {} });
      c.classList.add('top');
      filt.after(c);
    };
    filt.appendChild(start);
    const draw = () => {
      const all = (R.ctx && R.ctx.threads) || [];
      const n = {
        open: all.filter(t => t.state === 'open').length,
        outdated: all.filter(t => t.outdated).length,
        resolved: all.filter(t => t.state === 'resolved').length,
        all: all.length,
      };
      for (const [k] of FILTERS) {
        btns[k].classList.toggle('on', k === mode);
        btns[k].querySelector('.n').textContent = n[k];
      }
      right.innerHTML = '';
      const blk = all.filter(t => t.state === 'open' && t.blocking).length;
      right.appendChild(el('span', 'note',
        `${n.open} open` + (blk ? ` · ${blk} blocking` : '')));
      list.innerHTML = '';
      const rows = all.filter(t => mode === 'all' ? true
        : mode === 'outdated' ? t.outdated : t.state === mode);
      if (!rows.length) {
        const onRelease = R.ctx && R.ctx.scope.every(([t]) => t === 'release');
        list.appendChild(el('div', 'rv-empty', mode !== 'open' ? `No ${mode} threads.`
          : onRelease ? 'No open threads. Record what building it found — '
            + '"printed 12, all fit" — or hold the release with a BLOCKING thread.'
          : 'No open threads. Click a diff line, a redline row, or a point on '
            + 'the 3-D overlay to start one.'));
        return;
      }
      for (const t of rows) list.appendChild(R.threadCard(t));
    };
    const off = R.on(() => {
      if (!document.body.contains(card)) { off(); return; }
      draw();
    });
    draw();
    return card;
  };

  /* Jump to where a thread is anchored on this page. */
  R.goto = function (t) {
    const a = t.anchor || {};
    if (a.kind === 'mesh') { R.showMesh(t); return; }
    const sel = a.kind === 'key' ? `.rlrow[data-key="${CSS_ESC(a.key)}"]`
      : a.kind === 'line' ? `.dfile[data-file="${CSS_ESC(a.path)}"]` : null;
    const n = sel && document.querySelector(sel);
    if (n) {
      n.scrollIntoView({ block: 'center', behavior: 'smooth' });
      n.classList.add('wanted');
      setTimeout(() => n.classList.remove('wanted'), 1800);
    }
  };
  const CSS_ESC = (s) => (window.CSS && CSS.escape) ? CSS.escape(String(s))
    : String(s).replace(/["\\]/g, '\\$&');

  /* ------------------------------------------------------- diff lines */
  /* Called by WIDGETS.redline for every text diff while a review context is
   * live. `rows` are diffRows() output — [cls, text, glyph, oldNo, newNo]. */
  R.mountDiff = function (body, rows, m) {
    const c = R.lens(body);
    if (!c || !c.head) return;
    const path = m.headPath || m.basePath;
    body.classList.add('rv-on');
    const lines = body.querySelectorAll('.dline');
    const inlineFor = new Map();
    const place = () => {
      for (const n of body.querySelectorAll('.rv-inline')) n.remove();
      for (const n of lines) n.classList.remove('rv-has');
      body.style.setProperty('--rv-w', body.clientWidth + 'px');
      const mine = own(c, R.ctx ? R.ctx.threads : []).filter(t => t.anchor
        && t.anchor.kind === 'line' && !t.outdated
        && (t.anchor.path === path || t.anchor.path === m.basePath));
      inlineFor.clear();
      for (const t of mine) {
        const side = t.anchor.side || 'head';
        const ln = side === 'head' ? ((t.status && t.status.line_now) || t.anchor.line)
                                   : t.anchor.line;
        const i = rows.findIndex(r => side === 'head'
          ? (r[0] !== 'del' && r[4] === ln) : (r[0] === 'del' && r[3] === ln));
        if (i < 0 || !lines[i]) continue;
        if (!inlineFor.has(i)) inlineFor.set(i, []);
        inlineFor.get(i).push(t);
      }
      for (const [i, ts] of inlineFor) {
        lines[i].classList.add('rv-has');
        const box = el('div', 'rv-inline');
        for (const t of ts) box.appendChild(R.threadCard(t, { inline: true }));
        lines[i].after(box);
      }
    };
    lines.forEach((ln, i) => {
      const r = rows[i];
      if (!r || r[0] === 'hunk' || r[0] === 'meta') return;
      const g = ln.querySelector('.g');
      if (g && g.firstChild && g.firstChild.nodeType === 3) {
        const gx = el('span', 'gx', g.textContent); g.textContent = ''; g.appendChild(gx);
      }
      const open = (ev) => {
        if (ev && window.getSelection && String(window.getSelection()).length) return;
        if (body.querySelector('.rv-comp')) body.querySelector('.rv-comp').remove();
        const side = r[0] === 'del' ? 'base' : 'head';
        const no = side === 'base' ? r[3] : r[4];
        const anchor = { kind: 'line', path: side === 'base' ? (m.basePath || path)
          : path, line: no, side, sha: side === 'base' ? c.base : c.head };
        const comp = R.composer({
          anchor, label: `${anchor.path}:${no} (${side})`, subject: c.subject,
          suggest: side === 'head' ? { value: r[1], placeholder: 'replacement line',
            why: 'Replace this line — applied as ONE commit by the author' } : null,
          title: `Comment on line ${no}` });
        comp.style.cssText = 'position:sticky;left:0;width:'
          + Math.max(260, body.clientWidth - 2) + 'px';
        ln.after(comp);
      };
      (g || ln).addEventListener('click', (ev) => { ev.stopPropagation(); open(ev); });
      ln.addEventListener('dblclick', open);
    });
    place();
    const off = R.on(() => { if (!document.body.contains(body)) { off(); return; } place(); });
  };

  /* -------------------------------------------------------- redline rows */
  R.mountRedline = function (card) {
    if (!R.ctx || (!R.ctx.head && !R.ctx.members)) return;
    const rows = card.querySelectorAll('.rlrow[data-key]');
    const draw = () => {
      for (const n of card.querySelectorAll('.rv-inline.rl')) n.remove();
      const L = R.lens(card);       // asked at draw time: the card may not be
      for (const row of rows) {     // in the page yet when this is mounted
        const key = row.dataset.key;
        const ts = own(L, R.ctx ? R.ctx.threads : []).filter(t => t.anchor
          && t.anchor.kind === 'key' && t.anchor.key === key);
        const btn = row.querySelector('.rv-k');
        if (btn) btn.classList.toggle('has', ts.some(t => t.state === 'open'));
        if (!ts.length) continue;
        const box = el('div', 'rv-inline rl');
        for (const t of ts) box.appendChild(R.threadCard(t, { inline: true }));
        row.after(box);
      }
    };
    for (const row of rows) {
      const k = row.querySelector('.k');
      const b = el('button', 'rv-k', '💬');
      b.title = `Comment on ${row.dataset.key}`;
      b.setAttribute('aria-label', b.title);
      b.onclick = () => {
        const prev = card.querySelector('.rv-comp');
        if (prev) prev.remove();
        const key = row.dataset.key;
        const sug = /^attributes\.[^.]+$|^bom\[.*\]\.(qty|unit|find|notes)$|^(process|summary|verification|name)$/.test(key);
        const comp = R.composer({ anchor: { kind: 'key', key },
          subject: (R.lens(card) || {}).subject,
          label: `plm.toml  ${key}`, title: 'Comment on this value',
          suggest: sug ? { value: row.dataset.now || '', placeholder: 'new value',
            why: 'The author applies it as ONE commit on the draft' } : null });
        row.after(comp);
      };
      k.appendChild(b);
    }
    draw();
    queueMicrotask(draw);
    const off = R.on(() => { if (!document.body.contains(card)) { off(); return; } draw(); });
  };

  /* ------------------------------------------------------ the 3-D overlay */
  /* The pin tool rides the overlay bar. ONE WebGL context, and it moves (vDock):
   * this never builds a scene of its own — it asks the overlay host to open,
   * then draws into whatever `V` is showing. */
  R.meshBar = function (bar, host, sides, mountAfter) {
    if (!R.ctx || (!R.ctx.head && !R.ctx.members)) return;
    R.ovHost = host;
    host.sides = sides;
    queueMicrotask(() => {         // a change page: remember whose overlay
      const sec = bar.closest && bar.closest('[data-rv-pid]');
      if (sec) sec.rvOvHost = host;
    });
    const b = el('button', 'btn tiny rv-pin', '📍 Note a point');
    b.title = 'Click (or tap) the part to leave a note anchored to that point, '
      + 'in model space, with your camera — so the author sees what you saw.';
    const hint = el('div', 'rv-pinhint');
    hint.style.display = 'none';
    const list = el('div', 'rv-pinlist');
    const compHost = el('div');
    const set = (on) => {
      R.pinMode = on;
      if (typeof V !== 'undefined') V.pickPrefer = on ? 'head' : null;
      b.classList.toggle('on', on);
      hint.style.display = on ? '' : 'none';
      hint.textContent = 'Tap the part where the change should be — Esc or the '
        + 'button again to stop.';
      if (typeof V !== 'undefined' && V.renderer) V.renderer.domElement.style.cursor = on ? 'crosshair' : '';
    };
    b.onclick = async () => {
      R.active = R.lens(bar);
      if (OVL !== host) await host.open();
      set(!R.pinMode);
    };
    bar.insertBefore(b, bar.firstChild);
    mountAfter.after(hint);
    hint.after(compHost);
    compHost.after(list);
    R.onPick = (pick) => {
      set(false);
      const part = pick.part || {};
      const side = part.side || 'head';
      const P = pick.local, N = pick.normal || { x: 0, y: 0, z: 1 };
      V.group.updateMatrixWorld(true);
      const cp = V.group.worldToLocal(V.camera.position.clone());
      const ct = V.group.worldToLocal(V.controls.target.clone());
      const r3 = (v) => [v.x, v.y, v.z].map(x => +x.toFixed(3));
      const L = R.lens(bar) || R.ctx;
      const anchor = { kind: 'mesh', path: part.path, side,
        sha: side === 'base' ? L.base : L.head,
        p: r3(P), n: r3(N), tri: pick.tri, snapped: !!pick.snapped,
        explode: V.explode || 0,
        camera: { position: r3(cp), target: r3(ct), up: [0, 0, 1], fov: V.camera.fov } };
      compHost.innerHTML = '';
      R.drawPins({ x: P.x, y: P.y, z: P.z, part: pick.part });
      compHost.appendChild(R.composer({
        anchor, title: 'Note on the part', subject: L.subject,
        label: `${Path_basename(part.path)} (${side}) · point (${r3(P).join(', ')}) mm, model space`,
        onDone: () => { compHost.innerHTML = ''; },
        onCancel: () => R.drawPins() }));
      compHost.scrollIntoView({ block: 'nearest' });
    };
    const drawList = () => {
      list.innerHTML = '';
      const ts = own(R.lens(bar), R.ctx ? R.ctx.threads : [])
        .filter(t => t.anchor && t.anchor.kind === 'mesh');
      if (!ts.length) return;
      list.appendChild(el('span', 'dim', 'pins:'));
      for (const t of ts) {
        const bt = el('button', 'btn tiny' + (t.outdated ? ' out' : ''),
          `T${t.id} ${t.state === 'open' ? '●' : '✓'} ${String(t.title || '').slice(0, 28)}`);
        bt.title = 'Show me what they saw';
        bt.onclick = () => R.showMesh(t);
        list.appendChild(bt);
      }
      if (OVL === host) R.drawPins();
    };
    const off = R.on(() => { if (!document.body.contains(bar)) { off(); return; } drawList(); });
    drawList();
  };

  /* The pins: the measure tool's own mark (vDot), in redline — or amber when
   * the mesh has changed since (drawn AS DRAWN, never re-projected) — plus a
   * numbered callout. Parented to the body's mesh, so explode, solo and the
   * Z-up group transform carry them for free. */
  R.drawPins = function (draft) {
    if (typeof V === 'undefined' || !V.parts || !V.parts.length) return;
    R.clearPins();
    V.rvPins = [];
    // on a change page, the pins of the member whose overlay is open
    const L = R.ctx && R.ctx.members ? (R.active || (R.ovHost && R.lens(R.ovHost.mount))) : null;
    const ts = (L ? own(L, R.ctx.threads) : (R.ctx ? R.ctx.threads : []))
      .filter(t => t.anchor && t.anchor.kind === 'mesh');
    const put = (part, p, hex, label) => {
      const dot = vDot(hex);
      dot.position.set(p[0], p[1], p[2]);
      part.mesh.add(dot);
      V.rvPins.push(dot);
      if (label) {
        const s = vSprite(label, { color: hex, h: 0.022, cx: -0.5, cy: -0.9,
                                    renderOrder: 23 });
        s.position.set(p[0], p[1], p[2]);
        part.mesh.add(s);
        V.rvPins.push(s);
      }
    };
    for (const t of ts) {
      const a = t.anchor;
      const part = V.parts.find(q => q.path === a.path && (!q.side || q.side === (a.side || 'head')))
        || V.parts.find(q => q.path === a.path);
      if (!part) continue;
      put(part, a.p, t.outdated ? AMBER : (t.state === 'open' ? RED : '#8d939b'),
          `T${t.id}${t.outdated ? '?' : ''}`);
    }
    if (draft && draft.part) put(draft.part, [draft.x, draft.y, draft.z], RED, 'new');
  };
  R.clearPins = function () {
    for (const o of (typeof V !== 'undefined' && V.rvPins) || []) vKill(o);
    if (typeof V !== 'undefined') V.rvPins = [];
  };

  /* "Show me what they saw": open the overlay if another row has the room,
   * then put the camera where the reviewer's was. Stored in MODEL space and
   * pushed back through V.group, so it frames the same thing even if this
   * revision's union bbox (and so the display group) moved. */
  R.showMesh = async function (t) {
    // a change page has one overlay per member section: the thread's own
    const sec = R.ctx && R.ctx.members && t.subject && document.querySelector(
      `[data-rv-pid="${t.subject.id}"]`);
    if (sec && sec.rvOvHost) { R.ovHost = sec.rvOvHost; R.active = R.lens(sec); }
    else if (sec && sec.tagName === 'DETAILS' && !sec.open) {
      sec.open = true; toast('opened its section — press show again once the overlay is drawn');
      return;
    }
    const host = R.ovHost;
    if (!host) { toast('no 3-D overlay on this page'); return; }
    host.mount.scrollIntoView({ block: 'center', behavior: 'smooth' });
    if (OVL !== host || !V.parts.length) await host.open();
    const cam = t.anchor.camera;
    if (!cam || !V.group) return;
    V.group.updateMatrixWorld(true);
    const w = (a) => V.group.localToWorld(new THREE.Vector3(a[0], a[1], a[2]));
    if (t.anchor.explode != null && V.explode !== t.anchor.explode) {
      V.explode = Number(t.anchor.explode) || 0;
      vApplyExplode();
    }
    V.camera.position.copy(w(cam.position));
    V.controls.target.copy(w(cam.target));
    if (cam.fov) { V.camera.fov = cam.fov; V.camera.updateProjectionMatrix(); }
    V.controls.update();
    R.drawPins();
    toast(`T${t.id}: ${t.author}'s view` + (t.outdated ? ' — the mesh has changed since (amber pin)' : ''));
  };

  window.REVIEW_UI = R;
})();
