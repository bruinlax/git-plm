/* plm — part registry SPA.
 *
 * Hash-routed pages: the dashboard (products, approval queue, activity), the
 * registry table, the approval queue on its own (/approvals), and an item's
 * document page (title block, revision ledger, files/BOM/where-used/diff).
 * The 3D viewer is a dark overlay opened from a revision — geometry is the
 * appendix, the registry is the book.
 */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) {
    let d = '';
    try { d = (await r.json()).detail; } catch {}
    throw new Error(d || `${r.status} ${r.statusText}`);
  }
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
}

async function post(path, body, method = 'POST') {
  const r = await fetch(path, { method,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    // ⭐ KEEP THE STRUCTURED DETAIL ON THE ERROR. A refusal is often more than a
    // sentence — `ambiguous_artifact` carries the list of files you may choose
    // between — and flattening it to `r.statusText` threw the actionable half
    // away, leaving the caller with "Conflict" and nothing to do about it.
    const detail = d && d.detail;
    const err = new Error(
      typeof detail === 'string' ? detail
        : (detail && detail.error) || r.statusText);
    err.detail = detail;
    err.status = r.status;
    throw err;
  }
  return d;
}

// -- classification -------------------------------------------------
// The role registry is fetched ONCE and cached: it is the server's vocabulary,
// and a picker that invents its own options is how three partial copies of the
// role list came to exist in the first place.
let ROLES = null;
async function roleRegistry() {
  if (!ROLES) {
    try { ROLES = await api('/api/roles'); } catch { ROLES = []; }
  }
  return ROLES;
}

// Declare what a file is for. Candidates first — the server already worked out
// which roles these bytes could plausibly be — then the rest, so the common case
// is a three-item choice rather than a scroll through thirteen.
async function declareRole(f, anchor, done) {
  const reg = await roleRegistry();
  const cand = new Set(f.role_candidates || []);
  const pick = el('div', 'rolepick');
  pick.appendChild(el('div', 'rolepickh',
    `What is ${fileName(f)}? Measured format: ${f.media || 'unknown'}`));
  const offer = reg.filter(r => r.choose_for_new_files)
    .sort((a, b) => (cand.has(b.role) ? 1 : 0) - (cand.has(a.role) ? 1 : 0));
  for (const r of offer) {
    const b = el('button', 'rolebtn' + (cand.has(r.role) ? ' likely' : '')
      + (r.role === f.role ? ' current' : ''), r.label);
    b.title = r.help;
    b.onclick = async () => {
      try {
        const res = await post(`/api/files/${f.id}/role`,
                               { role: r.role, actor: actorName() }, 'PUT');
        f.role = res.role; f.role_label = r.label;
        f.role_confirmed = true; f.role_candidates = [];
        f.role_warning = res.warning || '';
        pick.remove();
        if (res.warning) toast(res.warning);
        // ⭐ RE-RENDER RATHER THAN PATCH THE CHIP. Declaring a role changes what
        // the file can DO — an unclassified STL becomes printable the moment
        // somebody says it is a mesh — so the row's whole action list is stale,
        // not just its label. The server recomputes it; the caller redraws.
        if (done) done();
      } catch (e) { toast(String(e.message || e)); }
    };
    pick.appendChild(b);
  }
  const cancel = el('button', 'rolebtn cancel', 'Cancel');
  cancel.onclick = () => pick.remove();
  pick.appendChild(cancel);
  const old = document.querySelector('.rolepick');
  if (old) old.remove();
  (anchor || document.body).appendChild(pick);
}

// Tags, as chips. A CAVEAT — an unresolved risk — is styled to be seen: these
// are the facts that used to be shouted in prose because there was nowhere
// structured to put them.
function tagChips(item, list, scope) {
  const wrap = el('div', 'tagrow');
  for (const t of list || []) {
    const c = el('span', 'tagchip' + (t.caveat ? ' caveat' : '')
      + (t.registered ? '' : ' unreg'), t.label);
    c.title = (t.help || 'Not in the tag registry — accepted, and a candidate '
               + 'for it if it turns out to be a fact people keep recording.')
      + (t.note ? `\n\n${t.note}` : '')
      + (t.actor ? `\n— ${t.actor}` : '');
    wrap.appendChild(c);
  }
  return wrap;
}

// Fallback actor for editor actions on instances with no sign-in: the
// instance's admin, as /auth/whoami reports it (`fallback_actor`, PLM_ADMIN).
// Once the server knows who we are (OIDC), use that name instead — see
// actorName().
/** Rows per page in the dashboard feed. One definition — the initial fetch,
 *  the pager's step and the button's wording all read it. */
const FEED_PAGE = 24;
let ACTOR = 'admin';
const actorName = () => (ME && ME.verified && ME.actor) || ACTOR;
let toastTimer = null;
function toast(msg, link) {
  const t = $('toast');
  t.textContent = msg;
  // ⭐ An optional "go and look" link. "It is on the print queue as q_4627b8e3"
  // is a claim you then have to go and verify by hand, in another tab, from an
  // id you cannot remember. The integration already knows its own landing page.
  if (link) {
    t.append(' ');
    const a = document.createElement('a');
    a.href = link; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = 'view →';
    a.style.color = 'inherit';
    a.style.textDecoration = 'underline';
    t.appendChild(a);
    // Longer, because a link nobody has time to click is not a link.
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('on'), 12000);
    t.classList.add('on');
    return;
  }
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), 4200);
}

/* ------------------------------------------------- handing a part downstream
 * ⭐ A REFUSAL YOU CAN ANSWER. the shop-floor service refuses a revision carrying more than
 * one printable STL — a jig and its test coupon, say — because guessing which
 * one you meant is exactly the kind of decision an integration must not make on
 * your behalf. It lists the candidates in the refusal; this turns that list into
 * a choice and sends the answer back as `path`.
 *
 * ⚠️ The PLM still does not decide what "printable" means. It relays a question
 * and relays the answer — the boundary stays where it was.
 */
async function sendToIntegration(ig, item, rev, path) {
  const body = { number: item.number, version: rev.version, actor: actorName() };
  if (path) body.path = path;
  try {
    const out = await post(`/api/integrations/${encodeURIComponent(ig.name)}/invoke`, body);
    const said = out && out.result && (out.result.message || out.result.detail);
    toast(said || `${ig.action_label}: sent ${item.number} v${rev.version}`, out && out.link);
  } catch (e) {
    const info = e && e.detail && typeof e.detail === 'object' ? e.detail : null;
    const candidates = info && info.details && info.details.candidates;
    if (info && info.code === 'ambiguous_artifact' && Array.isArray(candidates) && candidates.length) {
      pickArtifact(ig, item, rev, candidates);
      return;
    }
    toast((info && info.error) || e.message);
  }
}

/** The picker the `ambiguous_artifact` refusal asks for. */
function pickArtifact(ig, item, rev, candidates) {
  const back = el('div', 'modal-back');
  const box = el('div', 'modal');
  box.appendChild(el('h3', null, 'Which file should be printed?'));
  box.appendChild(el('div', 'note',
    `${item.number} v${rev.version} carries ${candidates.length} printable STLs. ` +
    'Pick the one you want; the others are left alone.'));
  const list = el('div', 'pad');
  for (const path of candidates) {
    const row = el('button', 'btn wide', path.split('/').pop());
    row.title = path;
    row.onclick = () => { back.remove(); sendToIntegration(ig, item, rev, path); };
    list.appendChild(row);
  }
  box.appendChild(list);
  const cancel = el('button', 'btn', 'Cancel');
  cancel.onclick = () => back.remove();
  box.appendChild(cancel);
  back.appendChild(box);
  back.onclick = (ev) => { if (ev.target === back) back.remove(); };
  document.body.appendChild(back);
}

let integrationsCache = null;
async function getIntegrations() {
  if (!integrationsCache) integrationsCache = api('/api/integrations').catch(() => []);
  return integrationsCache;
}

/* ------------------------------------------------------- who am I, really
 * Approval decisions are statements about who accepted the risk, so the
 * server establishes the identity from the OIDC session and ignores any
 * `actor` in the body. The client's job is only to SHOW who that is — and to
 * keep sending a username on instances where OIDC is not configured at all
 * (the sandbox, local dev), which still fall back to a trusted name.
 */
let ME = null;
async function whoami(force = false) {
  if (ME && !force) return ME;
  ME = await api('/auth/whoami').catch(() => ({ verified: false, oidc: {} }));
  if (ME && ME.fallback_actor) ACTOR = ME.fallback_actor;
  const box = $('whoami');
  if (box) {
    box.innerHTML = '';
    if (ME.verified) {
      // Signed in: the sign-in shell's loop guard has done its job.
      try { sessionStorage.removeItem('plm.signin_bounce'); } catch (e) { /* private mode */ }
      const u = ME.user || {};
      box.append(document.createTextNode('signed in as '),
        el('b', null, u.display || ME.actor));
      if (u.roles && !u.roles.split(',').includes('approver'))
        box.append(document.createTextNode(' · not an approver'));
    } else if (ME.oidc && ME.oidc.configured) {
      // Back to EXACTLY this page after sign-in: path + query + #route. The
      // href is recomputed when it is about to be used, because the hash route
      // changes under a link rendered once (plm/identity.py safe_next).
      const a = el('a', null, 'Sign in');
      const here = () => '/auth/login?next=' +
        encodeURIComponent(location.pathname + location.search + location.hash);
      a.href = here();
      for (const ev of ['click', 'auxclick', 'mouseenter', 'focus'])
        a.addEventListener(ev, () => { a.href = here(); });
      box.append(a);
    } else {
      box.append(document.createTextNode('unauthenticated (no OIDC)'));
    }
  }
  return ME;
}

// Body for a decide/withdraw call: nothing when the server knows us, the
// legacy trusted username only where there is no OIDC to know us by.
function decisionBody(extra = {}) {
  const legacy = ME && !ME.verified && !(ME.oidc && ME.oidc.configured);
  return legacy ? { actor: ACTOR, ...extra } : { ...extra };
}

/* ⭐ WHO DECIDED, AS A CHIP (stage 3c). Three different sentences about
 * accountability, and the ledger is only worth having if they are told apart:
 *
 *   AUTO   plm's POLICY merged it. Nobody looked, and the rule Geoff wrote says
 *          nobody needed to at this maturity.
 *   BOT    an agent decided, because the project's policy admits agents there.
 *          A person could have; one did not.
 *   (none) a human decided, which is the unremarkable case and gets no chip.
 *
 * Returns null for the human case ON PURPOSE: a badge on the normal path is
 * noise, and noise is what stops people reading badges. */
function whoChip(kind, { title = '' } = {}) {
  if (kind === 'policy') {
    const s = el('span', 'autochip', 'AUTO');
    s.title = title || 'Auto-merged by policy — no human reviewed this '
      + 'revision. plm/policy.py decides which maturities merge without a '
      + 'person; the approval row and the tag annotation both say so.';
    return s;
  }
  if (kind === 'bot') {
    const s = el('span', 'botchip', 'BOT');
    s.title = title || 'Approved by an agent. The project\'s approval policy '
      + 'admits agents at this maturity — no human reviewed it.';
    return s;
  }
  return null;
}

/* ------------------------------------------------------ approval queue
 * One builder, used by the dashboard and the dedicated /approvals view. It
 * ALWAYS returns a section — an empty queue says so rather than disappearing,
 * so the release gate is a permanent fixture of the page.
 */
async function approvalQueue(onChange, heading = 'Awaiting approval',
                             { legacyOnly = false } = {}) {
  await whoami();
  DRAFTS_CACHE.clear();     // a decision moves branches; re-ask every render
  let approvals = await api('/api/approvals?pending=1').catch(() => []);
  if (legacyOnly) {
    // STAGE 14: every review in a git-first `write` project is a CHANGE and is
    // decided on the change page; what is left here are the rounds of
    // projects outside `write`, which have no change (master's behaviour).
    approvals = approvals.filter(a => !(a.proposal && a.proposal.change_id));
    if (!approvals.length) return el('span');
    heading = 'Approvals outside git-first write mode';
  } else setApprovalCount(approvals.length);
  const q = el('div', 'doc');
  const h = el('h3', null, heading + (approvals.length ? ` · ${approvals.length}` : ''));
  q.appendChild(h);
  if (!approvals.length) {
    q.appendChild(el('div', 'empty',
      'Nothing awaiting approval. Submitted DRAFTs land here — a revision becomes '
      + 'a released record only when a human approves it.'));
    return q;
  }
  for (const a of approvals) {
    const row = el('div', 'feedrow');
    const tb = el('div', 'thumbbox');
    tb.appendChild(thumbImg(a.thumb,
      () => { tb.innerHTML = ''; tb.appendChild(el('span', 'noimg', 'REV')); }));
    const who = el('div', 'who');
    const numA = el('a', 'num', a.number);
    const ph = a.proposal ? pullPath(a.number, a.proposal.id) : roundPath(a.number, a.id);
    numA.href = ph;
    who.append(numA, el('span', 'nm', `v${a.version} → ${a.target_state}`));
    // STAGE 13: the proposal's number and its open threads, on the row — the
    // queue is where "impossible to approve without seeing them" is decided.
    if (a.proposal) {
      const tc = a.proposal.threads || {};
      const pb = el('span', 'nm', PLM_ROUTES.proposalLabel(a.proposal.id));
      who.appendChild(pb);
      if (tc.open) who.appendChild(el('span', 'thrbadge open'
        + (tc.blocking ? ' blocks' : ''), `💬 ${tc.open} open`
        + (tc.blocking ? ` · ${tc.blocking} blocking` : '')));
    }
    const what = el('div', 'what',
      (a.note || a.summary || '—') + `  ·  requested by ${a.requested_by}`);
    // How big is it? The queue's first question, answered without opening it.
    // One `/drafts` call per item (cached), because that route already knows
    // every branch's stats and which approval is open on it.
    const size = el('div', 'qsize');
    size.appendChild(el('span', 'dim', 'sizing…'));
    what.appendChild(size);
    (a.git_mode === 'off' ? Promise.resolve(null)
      : draftStatsFor(a.number, a.id)).then(d => {
      size.innerHTML = '';
      if (!d || !d.stats) { size.remove(); return; }
      const s = d.stats;
      size.append(el('span', null, `${s.files_changed} file`
        + `${s.files_changed === 1 ? '' : 's'}`));
      const n = el('span', 'n');
      n.append(el('span', 'plus', `+${s.additions}`),
               document.createTextNode(' '),
               el('span', 'minus', `−${s.deletions}`));
      size.appendChild(n);
      if (s.pins_changed) {
        size.appendChild(el('span', 'pinbump',
          `${s.pins_changed} pin bump${s.pins_changed === 1 ? '' : 's'}`));
      }
      if (d.ahead != null && d.counted_against) {
        size.appendChild(el('span', 'dim',
          `${d.ahead} ahead of ${d.counted_against}`));
      }
      if (window.CHECKS) CHECKS.rollupInto(size.appendChild(el('span')), a.number, d.commit, { target: (a.target_state || '').toLowerCase(), href: roundPath(a.number, a.id) });  // STAGE 6 HOOK (web/checks.js)
    }).catch(() => size.remove());
    // Comments on the SUBMISSION (plm/context.py): review remarks live with the
    // thing they are about, not on the part. Shipped inline — the queue is short.
    if (a.comments) {
      const cl = el('div', 'dim');
      cl.style.cssText = 'font-size:11.5px;margin-top:3px;white-space:pre-wrap';
      cl.textContent = `💬 ${a.comments} comment(s): `
        + (a.context || []).map(c => `${c.actor || '—'}: ${c.body}`).join('\n');
      what.appendChild(cl);
    }
    const meta = el('div', 'meta');
    // The row's own action is REVIEW, not decide. Approving is a decision about
    // bytes, and the page that shows you the bytes is one click away — the
    // quick verdict stays for the case where you already looked.
    const rev = el('a', 'btn primary', 'Review →');
    rev.href = ph;
    rev.title = 'The proposal: the diff, the release gate, the timeline, and '
      + 'the decision';
    const ok = el('button', 'btn', 'Approve');
    const no = el('button', 'btn', 'Reject');
    const wd = el('button', 'btn', 'Withdraw');
    wd.title = 'Pull it back to DRAFT for more work (submitter or approver)';
    const refused = el('div', 'qrefusal');
    const decide = async (decision) => {
      refused.innerHTML = '';
      try {
        const r = await post(`/api/approvals/${a.id}/decide`, decisionBody({ decision }));
        toast(`${a.number} v${a.version}: ${decision} → ${r.state}`
          + (r.git && r.git.tag ? ` · tagged ${r.git.tag}` : ''));
        onChange();
      } catch (e) {
        // A 409 from the write path is a paragraph with the fix in it. It is
        // rendered here in full rather than truncated into a toast.
        refused.appendChild(refusalBox(e));
      }
    };
    ok.onclick = (e) => { e.stopPropagation(); decide('approved'); };
    no.onclick = (e) => { e.stopPropagation(); decide('rejected'); };
    wd.onclick = async (e) => {
      e.stopPropagation();
      const note = prompt('Withdraw this submission — note for the record (optional):') ?? null;
      if (note === null) return;
      refused.innerHTML = '';
      try {
        await post(`/api/approvals/${a.id}/withdraw`, decisionBody({ note }));
        toast(`${a.number} v${a.version} back to DRAFT`);
        onChange();
      } catch (e2) { refused.appendChild(refusalBox(e2)); }
    };
    const btns = el('span'); btns.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end';
    btns.append(rev, ok, no, wd);
    meta.appendChild(btns);
    row.append(tb, who, what, meta);
    row.onclick = (e) => {
      if (e.target.closest('button') || e.target.closest('a')) return;
      navigate(roundPath(a.number, a.id));
    };
    q.appendChild(row);
    q.appendChild(refused);
  }
  return q;
}

/* ------------------------------------------------------ the release queue
 * The OTHER half of the queue (§2.1b). A proposal asks "is this change right?";
 * a release asks "is this revision ready to build against?" — and the two sit
 * side by side because they are the same person's inbox, not two systems.
 *
 * A release row never gets a one-click Approve here. A proposal's quick verdict
 * is defensible: you looked at the diff yesterday. A promotion is a CHECKLIST
 * somebody attests line by line, and a button that silently attested four lines
 * from a list view would make the workflow decorative. Open it. */
async function releaseQueue(onChange) {
  const rows = await api('/api/releases?pending=1').catch(() => []);
  const q = el('div', 'doc');
  const h = el('h3', null, 'Promotions awaiting a decision'
    + (rows.length ? ` · ${rows.length}` : ''));
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    'a release changes no files — it promotes a revision that already exists'));
  q.appendChild(h);
  if (!rows.length) {
    q.appendChild(el('div', 'empty',
      'No promotions pending. A prototype that has been built and proven is '
      + 'promoted from its Tags tab — no new version, no file change.'));
    return q;
  }
  for (const r of rows) {
    const row = el('div', 'feedrow relq');
    const tb = el('div', 'thumbbox');
    tb.appendChild(el('span', 'noimg', '↑'));
    const who = el('div', 'who');
    const numA = el('a', 'num', r.number);
    numA.href = relPath(r.number, r.id, r.tag);
    who.append(numA, el('span', 'nm',
      `v${r.version} · ${r.from_maturity || '—'} → ${r.to_maturity}`));
    const what = el('div', 'what');
    what.textContent = (r.item_name || '') + `  ·  requested by ${r.requested_by}`;
    const p = r.progress || {};
    const facts = el('div', 'qsize');
    facts.append(el('span', null,
      `${p.approvals || 0}/${p.approvals_required || 0} approval`
      + `${(p.approvals_required || 0) === 1 ? '' : 's'}`));
    facts.append(el('span', null,
      `${p.checklist_done || 0}/${p.checklist_total || 0} checklist`));
    if (p.blockers) {
      facts.appendChild(el('span', 'pinwarn',
        `${p.blockers} child pin${p.blockers === 1 ? '' : 's'} not released`));
    } else if (p.ready) {
      facts.appendChild(el('span', 'latestchip', 'ready'));
    }
    what.appendChild(facts);
    const meta = el('div', 'meta');
    // "Review →" on a proposal row, "Promotion →" here — short enough not to
    // wrap into three lines at 430 px, which is where these get read.
    const open = el('a', 'btn primary', 'Promotion →');
    open.href = relPath(r.number, r.id, r.tag);
    open.title = 'The workflow, the checklist, the pin closure and the decision';
    meta.appendChild(open);
    row.append(tb, who, what, meta);
    row.onclick = (e) => {
      if (e.target.closest('button') || e.target.closest('a')) return;
      navigate(relPath(r.number, r.id, r.tag));
    };
    q.appendChild(row);
  }
  return q;
}

/* "Auto-merged today" — the quiet, reviewable list (§2).
 *
 * Policy merging prototypes is the right default and it is also the thing that
 * could hide a bad change. So every auto-merge stays VISIBLE for a day, on the
 * page the approver already opens, with the compare link right there. Nobody
 * has to act on it; the point is that it cannot happen unseen. */
async function autoMergedCard(hours = 24, show = 15) {
  const rows = await api('/api/approvals?pending=0&limit=200').catch(() => []);
  const since = Date.now() / 1000 - hours * 3600;
  const all = rows.filter(a => a.decision === 'approved'
    && (a.approver_kind === 'policy' || a.approver_kind === 'bot')
    && (a.decided_at || 0) >= since);
  // Newest first, and capped: this is a list you SCAN, and a hundred rows of it
  // is a list nobody scans. The count in the header is the honest total.
  const recent = all.slice(0, show);
  const card = el('div', 'doc');
  const h = el('h3', null, 'Merged without a human, last 24 h');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    all.length ? `${all.length} · nothing to do — this is the record`
               : 'none'));
  card.appendChild(h);
  if (!recent.length) {
    card.appendChild(el('div', 'empty',
      'Nothing merged by policy in the last day. Prototype revisions merge on '
      + 'submit when they validate; they land here so they are reviewable '
      + 'after the fact.'));
    return card;
  }
  for (const a of recent) {
    const row = el('div', 'histrow');
    const left = el('div', 'who');
    const lk = el('a', 'num', `${a.number} v${a.version}`);
    lk.href = roundPath(a.number, a.id);
    left.append(lk, stamp(a.state));
    const chip = whoChip(a.approver_kind);
    if (chip) left.appendChild(chip);
    row.appendChild(left);
    const mid = el('div', 'what');
    mid.textContent = a.summary || a.note || '—';
    if (a.approver_kind === 'bot') {
      mid.appendChild(el('div', 'tr', `approved by ${a.approver}`));
    }
    row.appendChild(mid);
    const rt = el('div', 'rt');
    rt.appendChild(when(a.decided_at));
    const b = el('a', 'btn tiny', 'what changed');
    b.href = roundPath(a.number, a.id);
    rt.appendChild(b);
    row.appendChild(rt);
    card.appendChild(row);
  }
  if (all.length > recent.length) {
    const more = el('div', 'pad note');
    more.textContent = `…and ${all.length - recent.length} more in the last `
      + `${hours} h. They are in the ledger and in each item's History, `
      + 'stamped AUTO or BOT.';
    card.appendChild(more);
  }
  return card;
}

/* `/api/repo/<n>/drafts`, once per item per page. The queue asks the same
 * question of every row from the same item, and the route already computes
 * every branch's stats in one pass. */
const DRAFTS_CACHE = new Map();
async function draftStatsFor(number, approvalId) {
  if (!DRAFTS_CACHE.has(number)) {
    DRAFTS_CACHE.set(number, repoGet(number, 'drafts').catch(() => null));
  }
  const out = await DRAFTS_CACHE.get(number);
  if (!out) return null;
  return (out.drafts || []).find(
    d => d.proposal && d.proposal.id === approvalId) || null;
}

function setApprovalCount(n) {
  const b = $('apprCount');
  if (!b) return;
  b.textContent = n || '';
  b.classList.toggle('hide', !n);
}

// Cheap standing poll so the masthead count is right on any page.
async function refreshApprovalCount() {
  // stage 14: what is waiting on you is changes + release sets (+ the rounds
  // of projects outside `write`); an instance without them counts rounds
  const w = await api('/api/waiting').catch(() => null);
  const a = await api('/api/approvals?pending=1').catch(() => null);
  if (w && a) setApprovalCount((w.waiting || []).length
    + a.filter(x => !(x.proposal && x.proposal.change_id)).length);
  else if (a) setApprovalCount(a.length);
}

async function renderApprovals() {
  const page = $('page');
  page.innerHTML = '';
  // ⭐ STAGE 14 HOOK (web/changes.js): the queue lists CHANGES and RELEASE
  // SETS — one kind of thing per decision — not loose proposals. Rounds of
  // projects outside `write` (no change) and the quiet AUTO record follow.
  if (window.CHANGES_UI && await CHANGES_UI.approvalsPage(page)) {
    page.appendChild(await approvalQueue(renderApprovals, 'Approval queue',
                                         { legacyOnly: true }));
    page.appendChild(await autoMergedCard());
    return;
  }
  // Two objects, one inbox (§2.1b): proposals, then promotions, then the quiet
  // record of what merged with nobody looking.
  page.appendChild(await approvalQueue(renderApprovals, 'Approval queue'));
  page.appendChild(await releaseQueue(renderApprovals));
  page.appendChild(await autoMergedCard());
}

// Where a file's bytes live is the server's business (plm/storage.py):
// rows with a non-vault backend resolve by id, vault rows by path.
// `raw_url` is the explicit escape hatch: a file that came out of a git tree
// (or an external file via /api/repo/…/external) already knows its own URL and has no
// file row behind it. Everything else still resolves through /api/blob.
/* ⛔ M6 — ONE safeHref(), AND EVERY EXTERNAL LINK GOES THROUGH IT.
 *
 * `sources.url` comes verbatim from `[[suppliers]] url = …` in a `<NUMBER>.toml`
 * in `catalog.git`, with no scheme check at write or at render, and the anchor
 * TEXT (`s.supplier`) is attacker-controlled too — so the bait was free:
 * `url = "javascript:fetch('/api/…')"`, supplier `"Datasheet (PDF)"`, one click
 * on the Sourcing table. `el()` already makes the TEXT a non-sink
 * (`textContent`); this makes the HREF one.
 *
 * Allowed: http:, https:, mailto:, and a same-page `#` route. Anything else —
 * `javascript:`, `data:`, `vbscript:`, a scheme nobody has heard of — is
 * dropped, and the caller renders the URL as TEXT so the information is not
 * lost and the click is.
 */
const SAFE_SCHEME = /^(https?:|mailto:)/i;
function safeHref(raw) {
  const u = String(raw ?? '').trim();
  if (!u) return '';
  if (u.startsWith('#') || u.startsWith('/')) return u;   // our own routes
  /* eslint-disable-next-line no-control-regex */
  const flat = u.replace(/[\u0000-\u0020]/g, '');        // \tj\navascript:
  return SAFE_SCHEME.test(flat) ? u : '';
}

/* An <a> for a URL plm did not write. A refused scheme becomes plain text with
 * the reason in the tooltip — never a silent drop, never a live link. */
function extLink(url, text, cls) {
  const href = safeHref(url);
  const label = String(text || url || 'link');
  if (!href) {
    const span = el('span', cls || null, label);
    span.title = `plm will not link to ${String(url).slice(0, 80)} — only `
      + 'http(s) and mailto are followed.';
    return span;
  }
  const a = el('a', cls || null, label);
  a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer';
  return a;
}

const blobUrl = (f) => f.raw_url ? f.raw_url
  : (f.backend && f.backend !== 'vault' && f.id)
    ? '/api/blob?id=' + f.id
    : '/api/blob?path=' + encodeURIComponent(f.path);
const fmtSize = (n) => n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + ' MB'
  : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';
/* ------------------------------------------------------------------- when
 * ⚠️ LOCAL TIME, NEVER UTC. `fmtDate` was `toISOString().slice(0, 10)`, which is
 * UTC — so a revision checked in at 18:30 Pacific rendered as the NEXT DAY.
 * Every evening's work was dated tomorrow, on the dashboard, the registry table
 * and the revision list. That is not a formatting preference, it is a wrong fact
 * about when something changed.
 *
 * `en-CA` because it yields YYYY-MM-DD — ISO-shaped, sorts correctly by eye, and
 * unambiguous for a controlled record — while still resolving in the VIEWER's
 * zone. `toISOString()` looks like the same string and is not.
 *
 * Three formatters, one per question:
 *   fmtDate  what day            2026-08-14
 *   fmtWhen  what day and time   2026-08-14 18:30      <- the dashboard default
 *   fmtExact unambiguous, for a tooltip, with the zone spelled out
 */
const _at = (t) => new Date(t * 1000);
const fmtDate = (t) => t ? _at(t).toLocaleDateString('en-CA') : '';
// The zone is IN the timestamp, not only in the tooltip: `2026-08-14 18:30 PDT`.
// A bare time on a controlled record invites the reader to assume UTC — which is
// exactly the mistake this code was making — and a timestamp you have to hover to
// trust is one people misread. `short` (PDT/PST) rather than `shortGeneric` (PT)
// because it names the offset actually in effect, so a record does not go
// ambiguous across a DST boundary.
const fmtWhen = (t) => t
  ? `${_at(t).toLocaleDateString('en-CA')} `
    + _at(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit',
                                      hour12: false, timeZoneName: 'short' })
  : '—';
const fmtExact = (t) => t
  ? _at(t).toLocaleString([], { dateStyle: 'full', timeStyle: 'long' })
  : '';
/* "15 minutes ago" — the reading a human actually does first.
 *
 * BOTH forms, always, because they answer different questions: the absolute
 * stamp is what you quote in a ticket or match against a log, the relative one is
 * how you tell at a glance whether something just happened. Neither replaces the
 * other, so neither is hidden behind a hover.
 *
 * Intl.RelativeTimeFormat rather than hand-rolled arithmetic: it pluralises, and
 * `numeric: 'auto'` gives "yesterday" instead of the stilted "1 day ago". */
const AGO = new Intl.RelativeTimeFormat([], { numeric: 'auto' });
const AGO_STEPS = [
  [90, 'second', 1], [3600, 'minute', 60], [86400, 'hour', 3600],
  [604800, 'day', 86400], [2629800, 'week', 604800],
  [31557600, 'month', 2629800], [Infinity, 'year', 31557600],
];
const fmtAgo = (t) => {
  if (!t) return '';
  const d = Date.now() / 1000 - t;
  if (Math.abs(d) < 45) return 'just now';
  for (const [limit, unit, div] of AGO_STEPS) {
    if (Math.abs(d) < limit) return AGO.format(-Math.round(d / div), unit);
  }
  return '';
};

/** A timestamp: the absolute local time, with the relative form under it.
 *
 * One helper, because the pattern repeats and a hand-written copy at each site is
 * how one of them ends up formatted differently from the rest. */
const when = (t, cls = null) => {
  const s = el('span', 'when' + (cls ? ' ' + cls : ''));
  s.appendChild(el('span', 'abs', fmtWhen(t)));
  if (t) {
    const rel = el('span', 'rel', fmtAgo(t));
    // ⭐ The epoch rides on the element so ONE ticker can refresh every relative
    // label on screen without any registry to keep in sync. plm does not poll the
    // dashboard, so without this "15 minutes ago" would still claim 15 minutes an
    // hour later — a stale relative time is worse than none, because the exact
    // stamp sitting beside it makes the contradiction visible.
    rel.dataset.ago = String(t);
    s.appendChild(rel);
    s.title = fmtExact(t);
  }
  return s;
};
setInterval(() => {
  for (const n of document.querySelectorAll('[data-ago]')) {
    n.textContent = fmtAgo(Number(n.dataset.ago));
  }
}, 30000);
const stamp = (state, big = false) => {
  const s = el('span', `stamp st-${state || 'DRAFT'}` + (big ? ' big' : ''), state || '—');
  return s;
};
const kindchip = (kind) => el('span', 'kindchip', kind);

/* ------------------------------------------------- thumbnails that arrive late
 * PRESENCE IS DATA, NOT A GUESS. Every row the server hands us carries `thumb`:
 * a ready-to-use URL when that revision has geometry, or **null** when there is
 * nothing to render. Null means DO NOT ASK — draw the placeholder and issue
 * zero requests. (This page used to guess that every BOM child might have a
 * picture: opening a 25-line assembly fired 25 requests that could only 404,
 * and the retry below re-fired each of them three times. plm/thumbs.py answers
 * it from the DB instead, in the same payload as the row.)
 *
 * When `thumb` IS present, a 404 means the PNG is not rendered YET — the first
 * render of a fresh check-in is an OpenSCAD run behind a 2-slot semaphore, so
 * the very request that asks for it is often the one that triggers it. That is
 * exactly what the retry is for: back off behind a 'rendering…' placeholder,
 * then give up honestly.
 *
 * One helper, used by every thumbnail on the site (product cards, the item
 * shot, BOM and relation rows, the activity feed, approval rows).
 */
const THUMB_RETRY_MS = [5000, 15000, 45000];

function retryThumb(img, src, giveUp, label = 'rendering…') {
  // No URL = no geometry. The server already told us; nothing to fetch, nothing
  // to retry, no timers left running. The fallback is deferred by one microtask
  // on purpose: callers write `const img = thumbImg(src, () => img.replaceWith(…))`,
  // so running it inline would touch `img` before its binding exists.
  if (!src) { queueMicrotask(giveUp); return img; }
  let n = 0, ph = null;
  const bust = () => src + (src.includes('?') ? '&' : '?') + '_r=' + Date.now();
  const clearPh = () => { if (ph) { ph.remove(); ph = null; } img.style.display = ''; };
  img.onload = clearPh;
  img.onerror = () => {
    if (n >= THUMB_RETRY_MS.length) { clearPh(); giveUp(); return; }
    if (!ph && img.parentNode) {          // say what is happening while waiting
      ph = el('span', 'noimg pending', label);
      img.style.display = 'none';
      img.parentNode.insertBefore(ph, img);
    }
    setTimeout(() => { img.src = bust(); }, THUMB_RETRY_MS[n++]);
  };
  img.src = src;
  return img;
}

// The common case: a fresh <img> for a row's `thumb`, with a caller-supplied
// fallback for "nothing to show". A null/absent src goes straight to it.
function thumbImg(src, giveUp, { lazy = true, label } = {}) {
  const img = el('img');
  img.alt = '';
  if (lazy) img.loading = 'lazy';
  return retryThumb(img, src, giveUp, label);
}

/* --------------------------------------------------------------- search */
const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function renderSearch(q) {
  const page = $('page');
  page.innerHTML = '';
  $('gq').value = q;
  const hits = await api('/api/search?q=' + encodeURIComponent(q));
  const doc = el('div', 'doc');
  const h = el('h3', null,
    `Search — "${q}" · ${hits.length} hit${hits.length === 1 ? '' : 's'}` +
    (hits[0] && hits[0].mode === 'any term' ? ' (loose match: no result had every word)' : ''));
  doc.appendChild(h);
  for (const hit of hits) {
    const row = el('div', 'hit');
    const target = itemPath(hit.number);
    row.onclick = () => { navigate(target); };
    const top = el('div', 'top');
    const a = el('a', 'num', hit.number); a.href = target;
    top.append(a, el('span', null, hit.name || ''), el('span', 'dim ver',
      hit.version ? 'v' + hit.version : 'item'), kindchip(hit.kind));
    const snip = el('div', 'snip');
    snip.innerHTML = escapeHtml(hit.snippet || '')
      .replaceAll('«', '<mark>').replaceAll('»', '</mark>');
    row.append(top, snip);
    doc.appendChild(row);
  }
  if (!hits.length) doc.appendChild(el('div', 'empty',
    'No matches. Search covers numbers, names, change summaries, context notes and attached markdown.'));
  page.appendChild(doc);
}

$('gq').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && $('gq').value.trim()) {
    navigate(PLM_ROUTES.P.search($('gq').value.trim()));
  }
});

/* ------------------------------------------------------------ dashboard */
async function renderDashboard() {
  const page = $('page');
  page.innerHTML = '';
  const [acts, stats] = await Promise.all([
    api(`/api/activity?limit=${FEED_PAGE}`), api('/api/stats')]);
  $('mastStats').textContent =
    `${stats.items} items · ${stats.revisions} revisions · ${stats.bom_lines} BOM lines`;

  // ⭐ STAGE 14 HOOK (web/changes.js): the TOP card is "Waiting on you" — open
  // changes and release sets, newest first, each one click from its page. An
  // instance with no changes API (git-first off) keeps the old queue below.
  const waiting = window.CHANGES_UI ? await CHANGES_UI.waitingCard() : null;
  if (waiting) page.appendChild(waiting);

  // Projects: ONE CARD PER PROJECT — "what am I working on". The card renders
  // the project's most recently active top item and lists the others beneath,
  // so three related bench-test assemblies read as one project (which they
  // are), and a project whose top item is still a part is visible (it is real
  // work). The server decides what a top item is; see /api/products.
  const groups = await api('/api/products').catch(() => []);
  if (groups.length) {
    const grid = el('div'); grid.id = 'products';
    for (const g of groups) {
      const p = g.primary;
      const card = el('div', 'prod');
      const shot = el('div', 'shot');
      shot.onclick = () => { navigate(itemPath(p.number)); };
      shot.style.cursor = 'pointer';
      shot.appendChild(thumbImg(p.thumb,
        () => {
          shot.innerHTML = '';
          shot.appendChild(el('span', 'noimg', p.thumb ? 'NO RENDER' : 'NO GEOMETRY'));
        }));
      const info = el('div', 'info');
      const head = el('div');
      const pa = el('a', 'num', g.project); pa.href = PLM_ROUTES.P.registry({ project: g.project });
      pa.onclick = (e) => { e.stopPropagation(); inAppClick(e); };
      head.append(pa);
      // stage 10: the project's readiness — what flipping its git mode would do
      const rd = el('a', 'projlink', 'readiness');
      rd.href = PLM_ROUTES.P.project(g.project);
      rd.title = 'What flipping this project to read / write would DO, and what blocks it';
      rd.onclick = (e) => { e.stopPropagation(); inAppClick(e); };
      head.append(document.createTextNode(' '), rd);
      info.append(head, el('div', 'nm', p.name || p.number));
      const tops = el('div', 'tops');
      for (const t of g.top_items) {
        const row = el('div', 'toprow' + (t.id === p.id ? ' on' : ''));
        const a = el('a', 'num', t.number); a.href = itemPath(t.number);
        a.onclick = (e) => { e.stopPropagation(); inAppClick(e); };
        row.append(a, el('span', 'nm', t.name || ''), el('span', 'ver', 'v' + t.version),
          stamp(t.state));
        tops.appendChild(row);
      }
      info.appendChild(tops);
      const what = g.fallback_kind === 'part'
        ? `${g.counts.top_items} top part(s) · no assembly yet`
        : `${g.counts.top_items} top assembl${g.counts.top_items === 1 ? 'y' : 'ies'}`;
      info.appendChild(el('div', 'counts',
        `${what} · ${g.counts.items} items · ${g.counts.n_print} to print`
        + (g.counts.n_make ? ` · ${g.counts.n_make} to make` : '')
        + ` · ${g.counts.n_buy} to buy`));
      card.append(shot, info);
      grid.appendChild(card);
    }
    page.appendChild(grid);
  }

  // The approval queue: the human gate of the release process. Always on the
  // page, empty or not — a gate you cannot find is not a gate. With changes
  // (stage 14) the gate IS the "Waiting on you" card; the legacy queue then
  // shows only rounds that belong to no change (projects outside `write`).
  page.appendChild(await approvalQueue(renderDashboard, 'Awaiting approval',
                                       { legacyOnly: !!waiting }));

  const doc = el('div', 'doc');
  doc.innerHTML = '<h3>Recently changed</h3>';
  // PAGINATED, and it APPENDS rather than replacing. "Recently changed" is a
  // reading order — you scan down it — so a page that swapped the rows out would
  // make you lose your place to see the next 24. The server reports `total`, so
  // the button can say how many are left and disappear when there are none,
  // instead of being a live button that fetches nothing.
  const feed = el('div', 'feedlist');
  for (const a of (acts.activity || [])) feed.appendChild(feedRow(a));

  function feedRow(a) {
    const row = el('div', 'feedrow');
    row.onclick = () => { navigate(itemPath(a.number)); };
    const tb = el('div', 'thumbbox');
    const fallback = () => {
      tb.innerHTML = '';
      tb.appendChild(el('span', 'noimg', a.kind.toUpperCase().slice(0, 4)));
    };
    // `thumb` decides — the feed used to guess from stl_count/kind, which made
    // an assembly of purchased parts fire a request that could only 404.
    if (a.thumb) tb.appendChild(thumbImg(a.thumb, fallback));
    else fallback();
    const who = el('div', 'who');
    const numA = el('a', 'num', a.number); numA.href = itemPath(a.number);
    who.append(numA, el('span', 'nm', a.name || a.kind));
    const what = el('div', 'what', a.summary || '—');
    if (!a.summary) what.classList.add('dim');
    const meta = el('div', 'meta');
    const top = el('span'); top.appendChild(stamp(a.state));
    // WHEN IT CHANGED, to the minute, in the viewer's own timezone.
    //
    // `a.date` is the revision's AUTHORED date — engineering metadata, a day with
    // no time in it. `a.created_at` is when the change actually landed in plm, and
    // it has minutes. The feed's question is "what happened recently", so the
    // timestamp wins and the authored date is shown only when it disagrees (a
    // revision back-dated to the day the work was really done). Hover gives the
    // full local time with the zone named, so there is never a doubt.
    const ver = el('span', 'when');
    ver.appendChild(el('span', 'abs', `v${a.version} · ${fmtWhen(a.created_at)}`));
    if (a.created_at) {
      const rel = el('span', 'rel', fmtAgo(a.created_at));
      rel.dataset.ago = String(a.created_at);
      ver.appendChild(rel);
    }
    ver.title = fmtExact(a.created_at)
      + (a.date && a.date !== fmtDate(a.created_at) ? `\nauthored date: ${a.date}` : '');
    meta.append(top, ver);
    row.append(tb, who, what, meta);
    return row;
  }
  doc.appendChild(feed);
  if (!(acts.activity || []).length) {
    doc.appendChild(el('div', 'empty', 'Nothing checked in yet.'));
  }

  // The pager. `shown` is what is on screen; `total` is what exists. The button
  // names the remainder, so it answers "is it worth clicking" before you click.
  let shown = (acts.activity || []).length;
  const total = Number.isFinite(acts.total) ? acts.total : shown;
  const more = el('button', 'btn feedmore');
  const paint = () => {
    const left = Math.max(0, total - shown);
    more.textContent = left ? `Show ${Math.min(FEED_PAGE, left)} more — ${left} older` : '';
    more.style.display = left ? '' : 'none';
  };
  more.onclick = async () => {
    more.disabled = true; more.textContent = 'Loading…';
    try {
      const next = await api(`/api/activity?limit=${FEED_PAGE}&offset=${shown}`);
      for (const a of (next.activity || [])) feed.appendChild(feedRow(a));
      shown += (next.activity || []).length;
    } catch (e) {
      toast(String(e.message || e));
    } finally { more.disabled = false; paint(); }
  };
  paint();
  doc.appendChild(more);
  page.appendChild(doc);
}

/* ------------------------------------------------------------- registry */
const R = { items: null, q: '', kind: '', project: '', stats: null };

async function renderRegistry() {
  const page = $('page');
  page.innerHTML = '';
  if (!R.items) {
    [R.items, R.stats] = await Promise.all([api('/api/items'), api('/api/stats')]);
    $('mastStats').textContent =
      `${R.stats.items} items · ${R.stats.revisions} revisions · ${R.stats.bom_lines} BOM lines`;
  }

  const tools = el('div'); tools.id = 'tools';
  const q = el('input'); q.id = 'q'; q.placeholder = 'Search number or name…  ( / )';
  q.value = R.q;
  q.oninput = () => { R.q = q.value; drawRows(); };
  tools.appendChild(q);

  const kinds = Object.keys(R.stats.kinds).sort();
  for (const k of kinds) {
    const c = el('button', 'chip' + (R.kind === k ? ' on' : ''), `${k} ${R.stats.kinds[k]}`);
    c.onclick = () => { R.kind = R.kind === k ? '' : k; renderRegistry(); };
    tools.appendChild(c);
  }
  for (const p of R.stats.projects) {
    const c = el('button', 'chip' + (R.project === p ? ' on' : ''), p);
    c.onclick = () => { R.project = R.project === p ? '' : p; renderRegistry(); };
    tools.appendChild(c);
  }
  page.appendChild(tools);

  const table = el('table', 'reg');
  table.innerHTML = `<thead><tr>
    <th>Number</th><th>Name</th><th>Kind</th><th>Current</th><th>State</th>
    <th>Revs</th><th>Updated</th></tr></thead>`;
  const tbody = el('tbody');
  table.appendChild(tbody);
  page.appendChild(table);

  function drawRows() {
    tbody.innerHTML = '';
    const needle = R.q.toLowerCase();
    const shown = R.items.filter(i =>
      (!R.kind || i.kind === R.kind) &&
      (!R.project || i.project === R.project) &&
      (!needle || i.number.toLowerCase().includes(needle) || i.name.toLowerCase().includes(needle)));
    for (const i of shown) {
      const tr = el('tr');
      tr.onclick = () => { navigate(itemPath(i.number)); };
      const num = el('td', 'num'); num.appendChild(el('a', 'num', i.number));
      const name = el('td', null, i.name || '—');
      if (!i.name) name.classList.add('dim');
      const kind = el('td'); kind.appendChild(kindchip(i.kind));
      const ver = el('td', 'ver', i.latest_version ? 'v' + i.latest_version : '—');
      const st = el('td'); if (i.latest_state) st.appendChild(stamp(i.latest_state));
      const revs = el('td', 'dim', String(i.rev_count || 0));
      // Last touched, with the time — same reasoning as the dashboard feed. A
      // registry sorted by "updated" is unreadable if two rows from the same day
      // are indistinguishable.
      const upd = el('td', 'dim');
      upd.appendChild(when(i.updated_at));
      if (i.latest_date && i.latest_date !== fmtDate(i.updated_at)) {
        upd.title = `latest revision dated: ${i.latest_date}`;
      }
      tr.append(num, name, kind, ver, st, revs, upd);
      tbody.appendChild(tr);
    }
    if (!shown.length) {
      const tr = el('tr'); const td = el('td', 'empty', 'Nothing matches. Clear a filter or search differently.');
      td.colSpan = 7; tr.appendChild(td); tbody.appendChild(tr);
    }
  }
  drawRows();
  q.focus();
}

/* -------------------------------------------------- related items (typed)
 * ONE card for every kind of link (plm/relations.py, docs/PLM_RELATIONS.md).
 * The server hands back each relation already labelled from this item's point
 * of view, so nothing here knows what a coupon or a fork is: it groups by
 * `label`, draws a thumbnail and a link, and offers the registry's own type
 * list when adding one. A new relation type is a dict entry on the server and
 * shows up here with no UI change at all.
 */
let relTypesCache = null;
const relTypes = () => (relTypesCache = relTypesCache
  || api('/api/relations/types').catch(() => []));

async function linkDialog(item, version, done) {
  const types = await relTypes();
  // Both directions of every type the registry allows for THIS item's kind.
  const opts = [];
  for (const t of types) {
    if (!t.from_kinds.length || t.from_kinds.includes(item.kind))
      opts.push({ t, dir: 'out', text: `${item.number} ${t.label} …` });
    if (!t.to_kinds.length || t.to_kinds.includes(item.kind))
      opts.push({ t, dir: 'in', text: `${item.number} ${t.inverse_label} …` });
  }
  if (!opts.length) { toast('No relation type applies to a ' + item.kind); return; }
  const menu = opts.map((o, i) => `${i + 1}. ${o.text}   (${o.t.type})`).join('\n');
  const pick = prompt(`Link ${item.number} to another item.\n\n${menu}\n\n`
    + 'Which one? (number)');
  const o = opts[Number(pick) - 1];
  if (!o) return;
  const other = (prompt(`${o.text.replace('…', '')} which item?\n\nItem number:`) || '').trim();
  if (!other) return;
  const body = { type: o.t.type, note: '', actor: actorName() };
  if (o.dir === 'out') {
    body.from = item.number; body.to = other;
    if (o.t.rev_level) {
      body.to_version = (prompt(`Which revision of ${other}? (blank = its latest)`) || '').trim();
      body.from_version = (prompt(`Which revision of ${item.number}? (blank = latest)`,
        version || '') || '').trim();
    }
  } else {
    body.from = other; body.to = item.number;
    if (o.t.rev_level) {
      body.to_version = (prompt(`Which revision of ${item.number} does ${other} concern?`
        + ' (blank = latest)', version || '') || '').trim();
      body.from_version = (prompt(`Which revision of ${other}? (blank = its latest)`) || '').trim();
    }
  }
  body.note = prompt('Note — why does this link exist? (optional)') || '';
  try {
    await post('/api/relations', body);
    toast(`Linked: ${body.from} ${o.t.label} ${body.to}`);
    done();
  } catch (e) { toast(e.message); }
}

/* Comments on one relation, drawn into an expanded row. Reads and writes the
 * GENERIC context surface (GET/POST /api/context) — the same append-only log
 * the item page's Context card shows, pointed at a different target. */
async function linkComments(cell, r, done) {
  let data;
  try { data = await api(`/api/context?target_type=relation&target_id=${r.id}`); }
  catch (e) {
    cell.innerHTML = '';
    cell.appendChild(el('div', 'empty', 'could not load comments: ' + e.message));
    return;
  }
  cell.innerHTML = '';
  const head = el('div', 'dim', `comments on this link — ${data.target}`);
  cell.appendChild(head);
  for (const c of data.entries) {
    const e = el('div', 'ctx');
    const meta = el('div', 'meta');
    meta.append(el('span', null, 'link'), el('span', null, c.actor || '—'),
      when(c.created_at));
    e.append(meta, el('div', 'body', c.body));
    cell.appendChild(e);
  }
  if (!data.entries.length) cell.appendChild(el('div', 'empty',
    'No comments on this link yet. Why does it exist? What did it prove?'));
  const add = el('div', 'ctxadd');
  const ta = el('textarea');
  ta.placeholder = 'Comment on this link (markdown)';
  const row = el('div');
  row.style.cssText = 'display:flex;gap:8px;margin-top:7px;align-items:center';
  const save = el('button', 'btn primary', 'Add comment');
  save.onclick = async () => {
    if (!ta.value.trim()) return;
    try {
      await post('/api/context', {
        body: ta.value, target_type: 'relation', target_id: String(r.id),
        actor: actorName(),
      });
      toast('Comment recorded');
      done();
    } catch (e) { toast(e.message); }
  };
  row.appendChild(save);
  add.append(ta, row);
  cell.appendChild(add);
}

function relatedCard(item, version, done) {
  const card = el('div', 'doc');
  const h = el('h3', null, 'Related items');
  const rb = el('span', 'right');
  const add = el('button', 'btn', 'Link…');
  add.title = 'Record a typed relation to another item (coupon, fork lineage, …)';
  add.onclick = () => linkDialog(item, version, done);
  rb.appendChild(add);
  h.appendChild(rb);
  card.appendChild(h);

  const rels = item.related || [];
  if (!rels.length) {
    card.appendChild(el('div', 'empty',
      'No relations recorded. Typed links live here — the coupon that verifies '
      + 'a revision, the item this one was forked from, and whatever the '
      + 'registry learns next.'));
    return card;
  }
  const groups = new Map();
  for (const r of rels) {
    if (!groups.has(r.label)) groups.set(r.label, []);
    groups.get(r.label).push(r);
  }
  for (const [label, rows] of groups) {
    const g = el('div', 'relgroup');
    const gl = el('div', 'rellbl', label);
    gl.title = rows[0].hint || rows[0].type;
    g.appendChild(gl);
    const t = el('table', 'led');
    const tb = el('tbody'); t.appendChild(tb);
    for (const r of rows) {
      const tr = el('tr');
      const th = el('td', 'bomthumb');
      const rimg = thumbImg(r.thumb, () => rimg.replaceWith(
        el('span', 'nothumb', (r.kind || '').toUpperCase().slice(0, 3) || '—')),
        { label: '…' });
      th.appendChild(rimg);
      const who = el('td');
      const a = el('a', 'num', r.number); a.href = itemPath(r.number);
      who.append(a, document.createTextNode('  '));
      who.appendChild(kindchip(r.kind));
      if (r.name) who.append(el('div', 'dim', r.name));
      // A revision-level link normally names one version. An unpinned side
      // (e.g. a coupon registered before its own first check-in) shows the
      // other item's current revision instead, and says so on hover.
      // Stage 10: a derived row's version is the CLAIMANT revision's — the
      // coupon revision that makes the claim — even on a row written before
      // the registry recorded it (relations.claimant infers it).
      const claimV = r.version || (r.derived && r.home !== 'item'
        ? r.claimant_version : '');
      const ver = el('td', 'ver', claimV ? 'v' + claimV
        : r.rev_level ? (r.latest_version ? 'v' + r.latest_version : '—')
        : 'item');
      if (!r.version) ver.title = r.rev_level
        ? 'not pinned to a revision — showing this item’s current one'
        : 'item-level relation';
      const st = el('td'); if (r.state) st.appendChild(stamp(r.state));
      const why = el('td', 'dim',
        [r.my_version ? `concerns v${r.my_version}` : '', r.note || '']
          .filter(Boolean).join(' · '));
      // Stage 10: what the claim FOUND, and where it is RECORDED — the inverse
      // is derived, and saying so is what stops anybody recording it twice.
      if (r.result) {
        const rc = el('span', 'resultchip ' + r.result, r.result);
        rc.title = [r.result_note || '', Object.entries(r.measured || {})
          .map(([k, v]) => `${k} = ${v}`).join(', ')].filter(Boolean).join('\n');
        why.prepend(rc, document.createTextNode(' '));
      }
      if (r.home && item.git_mode && item.git_mode !== 'off') {
        const file = { revision: 'plm.toml', item: 'item.toml',
          note: 'refs/notes/plm' }[r.home] || r.recorded_in;
        const owner = r.derived ? r.number : item.number;
        const home = el('div', 'relhome', r.claimant === 'none'
          ? `registry only — ${owner} has no revision to declare it in`
          : r.derived
            ? `derived · recorded in ${r.number}${r.home !== 'item'
              && claimV ? ' v' + claimV : ''} ${file}`
            : `recorded here · ${r.recorded_in}`);
        if (r.to_commit) home.appendChild(el('span', 'mono',
          ` · pins ${r.to_commit.slice(0, 10)}`));
        home.title = r.derived
          ? `To change this, change ${r.number}: the side that makes a claim `
            + 'records it, and this page derives the other direction.'
          : `${r.type} lives in ${r.recorded_in} (relations.HOMES).`;
        why.appendChild(home);
      }
      const act = el('td');
      act.style.whiteSpace = 'nowrap';
      // Comments on the LINK itself (plm/context.py): a link is a thing people
      // question — "why is this a bench test and not a product?" — so it is a
      // context target like any other. Count now, bodies on demand.
      const talk = el('button', 'btn tiny', `💬 ${r.comments || ''}`.trim());
      talk.title = r.comments
        ? `${r.comments} comment(s) on this link — click to read`
        : 'Comment on this link — why does it exist?';
      const x = el('button', 'btn tiny', '✕');
      x.title = `Unlink this ${r.type} relation`;
      x.onclick = async () => {
        try {
          await post(`/api/relations/${r.id}?actor=${encodeURIComponent(actorName())}`,
            {}, 'DELETE');
          toast(`Unlinked ${r.number}`);
          done();
        } catch (e) { toast(e.message); }
      };
      act.append(talk, x);
      // Three columns, not six: the card sits in the page's narrow column
      // (≈370 px), and six cells set a minimum the card cannot hold.
      const line1 = who.firstChild ? el('div', 'relline') : null;
      if (line1) {
        while (who.firstChild && who.firstChild.nodeName !== 'DIV') line1.appendChild(who.firstChild);
        who.prepend(line1);
        ver.className = 'ver'; line1.append(el('span', 'relver', ver.textContent));
        if (ver.title) line1.lastChild.title = ver.title;
        if (st.firstChild) line1.appendChild(st.firstChild);
      }
      if (why.textContent) {
        const w = el('div', 'relwhy');
        while (why.firstChild) w.appendChild(why.firstChild);
        who.appendChild(w);
      }
      tr.append(th, who, act);
      tb.appendChild(tr);
      // The expandable row: one <tr> spanning the table, closed until asked.
      const drawer = el('tr');
      drawer.style.display = 'none';
      const cell = el('td'); cell.colSpan = 3;
      drawer.appendChild(cell);
      tb.appendChild(drawer);
      talk.onclick = async () => {
        const open = drawer.style.display === 'none';
        drawer.style.display = open ? '' : 'none';
        if (!open) return;
        cell.innerHTML = '';
        cell.appendChild(el('div', 'empty', 'loading…'));
        await linkComments(cell, r, done);
      };
    }
    g.appendChild(t);
    card.appendChild(g);
  }
  return card;
}

/* ------------------------------------------------------------ project page
 * `/projects/<p>` — what flipping this project's PLM_GIT_MODE would DO, before
 * anybody flips it (stage 10, plm/readiness.py). The server owns every
 * judgement: the verdict per target mode, each finding's level, the "will"
 * sentences and the command. This page lays them out and decides nothing — the
 * same report the CLI prints and the MCP returns. The flip itself stays the
 * operator's command, shown here to copy, never a button.
 */
async function renderProject(project) {
  const page = $('page');
  page.innerHTML = '';
  const crumbs = el('div', 'crumbs');
  const back = el('a', null, '← registry'); back.href = '/registry';
  crumbs.append(back, document.createTextNode(`  /  ${project}  /  readiness`));
  page.appendChild(crumbs);
  const wait = el('div', 'empty', `measuring ${project}… (reads the registry, `
    + 'the repos and a cleanup dry run; nothing is written)');
  page.appendChild(wait);
  let r;
  try { r = await api(`/api/projects/${encodeURIComponent(project)}/readiness`); }
  catch (e) { wait.textContent = `Could not read ${project}: ${e.message}`; return; }
  if (!location.pathname.startsWith('/projects/')) return;
  wait.remove();

  // ---- title block: where it stands now ----
  const c = r.counts;
  const tb = el('div'); tb.id = 'tblock';
  const cell = (label, value, cls = '') => {
    const d = el('div', 'cell' + (cls ? ' ' + cls : ''));
    d.appendChild(el('div', 'lbl', label));
    const v = el('div', 'v');
    if (value instanceof Node) v.appendChild(value); else v.textContent = value;
    d.appendChild(v);
    return d;
  };
  const modeChip = el('span', 'modechip m-' + r.mode.mode, r.mode.mode);
  modeChip.title = `${r.mode.label}\n(source: ${r.mode.source})`;
  const name = cell('Project', project, 'wide'); name.querySelector('.v').classList.add('title');
  tb.append(name, cell('PLM_GIT_MODE now', modeChip),
    cell('Items', `${c.items} (${c.design_items} design)`),
    cell('Revisions', `${c.revisions}`),
    cell('Released · draft', `${c.releases} · ${c.drafts}`),
    cell('Repos built / live', `${r.repos.built} / ${r.repos.live.length}`),
    cell('Catalog items used', `${c.catalog_items_referenced}`));
  page.appendChild(tb);
  if (window.CHANGES_UI && CHANGES_UI.banners) {        // §2.9: one row per change
    const bn = el('div');
    page.appendChild(bn);
    CHANGES_UI.banners(bn, { project });
    page.appendChild(CHANGES_UI.pullList({ project }));
  }

  // ---- the panel: what flipping to read / write will do ----
  const flip = el('div', 'flipgrid');
  for (const target of ['read', 'write']) {
    const v = r.verdicts[target];
    const card = el('div', 'doc flip ' + (v.ok ? (v.warnings.length ? 'warn' : 'ok') : 'block'));
    const h = el('h3', null, `Flipping to ${target}`);
    const right = el('span', 'right');
    right.appendChild(el('span', 'verdict', v.verdict));
    h.appendChild(right);
    card.appendChild(h);
    const body = el('div', 'pad');
    body.appendChild(el('div', 'now', v.now));
    body.appendChild(el('div', 'lbl', 'it will'));
    const ul = el('ul', 'will');
    for (const w of r.will[target]) ul.appendChild(el('li', null, w));
    body.appendChild(ul);
    const order = { block: 0, warn: 1, info: 2 };
    const fs = [...v.findings].sort((a, b) => order[a.level] - order[b.level]);
    if (fs.length) {
      body.appendChild(el('div', 'lbl', fs.some(f => f.level === 'block')
        ? 'what blocks it, and what to know' : 'what to know'));
      for (const f of fs) {
        const row = el('div', 'finding ' + f.level);
        row.appendChild(el('span', 'lvl', f.level));
        const t = el('div', 'txt');
        t.appendChild(el('div', null, f.text));
        if (f.fix) {
          const fx = el('div', 'fix');
          fx.append(el('span', 'lbl', 'fix '), el('code', null, f.fix));
          t.appendChild(fx);
        }
        if ((f.items || []).length) {
          const d = el('details', 'items');
          d.appendChild(el('summary', null,
            `${f.items.length}${f.more ? ` of ${f.items.length + f.more}` : ''} listed`));
          const l = el('ul');
          for (const it of f.items) l.appendChild(el('li', null, it));
          d.appendChild(l);
          t.appendChild(d);
        }
        row.appendChild(t);
        body.appendChild(row);
      }
    }
    const cmd = el('div', 'cmd');
    cmd.append(el('div', 'lbl', 'the flip (an operator command, the rollout runbook)'),
      el('code', null, r.flip[target]));
    body.appendChild(cmd);
    card.appendChild(body);
    flip.appendChild(card);
  }
  page.appendChild(flip);

  // ---- the facts behind the verdicts ----
  const facts = el('div', 'flipgrid');
  const kv = (title, rows, extra) => {
    const card = el('div', 'doc');
    card.appendChild(el('h3', null, title));
    const t = el('table', 'led kvt');
    const tb2 = el('tbody');
    for (const [k, v] of rows) {
      const tr = el('tr');
      tr.append(el('td', 'dim', k), el('td', 'mono', String(v)));
      tb2.appendChild(tr);
    }
    t.appendChild(tb2);
    card.appendChild(t);
    if (extra) card.appendChild(extra);
    return card;
  };
  const list = (label, items, more = 0) => {
    if (!(items || []).length) return null;
    const d = el('details', 'items pad');
    d.appendChild(el('summary', null, `${label} (${items.length}${more ? ` of ${items.length + more}` : ''})`));
    const l = el('ul');
    for (const it of items) l.appendChild(el('li', null, it));
    d.appendChild(l);
    return d;
  };
  const rel = r.relations;
  const relBox = el('div');
  for (const x of [list('every relation: old home → new home', rel.moves, rel.more),
    list('coupons with no result yet', rel.coupons_without_result)]) if (x) relBox.appendChild(x);
  facts.appendChild(kv('Relations — one home per claim', [
    ...Object.entries(rel.by_home).map(([k, v]) => [k.replace(':', ' · '), v]),
    ['coupon claims', rel.coupons],
    ['with a result', `${rel.coupons_with_result} (${rel.coupons_passing} pass)`]], relBox));
  const pinBox = el('div');
  const pp = list('parents that could not be RELEASED today',
    (r.pins.parents_not_releasable || []).map(p =>
      `${p.item} v${p.version} (${p.state}) — ${p.blocked_by.join('; ')}`));
  if (pp) pinBox.appendChild(pp);
  facts.appendChild(kv('BOM pins', Object.entries(r.pins.lines), pinBox));
  const fnBox = el('div');
  const fe = list('renames', r.filenames.examples, r.filenames.more);
  if (fe) fnBox.appendChild(fe);
  const fc = list('collisions', r.filenames.collisions);
  if (fc) fnBox.appendChild(fc);
  facts.appendChild(kv('Filenames (§6c-1)', [
    ['normalised in the repos', r.filenames.normalise ? 'yes' : 'no'],
    ['tracked files renamed', r.filenames.renames],
    ['collisions', r.filenames.collisions.length]], fnBox));
  const srcBox = el('div');
  for (const k of ['prose', 'none']) {
    const x = list(k === 'prose' ? 'prose selectors' : 'no [source]', r.sources[k].items);
    if (x) srcBox.appendChild(x);
  }
  facts.appendChild(kv('Sources — what render-matches-source will say', [
    ['regenerable', r.sources.regenerable.count], ['prose selector', r.sources.prose.count],
    ['no source', r.sources.none.count]], srcBox));
  facts.appendChild(kv('External files', [
    ['files', r.external.count], ['size', `${(r.external.bytes / 1e6).toFixed(0)} MB`],
    [`over the ${(r.external.cap_bytes / 1e6).toFixed(0)} MB cap`, r.external.over_cap]],
    list('largest', r.external.largest)));
  const clBox = el('div');
  for (const cr of r.cleanup.rules) {
    const x = list(`${cr.rule} — ${cr.lines} line(s) [${cr.level}]`, cr.examples);
    if (x) clBox.appendChild(x);
  }
  clBox.appendChild(el('div', 'pad dim small', r.cleanup.note));
  facts.appendChild(kv('Cleanup (migrate_cleanup --dry-run)', [
    ...r.cleanup.rules.map(cr => [cr.rule, `${cr.lines} line(s) · ${cr.level}`]),
    ['relations rows to write', r.cleanup.relations_would_change]], clBox));
  const o = r.open;
  const oBox = el('div');
  for (const [k, lbl] of [['drafts', `open drafts${o.drafts.length < c.drafts ? ` — first ${o.drafts.length} of ${c.drafts}` : ''}`], ['approvals_pending', 'pending approvals'],
    ['releases_open', 'open releases'], ['checkouts', 'checkouts']]) {
    const x = list(lbl, o[k]);
    if (x) oBox.appendChild(x);
  }
  facts.appendChild(kv('Open work', [
    ['drafts', c.drafts], ['approvals pending', o.approvals_pending.length],
    ['releases open', o.releases_open.length],
    ['checkouts (stale)', `${o.checkouts.length} (${o.checkouts_stale.length})`]], oBox));
  facts.appendChild(kv('Policy (plm/policy.py)', [
    ['PROTOTYPE', `${r.policy.PROTOTYPE.auto ? 'auto-merge' : 'a decision'} · ${(r.policy.PROTOTYPE.approver_kinds || []).join(', ')}`],
    ['RELEASED', `${r.policy.RELEASED.auto ? 'auto-merge' : 'a decision'} · ${(r.policy.RELEASED.approver_kinds || []).join(', ')}`]]));
  page.appendChild(facts);
}

/* ------------------------------------------------------------- item page */
let CURRENT_ITEM = null;   // which item page is rendered (viewer routing)
// The 3D viewer is NOT a routed state (Geoff 2026-08-12: "get rid of the route
// for the 3d view — let's just keep it still the part view"). It opens over the
// item page and the URL stays on the part; Close / Esc / the backdrop dismiss it.
// The one exception is a snippet (/snip/<token>), which has no part page behind
// it — that link IS the page, so it stays routed.

/* One fetch of `/api/items/N` per item, reused while you stay on it. Picking a
 * ref re-renders the WHOLE page at that ref (§6c) rather than only the repo
 * band, and re-reading the item for every dropdown click would make the page
 * feel like a reload. `renderItem(n, {fresh:true})` after a write drops it. */
let ITEM_CACHE = null;

async function renderItem(number, at = {}) {
  CURRENT_ITEM = number;
  REPO = null;
  const page = $('page');
  page.innerHTML = '';
  let item;
  if (!at.fresh && ITEM_CACHE && ITEM_CACHE.number === number) {
    item = ITEM_CACHE.item;
  } else {
    try { item = await api(`/api/items/${encodeURIComponent(number)}`); }
    catch (e) { page.appendChild(el('div', 'empty', `Could not load ${number}: ${e.message}`)); return; }
    ITEM_CACHE = { number, item };
  }
  if (CURRENT_ITEM !== number) return;

  // PLM_GIT_MODE (stage 9): `off` means this project has no repository, and
  // the page is the registry's page — master's layout, no repo band, no ref
  // picker, and no repo route asked (a 404 per page view would be console
  // noise and a lie about something missing). `read`/`write` are the repo home.
  const gitOff = item.git_mode === 'off';

  // The repo, read once and kept for the whole visit. A 404 is an ordinary
  // answer here — catalog items have no repo of their own (§5.4).
  let info = null;
  if (!gitOff) {
    try { info = await repoGet(number, 'refs'); }
    catch (e) { info = { error: e.message }; }
  }
  if (CURRENT_ITEM !== number) return;

  const crumbs = el('div', 'crumbs');
  const back = el('a', null, '← registry'); back.href = '/';
  crumbs.appendChild(back);
  crumbs.appendChild(document.createTextNode('  /  '));
  if (item.project) {
    // the project page (stage 10): what flipping this project would do
    const pl = el('a', null, item.project);
    pl.href = PLM_ROUTES.P.project(item.project);
    pl.title = 'Project readiness — what flipping PLM_GIT_MODE would do';
    crumbs.appendChild(pl);
  } else crumbs.appendChild(document.createTextNode('no project'));
  crumbs.appendChild(document.createTextNode(`  /  ${item.number}`));
  page.appendChild(crumbs);

  const latest = item.revisions[0] || null;
  let bomMode = 'tree';

  /* ── the selected ref, and the revision it IS ───────────────────────────
   * A release `v0.4.1` and the DB revision 0.4.1 are the same fact seen from
   * two sides, so picking a ref in the dropdown selects that revision and the
   * database half of the page follows along. Where there is no matching
   * revision, keep revision-backed geometry and actions unavailable. */
  const defRef = info && info.releases
    ? (info.default_release || info.default_ref || '') : '';
  const ref = at.ref || defRef;
  /* Which revision a ref IS. `split` named its drafts `draft/vX.Y.Z`, so the
   * ref's own text answered it; a PUSHED draft is named by its author
   * (`draft/vent-slot-rework`) and the name answers nothing. `/refs` already
   * read the version out of `plm.toml` at the tip, so ask it first and only
   * fall back to parsing the ref. */
  const refVersion = (r) => {
    // `main` is released work only, and its tip IS the newest release.
    if (info && !info.error && r && r === info.default_ref && info.default_release)
      r = info.default_release;
    const all = info && !info.error ? [...info.releases, ...info.drafts] : [];
    const hit = all.find(x => x.ref === r);
    if (hit && hit.version) return hit.version;
    return String(r || '').replace(/^draft\//, '').replace(/^v/, '');
  };
  const revOf = (r) => {
    // A historical snapshot must never borrow the newest revision's geometry.
    // Full commit identities are authoritative; a version can be reused by a
    // later push to a draft, so parsing a SHA's manifest version is insufficient.
    if (/^[0-9a-f]{40}$/i.test(r || '')) {
      return item.revisions.find(x => x.git_commit === r) || null;
    }
    return item.revisions.find(x => x.version === refVersion(r)) || null;
  };
  let selected = ref ? revOf(ref) : latest;
  // `main` IS the release the repo's default_ref names (a tree/main banner
  // used to say "no revision matches this ref")
  const onDefault = !ref || ref === defRef
    || !!(info && !info.error && ref === info.default_ref);
  // With git off there is no Part (repo home), Releases, Drafts or History
  // tab: the registry page is the Record tab, shown under the Part tab's name.
  const tab = gitOff && (!at.tab || ['part', 'releases', 'drafts', 'history'].includes(at.tab))
    ? 'record' : (at.tab || 'part');

  // ---- the title block: the drawing-corner header ----
  const tb = el('div'); tb.id = 'tblock';
  const cell = (label, value, opts = {}) => {
    const c = el('div', 'cell' + (opts.wide ? ' wide' : ''));
    c.appendChild(el('div', 'lbl', label));
    const v = el('div', 'v' + (opts.title ? ' title' : ''));
    if (value instanceof Node) v.appendChild(value); else v.textContent = value;
    c.appendChild(v);
    return c;
  };
  // Identity, with its history: a renumbered item says what it used to be
  // called, because the old number is what is written on the printed part and
  // in last month's notes (ops.renumber_item; both still resolve).
  const numCell = el('div');
  numCell.textContent = item.number;
  if ((item.former_numbers || []).length) {
    const was = el('div', 'dim', 'was ' + item.former_numbers.join(', '));
    was.style.cssText = 'font-size:10.5px;margin-top:2px';
    was.title = 'Renumbered — the old number still resolves everywhere in plm';
    numCell.appendChild(was);
  }
  tb.appendChild(cell('Item number', numCell));
  tb.appendChild(cell('Title', item.name || '(unnamed)', { wide: true, title: true }));
  const stc = el('div', 'cell stampcell');
  stc.appendChild(stamp(latest ? latest.state : 'DRAFT', true));
  tb.appendChild(stc);
  tb.appendChild(cell('Kind', kindchip(item.kind)));
  tb.appendChild(cell('Current revision', latest ? 'v' + latest.version : '—'));
  tb.appendChild(cell('Project', item.project || '—'));
  tb.appendChild(cell('ECN', latest && latest.ecn || '—'));
  page.appendChild(tb);
  // §2.9: GitHub's repo-home banners — a change waiting on you, a draft branch
  // pushed into no change (web/changes.js; plm/pending.py)
  if (!gitOff && window.CHANGES_UI && CHANGES_UI.banners) {
    const bn = el('div');
    page.appendChild(bn);
    CHANGES_UI.banners(bn, { number: item.number });
  }

  // ---- item-level actions: check out, rev up, push up the tree ----
  const bar = el('div');
  bar.style.cssText = 'display:flex;gap:8px;margin:-6px 0 18px;align-items:center;flex-wrap:wrap';
  const co = (await api(`/api/items/${item.number}/checkout`).catch(() => ({}))).checkout;
  const coBtn = el('button', 'btn',
    co ? (co.actor === actorName() ? `Release checkout` : `Checked out — ${co.actor}`) : 'Check out');
  if (co && co.actor !== actorName()) coBtn.disabled = true;
  coBtn.onclick = async () => {
    try {
      await post(`/api/items/${item.number}/checkout` + (co ? '/release' : ''), { actor: actorName() });
      toast(co ? 'Checkout released' : `Checked out to ${actorName()}`);
      renderItem(item.number, { fresh: true, ref, tab });
    } catch (e) { toast(e.message); }
  };
  const bumpSel = el('select', 'btn');
  for (const b of ['patch', 'minor', 'major']) bumpSel.appendChild(new Option(b, b));
  const revBtn = el('button', 'btn', 'Revise ↑');
  revBtn.title = 'New draft revision seeded from the current one — files and BOM carried forward';
  revBtn.disabled = !latest;
  revBtn.onclick = async () => {
    try {
      const r = await post(`/api/items/${item.number}/revise`,
        { bump: bumpSel.value, actor: actorName() });
      toast(`Revised to v${r.revision.version} (draft)`);
      renderItem(item.number, { fresh: true, ref, tab });
    } catch (e) { toast(e.message); }
  };
  bar.append(coBtn, bumpSel, revBtn);
  if (item.where_used.length && latest) {
    const propBtn = el('button', 'btn', 'Propagate ↑ tree');
    propBtn.title = 'Rev up every parent still binding an older revision of this item';
    propBtn.onclick = async () => {
      try {
        const r = await post(`/api/items/${item.number}/propagate`, { actor: actorName() });
        toast(r.created.length
          ? 'Revised: ' + r.created.map(c => `${c.number} → v${c.version}`).join(', ')
          : 'Nothing to do — parents bind this revision already, or bind imprecisely (auto-latest)');
        if (r.created.length) renderItem(item.number, { fresh: true, ref, tab });
      } catch (e) { toast(e.message); }
    };
    bar.appendChild(propBtn);
  }
  if (latest && latest.state === 'DRAFT') {
    const target = el('select', 'btn');
    target.appendChild(new Option('→ PROTOTYPE', 'PROTOTYPE'));
    target.appendChild(new Option('→ RELEASED', 'RELEASED'));
    const submit = el('button', 'btn primary', 'Submit for release');
    submit.title = 'Freeze this draft and open an approval — it becomes a real version when approved';
    submit.onclick = async () => {
      const note = prompt(`Submit ${item.number} v${latest.version} for release as ${target.value}.\n\nNote for the approver (optional):`) ?? null;
      if (note === null) return;
      try {
        await postSubmit(item.number,
          { version: latest.version, target_state: target.value, note, actor: actorName() });
        toast(`v${latest.version} submitted — awaiting approval`);
        renderItem(item.number, { fresh: true, ref, tab });
      } catch (e) { toast(e.message); }
    };
    bar.append(target, submit);
  }
  const forkBtn = el('button', 'btn', 'Fork…');
  forkBtn.title = 'Save-as: a NEW item number seeded from this one, with provenance recorded';
  forkBtn.disabled = !latest;
  forkBtn.onclick = async () => {
    const name = prompt(`Fork ${item.number} as a new item.\n\nName for the fork:`);
    if (!name) return;
    const reason = prompt('Why fork? (recorded as context — optional)') || '';
    try {
      const r = await post(`/api/items/${item.number}/fork`,
        { new_name: name, reason, actor: actorName() });
      toast(`Forked to ${r.number}`);
      navigate(itemPath(r.number));
    } catch (e) { toast(e.message); }
  };
  bar.appendChild(forkBtn);
  if (co) {
    // Stale after 6 h (PLM_CHECKOUT_STALE_SECONDS), so "3 hours ago" is the form
    // that tells you whether this lock is about to be taken over.
    const note = el('span', 'note');
    note.append(document.createTextNode('locked since '), when(co.at));
    bar.appendChild(note);
  }
  // (Lineage used to be a line of crumbs here. It is now one row among the
  // others in the generic Related items card, beside the BOM — fork
  // provenance is a relation like any other.)

  /* ── cards, then a layout ───────────────────────────────────────────────
   * Every card below is built exactly as it always was; what changed is that
   * nothing appends itself to the page any more. Each one is filed in `K` and
   * `layout()` decides which tab it belongs to — GitHub's information
   * architecture (Code | Issues | Pull requests | …) in this registry's own
   * vocabulary. Nothing was deleted; everything got a home.
   *   fixed — built once per render (ledger, where-used, sourcing)
   *   rev   — rebuilt whenever the selected revision changes
   */
  const K = { fixed: {}, rev: {} };

  // ---- item image: the selected revision's render, above the ledger ----
  const shot = el('div', 'doc itemshot');
  const shotImg = el('img');
  shotImg.alt = ''; shotImg.loading = 'lazy'; shotImg.title = 'Open 3D view';
  const shotEmpty = el('div', 'empty', 'No render for this revision.');
  shotEmpty.style.display = 'none';
  const shotBar = el('div', 'shotbar');
  const shotView = el('button', 'btn primary', 'View 3D');
  const view3d = () => {
    // Same data, no guessing: a revision with no geometry has no 3D to open.
    if (!selected || !selected.thumb) return;
    openViewer(item.number, selected.version, `${item.number} v${selected.version}`);
  };
  shotView.onclick = view3d;
  const shotVer = el('span', 'note', '');
  // WILL IT EXPLODE? Asked of the server (thumbs.explode_status — the same
  // resolver the viewer builds from), so the answer cannot disagree with what
  // the slider does. Shown only where there is a BOM to explode.
  const shotEx = el('span', 'explchip');
  shotEx.style.display = 'none';
  shotBar.append(shotVer, shotEx, shotView);
  shot.append(shotImg, shotEmpty, shotBar);
  shotImg.onclick = view3d;
  const drawShot = () => {
    if (!selected) { shotImg.style.display = 'none'; shotEmpty.style.display = 'block'; shotBar.style.display = 'none'; return; }
    shotBar.style.display = 'flex';
    shotVer.textContent = `${item.number} · v${selected.version}`;
    shotEx.style.display = 'none';
    if ((item.bom[selected.id] || []).length) {
      const want = selected.id;
      api(`/api/items/${encodeURIComponent(item.number)}/explode?version=${encodeURIComponent(selected.version)}`)
        .then(st => {
          if (!selected || selected.id !== want) return;
          shotEx.className = 'explchip ' + (st.explodable ? 'ok' : st.required ? 'bad' : 'meh');
          shotEx.textContent = st.explodable ? `explodes · ${st.bodies} bodies` : "won't explode";
          const fixes = (st.children || []).filter(k => k.fix).map(k => `• ${k.number}: ${k.fix}`);
          shotEx.title = (st.explodable ? '' : st.reason + '\n\n') + fixes.join('\n')
            + (fixes.length ? '\n\n' : '') + 'Rule: ' + st.rule;
          shotEx.style.display = '';
        }).catch(() => {});
    }
    // `selected.thumb` is the server's answer (plm/thumbs.py): a URL when this
    // revision has a body of its own or inherits one through its BOM, null when
    // there is nothing to render — and nothing to view in 3D either, so the
    // button and the click-through go away instead of failing in the dark room.
    const has = !!selected.thumb;
    shotImg.style.cursor = has ? 'pointer' : 'default';
    shotImg.title = has ? 'Open 3D view' : '';
    shotView.disabled = !has;
    shotView.title = has ? 'Open this revision in the 3D viewer'
      : `No geometry on v${selected.version} — nothing to view`;
    // A FABRICATED PART IS NOT MISSING ITS STL. Readiness for a MAKE
    // revision is its ROUTING, not a mesh — a cut length, an end treatment and a
    // hole datum fully define an 86-inch rail, and telling Geoff his correctly
    // defined bracket needs an STL is telling him a controlled part is broken.
    // (Flagged in devops/docs/projects/2026-08-13-cnc-plasma/DESIGN.md 9.2.)
    const madeOps = (selected.operations || []).length;
    shotEmpty.textContent = has ? 'No render for this revision.'
      : madeOps
        ? `No mesh on v${selected.version} — and it does not need one. This is a `
          + `FABRICATED revision: its definition is the routing below `
          + `(${madeOps} operation${madeOps === 1 ? '' : 's'}), plus the stock it `
          + `is cut from. A drawing or a flat pattern is welcome; an STL is not `
          + `what makes it manufacturable.`
        : `No geometry on v${selected.version}: no mesh of its own, and nothing `
          + 'renderable in its BOM. Check in an STL (or a viz body) to get a '
          + 'picture and a 3D view.';
    // Retries behind a 'rendering…' placeholder: the first render of a fresh
    // check-in is an OpenSCAD job that may still be queued (see retryThumb).
    shotEmpty.style.display = 'none';
    retryThumb(shotImg, selected.thumb,
      () => { shotImg.style.display = 'none'; shotEmpty.style.display = 'block'; },
      'rendering this revision…');
    if (has) shotImg.addEventListener('load', () => {
      shotImg.style.display = 'block'; shotEmpty.style.display = 'none';
    }, { once: true });
  };
  K.fixed.hero = shot;



  // ---- revision ledger ----
  const led = el('div', 'doc');
  led.innerHTML = '<h3>Revision ledger</h3>';
  const lt = el('table', 'led');
  lt.innerHTML = '<thead><tr><th>Rev</th><th>Date</th><th>State</th><th>ECN</th></tr></thead>';
  const ltb = el('tbody'); lt.appendChild(ltb);
  for (const r of item.revisions) {
    const tr = el('tr', 'rev' + (selected && r.id === selected.id ? ' on' : ''));
    const rd = el('td', 'dim', r.date || fmtDate(r.created_at));
    // Authored date stays primary here — it is the revision's date of record —
    // but the true check-in time is one hover away.
    rd.title = `checked in ${fmtExact(r.created_at)}`;
    tr.append(el('td', 'ver', 'v' + r.version), rd);
    const st = el('td'); st.appendChild(stamp(r.state)); tr.appendChild(st);
    tr.appendChild(el('td', 'dim', r.ecn || ''));
    tr.onclick = () => { selected = r; drawSelected(); ltb.querySelectorAll('tr').forEach(x => x.classList.remove('on')); tr.classList.add('on'); };
    ltb.appendChild(tr);
  }
  if (!item.revisions.length) ltb.appendChild(el('tr')).appendChild(el('td', 'empty', 'No revisions checked in yet.'));
  led.appendChild(lt);
  K.fixed.ledger = led;

  // ---- where used ----
  if (item.where_used.length) {
    const wu = el('div', 'doc');
    wu.innerHTML = '<h3>Where used</h3>';
    const wt = el('table', 'led');
    const wb = el('tbody'); wt.appendChild(wb);
    for (const w of item.where_used) {
      const tr = el('tr');
      const a = el('a', 'num', w.number); a.href = itemPath(w.number);
      const td = el('td'); td.appendChild(a);
      tr.append(td, el('td', null, w.name),
                el('td', 'ver', 'v' + w.parent_version),
                el('td', 'dim', `${w.qty} ${w.unit}`));
      wb.appendChild(tr);
    }
    wu.appendChild(wt);
    K.fixed.whereused = wu;
  }

  // ---- sourcing ----
  if (item.sources.length) {
    const so = el('div', 'doc');
    so.innerHTML = '<h3>Sourcing</h3>';
    const stt = el('table', 'led'); const sb = el('tbody'); stt.appendChild(sb);
    for (const s of item.sources) {
      const tr = el('tr');
      const td = el('td');
      if (s.url) td.appendChild(extLink(s.url, s.supplier || 'link'));
      else td.textContent = s.supplier;
      tr.append(td, el('td', null, `${s.manufacturer} ${s.mpn}`.trim()), el('td', 'dim', s.status));
      sb.appendChild(tr);
    }
    so.appendChild(stt);
    K.fixed.sourcing = so;
  }

  /* ---- the OLD "Compare revisions" card, retired to a launcher ----
   * It ran `/api/items/N/diff`, which prints the database's own summary of
   * two revisions — attributes, BOM lines, file names — as a patch-shaped
   * text blob. The git compare view is a superset of it (it renders the same
   * manifest delta as redlines, plus real file diffs, plus the 3-D overlay),
   * so keeping both meant two compare UIs of different power on one page.
   * The pickers stay, because picking two REVISIONS by number is how this
   * page has always worked; pressing the button now lands on the real thing.
   * Nothing the old card showed is missing — the DB diff's one exclusive
   * fact, revisions that have no tag and no draft branch, is on the History
   * tab, marked "database only". */
  if (item.revisions.length >= 2) {
    const df = el('div', 'doc');
    const h = el('h3', null, 'Compare revisions');
    df.appendChild(h);
    const body = el('div', 'pad');
    const rowd = el('div', 'cmppick');
    const selA = el('select', 'btn'), selB = el('select', 'btn');
    for (const r of item.revisions) {
      selA.appendChild(new Option('v' + r.version, r.version));
      selB.appendChild(new Option('v' + r.version, r.version));
    }
    selA.value = item.revisions[1].version;  // previous…
    selB.value = item.revisions[0].version;  // …to current
    const go = el('button', 'btn primary', gitOff ? 'Diff' : 'Compare');
    // PLM_GIT_MODE=off: there is no compare view to land on (no repo), so the
    // card is master's — `/api/items/N/diff`, the database's own summary,
    // printed in place.
    const out = gitOff ? el('pre', 'diff') : null;
    if (out) out.textContent = 'Pick two revisions and press Diff.';
    go.onclick = gitOff ? async () => {
      out.textContent = 'diffing…';
      try {
        const text = await api(`/api/items/${encodeURIComponent(number)}/diff?a=`
          + `${encodeURIComponent(selA.value)}&b=${encodeURIComponent(selB.value)}`);
        out.innerHTML = '';
        for (const line of String(text).split('\n')) {
          const cls = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del'
            : line.startsWith('@@') || line.startsWith('~') ? 'hunk' : '';
          out.appendChild(el('div', cls, line || ' '));
        }
      } catch (e) { out.textContent = 'diff failed: ' + e.message; }
    } : () => {
      const refFor = (v) => {
        const hit = (info && !info.error ? [...info.releases, ...info.drafts] : [])
          .find(x => x.ref.replace(/^draft\//, '').replace(/^v/, '') === v);
        return hit ? hit.ref : 'v' + v;
      };
      navigate(cmpPath(number, refFor(selA.value), refFor(selB.value)));
    };
    rowd.append(el('span', 'lbl', 'Old'), selA, el('span', 'rl-to', '→'),
                el('span', 'lbl', 'New'), selB, go);
    body.appendChild(rowd);
    if (out) body.appendChild(out);
    else body.appendChild(el('div', 'note',
      'Opens the compare view: file diffs, the manifest as redlines, and the '
      + '3-D overlay.'));
    df.appendChild(body);
    K.fixed.olddiff = df;
  }

  // ---- right column: the selected revision ----
  function drawSelected() {
    bar.style.display = selected ? 'flex' : 'none';
    drawShot();
    K.rev = {};
    if (!selected) {
      K.rev.norev = el('div', 'doc');
      K.rev.norev.appendChild(el('div', 'empty', ref
        ? 'This snapshot has no matching revision record. Browse its files and pinned parts below; revision geometry and actions are unavailable.'
        : 'No revision checked in yet.'));
      layout();
      return;
    }

    const head = el('div', 'doc');
    const h = el('h3', null, `Revision v${selected.version}`);
    const rb = el('span', 'right');
    // Integration actions: whatever downstream services registered (a printer
    // hand-off, a notifier…) appears here — the PLM only hands over data.
    getIntegrations().then(list => {
      for (const ig of list.filter(x => x.enabled && x.action_label)) {
        const b = el('button', 'btn', ig.action_label);
        b.onclick = async () => {
          try {
            // ⭐ SAY WHAT THE DOWNSTREAM SERVICE SAID, not "sent". This used to
            // toast a hardcoded "sent {number} v{version}" and DISCARD the
            // response, so the one answer that matters — did anything actually
            // happen? — was thrown away. KML-PRT-1012 was already queued and
            // batched onto a plate, so the shop-floor service correctly replied "already on
            // the print queue as q_cea9014e0f54 (batched, qty 1) — nothing
            // added"; the button reported success every time and the part never
            // appeared as new. Pressing it repeatedly was the rational response
            // to a button that lies. `result.message` is the integration's own
            // sentence; the generic line is only the fallback for a service that
            // returns nothing to say.
            await sendToIntegration(ig, item, selected, '');
          } catch (e) { toast(e.message); }
        };
        rb.appendChild(b);
      }
    });
    h.appendChild(rb);
    head.appendChild(h);
    // (No thumbnail here — the item image card by the ledger is the render,
    // with the View 3D button beside it.)

    const body = el('div', 'pad');
    if (selected.summary) body.appendChild(el('div', 'note', selected.summary));
    if (selected.verification) {
      const v = el('div', 'note'); v.style.marginTop = '6px';
      v.append(el('span', 'lbl', 'Verification  '), document.createTextNode(selected.verification));
      body.appendChild(v);
    }
    if (selected.source) {
      const s = el('div', 'note'); s.style.marginTop = '6px';
      s.append(el('span', 'lbl', 'Source  '),
               document.createTextNode(`${selected.source}` + (selected.selector ? `  ·  ${selected.selector}` : '')));
      body.appendChild(s);
    }
    if (selected.git_commit) {
      const g = el('div', 'note'); g.style.marginTop = '6px';
      g.append(el('span', 'lbl', 'Commit  '), document.createTextNode(selected.git_commit.slice(0, 12)));
      body.appendChild(g);
    }
    if (body.children.length) head.appendChild(body);
    K.rev.revhead = head;

    // TAGS: the cross-cutting labels — item-level facts that are true
    // of the thing forever, plus this revision's own. CAVEATS are shown first
    // and loudest, because an unproven load path or a provisional datum is the
    // single most important thing to know before making or releasing anything,
    // and until now it was a sentence in a paragraph.
    const itemTags = item.tags || [];
    const revTags = selected.tags || [];
    if (itemTags.length || revTags.length) {
      const td = el('div', 'doc tags');
      const caveats = [...itemTags, ...revTags].filter(t => t.caveat);
      const th = el('h3', null, 'Tags');
      if (caveats.length) {
        th.appendChild(el('span', 'cavcount',
          `${caveats.length} unresolved`));
      }
      td.appendChild(th);
      if (caveats.length) {
        td.appendChild(el('div', 'cavnote',
          'Unresolved: nothing below has been proven yet. Clearing a caveat is '
          + 'a decision somebody makes after measuring, not housekeeping.'));
      }
      if (itemTags.length) {
        td.appendChild(el('div', 'taglbl', 'This part, always'));
        td.appendChild(tagChips(item, itemTags, 'item'));
      }
      if (revTags.length) {
        td.appendChild(el('div', 'taglbl', `v${selected.version}`));
        td.appendChild(tagChips(item, revTags, 'revision'));
      }
      K.rev.tags = td;
    }

    // Attributes: the measurable facts of THIS revision (mass, material,
    // bounding box, colour, print time…). The server merges the column-backed
    // facts into the same dict and hands over the registry's label/unit, so
    // this block renders whatever exists and knows nothing about any particular
    // attribute name.
    const attrs = selected.attributes || {};
    const attrNames = Object.keys(attrs);
    const ad = el('div', 'doc');
    const ah = el('h3', null, 'Attributes');
    const arb = el('span', 'right');
    const aEdit = el('button', 'btn', 'Set…');
    aEdit.title = 'Record a measured property on this revision '
      + '(normally posted by the slicer or CAD export)';
    aEdit.onclick = async () => {
      const known = 'mass_g, material, bbox_x_mm, bbox_y_mm, bbox_z_mm, color, print_time_min';
      const name = (prompt(`Attribute name for ${item.number} v${selected.version}.`
        + `\n\nWell-known: ${known}\n(any other name is allowed)`) || '').trim();
      if (!name) return;
      const value = prompt(`Value for ${name} (blank clears it):`,
        (attrs[name] || {}).value || '');
      if (value === null) return;
      const source = prompt('Where did this number come from? (e.g. "orca 2.4.2, 0.20mm")',
        (attrs[name] || {}).source || '') || '';
      try {
        await post(`/api/revisions/${selected.id}/attributes`,
          { attributes: { [name]: { value, source } }, source, actor: actorName() }, 'PATCH');
        toast(`${name} recorded on v${selected.version}`);
        renderItem(item.number, { fresh: true, ref, tab });
      } catch (e) { toast(e.message); }
    };
    arb.appendChild(aEdit);
    ah.appendChild(arb);
    ad.appendChild(ah);
    if (attrNames.length) {
      const at = el('table', 'led');
      const atb = el('tbody'); at.appendChild(atb);
      for (const n of attrNames) {
        const a = attrs[n];
        const tr = el('tr');
        const lbl = el('td', null, a.label || n);
        lbl.title = n + (a.well_known ? ' (well-known attribute)' : ' (free-form attribute)');
        const val = el('td', 'ver', a.value + (a.unit ? ' ' + a.unit : ''));
        const src = el('td', 'dim', a.source || (a.column ? 'revision master data' : ''));
        tr.append(lbl, val, src);
        atb.appendChild(tr);
      }
      ad.appendChild(at);
    } else {
      ad.appendChild(el('div', 'empty',
        'No attributes recorded. Mass, material, bounding box, colour and print '
        + 'time belong on the revision — geometry changes change them.'));
    }
    K.rev.attributes = ad;

    // Routing: the ORDERED WORK that makes this revision (plm/fab.py).
    // Read-only here on purpose — a routing is written by the agent that worked
    // out the geometry, and a prompt() per operation param would be a worse
    // editor than the MCP call. Rendered only when there is work to show, and
    // every string comes from the server's registry gloss (label, summary), so
    // this block knows nothing about any particular operation verb.
    const routing = selected.operations || [];
    if (routing.length) {
      const rd = el('div', 'doc');
      const rh = el('h3', null, 'Routing');
      const proc = (attrs.process || {}).value || '';
      if (proc) rh.appendChild(el('span', 'right', proc.toUpperCase()));
      rd.appendChild(rh);
      const rt = el('table', 'led');
      rt.innerHTML = '<thead><tr><th>Op</th><th>Work</th><th>Where</th></tr></thead>';
      const rtb = el('tbody'); rt.appendChild(rtb);
      for (const o of routing) {
        const tr = el('tr');
        const opc = el('td', 'ver', o.op);
        if (o.consumes_stock) opc.title = 'consumes raw stock — this is where a '
          + 'stick of material becomes this part';
        const work = el('td', null, o.summary || o.label);
        if (o.notes) {
          work.appendChild(el('div', 'dim', o.notes));
        }
        tr.append(opc, work, el('td', 'dim', o.work_center || '—'));
        rtb.appendChild(tr);
      }
      rd.appendChild(rt);
      K.rev.routing = rd;
    }

    // files
    const files = item.files[selected.id] || [];
    // ---- flat pattern: a sewing pattern / cut profile on this revision -------
    // The *.flat.json model of record, featured full width under the columns so
    // a 110-inch boxing strip has room to be seen. WHICH file is the server's
    // answer (`kind: 'pattern'`, plm/roles.py MEDIA_KINDS) and HOW it is drawn
    // is the registered widget — the same one the lightbox mounts.
    const flatF = files.find(f => f.kind === 'pattern');
    const oldFlat = document.getElementById('flatcard');
    if (oldFlat) oldFlat.remove();
    K.rev.flat = null;
    if (flatF && WIDGETS[flatF.view]) {
      const fc = el('div', 'doc'); fc.id = 'flatcard';
      const fh3 = el('h3', null, 'Flat pattern');
      fh3.appendChild(el('span', 'right', fileName(flatF)));
      fc.appendChild(fh3);
      const host = el('div'); fc.appendChild(host);
      host.appendChild(el('div', 'empty', 'loading pattern…'));
      const fctx = { number: item.number, files };
      Promise.resolve(WIDGETS[flatF.view].mount(host, { file: flatF, ctx: fctx,
        onFull: () => openFile(flatF, fctx) }))
        .catch(e => { host.innerHTML = ''; host.appendChild(el('div', 'empty', 'Could not load the pattern: ' + e.message)); });
      K.rev.flat = fc;
    }
    const fd = el('div', 'doc');
    fd.innerHTML = '<h3>Files</h3>';
    if (selected.state === 'DRAFT') {
      // Working revision: files may be replaced in place, no version bump.
      const fh = fd.querySelector('h3');
      const fr = el('span', 'right');
      const inp = el('input'); inp.type = 'file'; inp.multiple = true;
      inp.style.display = 'none';
      const up = el('button', 'btn', 'Upload (no version bump)');
      up.title = 'DRAFT revisions are working revisions — files land in place; git keeps the history';
      up.onclick = () => inp.click();
      inp.onchange = async () => {
        if (!inp.files.length) return;
        const payload = [];
        for (const f of inp.files) {
          const buf = await f.arrayBuffer();
          let bin = '';
          const bytes = new Uint8Array(buf);
          for (let i = 0; i < bytes.length; i += 0x8000) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
          }
          payload.push({ name: f.name, content_b64: btoa(bin) });
        }
        try {
          await post(`/api/items/${item.number}/files`,
            { files: payload, version: selected.version, actor: actorName() });
          toast(`${payload.length} file(s) updated on v${selected.version}`);
          renderItem(item.number, { fresh: true, ref, tab });
        } catch (e) { toast(e.message); }
      };
      fr.append(up, inp);
      fh.appendChild(fr);
    }
    if (files.length) {
      // ⭐ ONE ROW BUILDER, FIVE SURFACES. This list, the repo tree, the
      // externals, the compare rows and the lightbox all render from the same
      // `{kind, preview, view, diff, actions}` block the server puts on every
      // file payload — so the Record tab cannot disagree with the tree about
      // what a file is, or about what may be done with it.
      const tbl = el('table', 'gtree frows');
      const tb = el('tbody'); tbl.appendChild(tb);
      const rowCtx = { number: item.number, files, refresh: () =>
        renderItem(item.number, { fresh: true, ref, tab }) };
      for (const f of files) tb.appendChild(fileRow(f, rowCtx));
      fd.appendChild(tbl);
    } else {
      fd.appendChild(el('div', 'empty', 'No files recorded for this revision.'));
    }
    K.rev.files = fd;

    // BOM
    const lines = item.bom[selected.id] || [];
    const bd = el('div', 'doc');
    const bh = el('h3', null, `Bill of materials`);
    bd.appendChild(bh);
    if (lines.length) {
      // Three ways to read a structure (Teamcenter PSE-style): the nested
      // tree you can walk into, the flattened rollup, and the purchasing cut.
      const modes = el('span', 'right');
      for (const [label, m] of [['Tree', 'tree'], ['Flattened', 'flat'], ['Purchasing', 'buy']]) {
        const b = el('button', 'btn' + (bomMode === m ? ' primary' : ''), label);
        b.onclick = () => { bomMode = m; drawSelected(); };
        modes.appendChild(b);
      }
      bh.appendChild(modes);
      const bt = el('table', 'led');
      const bb = el('tbody');
      // One cell builder for every BOM view. It renders the row's OWN `thumb`
      // (null ⇒ the quiet kind chip, zero requests) — the row already knows,
      // because the server put the answer in the payload.
      const thumbCell = (thumb, kind) => {
        const td = el('td', 'bomthumb');
        const img = thumbImg(thumb, () => img.replaceWith(
          el('span', 'nothumb', (kind || '').toUpperCase().slice(0, 3) || '—')),
          { label: '…' });
        td.appendChild(img);
        return td;
      };

      if (bomMode === 'tree') {
        bt.innerHTML = '<thead><tr><th></th><th>Item</th><th>Rev</th><th>Qty</th><th></th></tr></thead>';
        bt.appendChild(bb);
        const addRows = (rows, depth, anchor) => {
          let after = anchor;
          for (const l of rows) {
            const tr = el('tr');
            tr.dataset.depth = depth;
            if (l.suppressed) tr.classList.add('sup');
            const td = el('td');
            td.style.paddingLeft = (12 + depth * 20) + 'px';
            const expandable = l.child_kind === 'assembly';
            const caret = el('span', 'caret2', expandable ? '▸' : '·');
            const a = el('a', 'num', l.child_number); a.href = itemPath(l.child_number);
            td.append(caret, a, el('span', 'dim', '  ' + (l.child_name || '')));
            if (expandable) {
              caret.style.cursor = 'pointer';
              let open = false;
              caret.onclick = async (e) => {
                e.preventDefault(); e.stopPropagation();
                if (open) {
                  let n = tr.nextSibling;
                  while (n && Number(n.dataset.depth) > depth) { const x = n; n = n.nextSibling; x.remove(); }
                  caret.textContent = '▸'; open = false; return;
                }
                const kid = await api(`/api/items/${l.child_number}/bom` +
                  (l.child_version ? `?version=${encodeURIComponent(l.child_version)}` : ''))
                  .catch(() => []);
                if (!kid.length) { caret.textContent = '·'; return; }
                addRows(kid, depth + 1, tr);
                caret.textContent = '▾'; open = true;
              };
            }
            const act = el('td');
            if (depth === 0) {  // suppression belongs to THIS revision's occurrences
              const sb = el('button', 'btn tiny', l.suppressed ? '↩' : '⊘');
              sb.title = l.suppressed ? 'Unsuppress this occurrence'
                : 'Suppress: stays in the record, excluded from roll-up and viewer';
              sb.onclick = async () => {
                try {
                  await post(`/api/bom/${l.id}`, { suppressed: !l.suppressed, actor: actorName() }, 'PATCH');
                  toast(l.suppressed ? 'Unsuppressed' : 'Suppressed');
                  renderItem(item.number, { fresh: true, ref, tab });
                } catch (e) { toast(e.message); }
              };
              act.appendChild(sb);
            }
            tr.append(thumbCell(l.thumb, l.child_kind), td,
                      el('td', 'ver', l.child_version ? 'v' + l.child_version : '—'),
                      el('td', null, `${l.qty} ${l.unit}`), act);
            if (after) { after.after(tr); after = tr; } else bb.appendChild(tr);
          }
        };
        addRows(lines, 0, null);
      } else if (bomMode === 'flat') {
        bt.innerHTML = '<thead><tr><th></th><th>Item</th><th>Name</th><th>Rev</th><th>Total qty</th></tr></thead>';
        bt.appendChild(bb);
        api(`/api/items/${item.number}/bom?version=${selected.version}&rolled=1`).then(flat => {
          for (const l of flat) {
            const tr = el('tr');
            const a = el('a', 'num', l.number); a.href = itemPath(l.number);
            const td = el('td'); td.appendChild(a);
            td.title = l.paths.join('\n');
            tr.append(thumbCell(l.thumb, l.kind), td,
                      el('td', null, l.name || ''),
                      el('td', 'ver', l.version ? 'v' + l.version : '—'),
                      el('td', null, `${l.qty} ${l.unit}`));
            bb.appendChild(tr);
          }
        });
      } else {  // purchasing
        bt.innerHTML = '<thead><tr><th>Item</th><th>Name</th><th>Total qty</th><th>Source</th></tr></thead>';
        bt.appendChild(bb);
        api(`/api/items/${item.number}/plan?version=${selected.version}`).then(plan => {
          for (const l of plan.buy) {
            const tr = el('tr');
            const a = el('a', 'num', l.number); a.href = itemPath(l.number);
            const td = el('td'); td.appendChild(a);
            const src = el('td', 'dim');
            if (l.source && l.source.url) {
              src.appendChild(extLink(
                l.source.url,
                `${l.source.supplier || 'link'} — ${l.source.mpn || ''}`.trim()));
            } else src.textContent = l.source
              ? `${l.source.supplier || ''} ${l.source.mpn || ''}`.trim() || '—' : '—';
            tr.append(td, el('td', null, l.name || ''),
                      el('td', null, `${l.qty} ${l.unit}`), src);
            bb.appendChild(tr);
          }
          if (!plan.buy.length) bb.appendChild(el('tr')).appendChild(
            el('td', 'empty', 'Nothing purchased in this tree.'));
        });
      }
      bd.appendChild(bt);
    } else {
      bd.appendChild(el('div', 'empty',
        item.kind === 'assembly' ? 'No BOM checked in for this revision.' : 'Not an assembly — no BOM.'));
    }
    // STAGE 19 — purchase REQUIREMENTS: `[[bom]] requirement = …` lines with
    // no path. Bought, but nothing selected yet; said so in so many words.
    if (selected) reqCard(bd, item.number, selected.version);
    K.rev.bom = bd;

    // Related items: the OTHER structure — typed links rather than
    // occurrences. Deliberately beside the BOM, because "what verifies this"
    // is the question you ask right after "what is in this".
    K.rev.related = relatedCard(item, selected ? selected.version : '',
                                () => renderItem(item.number, { fresh: true, ref, tab }));

    // Inline document: document items render their primary markdown; any
    // revision carrying work instructions renders those. The registry should
    // be readable, not just a file index.
    // The KIND comes from the server (plm/roles.py), not from the extension;
    // WHICH markdown file to feature is a question about this item's purpose,
    // which is the one thing a name can still answer.
    const mdFile = files.find(f => f.kind === 'markdown' &&
      (item.kind === 'document' ? !/MANIFEST/i.test(f.path)
                                : /instruction/i.test(f.path)));
    if (mdFile) {
      const dd = el('div', 'doc');
      /* ⛔ M10 — the ONE innerHTML site in 7,000 lines where an
       * attacker-controlled STRING reached the sink. Git allows `<`, `>` and
       * spaces in path names, so `<img src=x onerror=…>.md` was a committed
       * filename away. `el()` uses textContent, which is why every other
       * innerHTML here takes a number or a server-computed value. */
      dd.appendChild(el('h3', null,
                        'Document — ' + mdFile.path.split('/').pop()));
      // Same two affordances as a file row: read it big, or take it with you.
      const dr = el('span', 'right');
      const big = el('button', 'btn', 'Open');
      big.title = 'Read it full width';
      big.onclick = () => openFile(mdFile, { number: item.number });
      const ddl = el('a', 'btn', 'Download');
      ddl.href = blobUrl(mdFile); ddl.download = fileName(mdFile);
      ddl.style.textDecoration = 'none';
      dr.append(big, ddl);
      dd.querySelector('h3').appendChild(dr);
      const body = el('div', 'mddoc');
      body.textContent = 'loading…';
      dd.appendChild(body);
      fetch(blobUrl(mdFile))
        .then(r => r.ok ? r.text() : Promise.reject(new Error(r.statusText)))
        .then(text => { body.innerHTML = mdToHtml(text); })
        .catch(e => { body.textContent = 'could not load: ' + e.message; });
      K.rev.document = dd;
    }

    // Build plan: the two shop questions, for assemblies.
    if (item.kind === 'assembly' && lines.length) {
      const pd = el('div', 'doc plan');
      pd.innerHTML = '<h3>Build plan</h3>';
      const body = el('div');
      pd.appendChild(body);
      body.appendChild(el('div', 'empty', 'computing…'));
      api(`/api/items/${item.number}/plan?version=${encodeURIComponent(selected.version)}`)
        .then(plan => {
          body.innerHTML = '';
          // Material rollup: what to order, waste included.
          for (const m of plan.materials || []) {
            const line = el('div', 'matline');
            line.innerHTML = `<b>${escapeHtml(m.material)}</b> — ${m.net_g} g net · ` +
              `~<b>${m.with_waste_g} g</b> with ${plan.waste_pct}% waste · ${m.parts} part(s)` +
              (m.unknown_mass ? `  <span class="warn">⚠ ${m.unknown_mass} without mass data</span>` : '');
            body.appendChild(line);
          }
          const pt = el('table', 'led');
          pt.innerHTML = '<thead><tr><th>Print</th><th>Qty</th><th>Rev</th><th>Mass</th><th>Geometry</th></tr></thead>';
          const ptb = el('tbody'); pt.appendChild(ptb);
          for (const l of plan.print) {
            const tr = el('tr');
            const a = el('a', 'num', l.number); a.href = itemPath(l.number);
            const td = el('td'); td.append(a, document.createTextNode('  ' + (l.name || '')));
            const mass = l.mass_g ? `${Math.round(l.mass_g * l.qty)} g` +
              (l.material ? ` ${l.material}` : '') : '—';
            tr.append(td, el('td', null, `${l.qty} ${l.unit}`),
                      el('td', 'ver', l.version ? 'v' + l.version : 'latest'),
                      el('td', 'dim', mass),
                      el('td', l.has_stl ? 'ok' : 'missing', l.has_stl ? 'STL ✓' : 'no STL'));
            ptb.appendChild(tr);
          }
          if (!plan.print.length) ptb.appendChild(el('tr')).appendChild(
            el('td', 'empty', 'Nothing printable in this tree.'));
          body.appendChild(pt);

          // MAKE: fabricated pieces and weldments. Rendered only when
          // the tree has any, so a lantern's plan looks exactly as it did.
          if (plan.make && plan.make.length) {
            const mt = el('table', 'led');
            mt.innerHTML = '<thead><tr><th>Make</th><th>Qty</th><th>Cut</th>'
              + '<th>Stock</th><th>Work</th></tr></thead>';
            const mtb = el('tbody'); mt.appendChild(mtb);
            for (const l of plan.make) {
              const tr = el('tr');
              const a = el('a', 'num', l.number); a.href = itemPath(l.number);
              const td = el('td');
              td.append(a, document.createTextNode('  ' + (l.name || '')));
              if (l.weldment) td.appendChild(el('span', 'dim', '  weldment'));
              const cutP = l.cut && l.cut.params && l.cut.params.length;
              const cut = el('td', 'ver', cutP
                ? `${cutP.value} ${cutP.unit || ''}`.trim() : '—');
              const st = el('td', 'dim');
              if (l.stock) {
                const sa = el('a', 'num', l.stock.stock_profile || l.stock.number);
                sa.href = itemPath(l.stock.number);
                sa.title = l.stock.stock_section || l.stock.name || '';
                st.appendChild(sa);
              } else st.textContent = l.weldment ? '—' : 'no cut_from link';
              tr.append(td, el('td', null, `${l.qty} ${l.unit}`), cut, st,
                        el('td', 'dim',
                           (l.operations || []).map(o => o.op).join(' → ') || '—'));
              mtb.appendChild(tr);
            }
            body.appendChild(mt);
          }

          // The cut list rides along with the plan: stick counts and waste per
          // stock profile, which is the question a make list stops one short of.
          if (plan.cutlist) {
            for (const g of plan.cutlist.stock) {
              const line = el('div', 'matline');
              line.innerHTML =
                `<b>${escapeHtml(g.stock_profile || g.stock_number)}</b> — `
                + `<b>${g.stick_count} × ${g.stock_length} ${g.unit}</b> `
                + `(${escapeHtml(g.stock_number)}) · ${g.piece_count} pieces · `
                + `yield ${g.yield_pct}% · waste ${g.waste_pct}% · `
                + `usable remnant ${g.usable_remnant_total} ${g.unit}`;
              line.title = g.sticks.map(s =>
                `stick ${s.index}: ` + s.cuts.map(c => `${c.number} @ ${c.length}`).join(' + ')
                + ` → remnant ${s.remnant}${s.remnant_usable ? ' (usable)' : ''}`).join('\n');
              body.appendChild(line);
            }
            for (const i of plan.cutlist.issues) {
              body.appendChild(el('div', 'matline warn',
                `⚠ ${i.number}: ${i.problem}`));
            }
            const cl = el('div', 'note');
            const cla = el('a', 'btn',
              `Cut list (${plan.cutlist.totals.sticks} sticks)`);
            cla.href = `/api/items/${item.number}/cutlist.md`
              + `?version=${encodeURIComponent(selected.version)}`;
            cla.target = '_blank'; cla.style.textDecoration = 'none';
            cla.title = 'The generated cut list and nest, as markdown — '
              + plan.cutlist.algorithm.name;
            cl.appendChild(cla);
            body.appendChild(cl);
          }

          const bt2 = el('table', 'led');
          bt2.innerHTML = '<thead><tr><th>Buy</th><th>Qty</th><th>Source</th></tr></thead>';
          const btb = el('tbody'); bt2.appendChild(btb);
          for (const l of plan.buy) {
            const tr = el('tr');
            const a = el('a', 'num', l.number); a.href = itemPath(l.number);
            const td = el('td'); td.append(a, document.createTextNode('  ' + (l.name || '')));
            const src = el('td', 'dim');
            if (l.source && l.source.url) {
              src.appendChild(extLink(
                l.source.url,
                `${l.source.supplier || 'link'} — ${l.source.mpn || ''}`.trim()));
            } else if (l.source) {
              src.textContent = `${l.source.supplier || ''} ${l.source.mpn || ''}`.trim() || '—';
            } else src.textContent = '—';
            tr.append(td, el('td', null, `${l.qty} ${l.unit}`), src);
            btb.appendChild(tr);
          }
          for (const r of plan.requirements || []) {
            const tr = el('tr', 'reqrow');
            const td = el('td');
            td.append(el('span', 'pill warn', 'requirement — not selected'),
                      document.createTextNode('  ' + r.requirement));
            tr.append(td, el('td', null, `${r.qty} ${r.unit}`),
                      el('td', 'dim', (r.candidates || []).length
                        ? 'candidates: ' + r.candidates.join(', ') : 'no candidate yet'));
            btb.appendChild(tr);
          }
          if (!plan.buy.length && !(plan.requirements || []).length) btb.appendChild(el('tr')).appendChild(
            el('td', 'empty', 'Nothing to buy in this tree.'));
          body.appendChild(bt2);
        })
        .catch(e => { body.innerHTML = ''; body.appendChild(el('div', 'empty', 'plan failed: ' + e.message)); });
      K.rev.plan = pd;
    }

    // Context: the why. Item-level entries plus the selected revision's.
    const cd = el('div', 'doc');
    cd.innerHTML = '<h3>Context</h3>';
    const entries = item.context.filter(c =>
      !c.revision_id || c.revision_id === selected.id);
    for (const c of entries) {
      const e = el('div', 'ctx');
      const meta = el('div', 'meta');
      meta.append(
        el('span', null, c.revision_id ? `v${c.version}` : 'item'),
        el('span', null, c.actor || '—'),
        when(c.created_at));
      const body = el('div', 'body', c.body);
      e.append(meta, body);
      cd.appendChild(e);
    }
    if (!entries.length) cd.appendChild(el('div', 'empty',
      'No context yet. Record the why — searches will find it later.'));
    const add = el('div', 'ctxadd');
    const ta = el('textarea');
    ta.placeholder = 'Why did this change? What did we learn? (markdown)';
    const row = el('div');
    row.style.cssText = 'display:flex;gap:8px;margin-top:7px;align-items:center';
    const scope = el('select', 'btn');
    scope.appendChild(new Option(`this revision (v${selected.version})`, selected.version));
    scope.appendChild(new Option('the item', ''));
    const save = el('button', 'btn primary', 'Add context');
    save.onclick = async () => {
      if (!ta.value.trim()) return;
      try {
        await post(`/api/items/${item.number}/context`,
          { body: ta.value, version: scope.value, actor: actorName() });
        toast('Context recorded');
        renderItem(item.number, { fresh: true, ref, tab });
      } catch (e) { toast(e.message); }
    };
    row.append(scope, save);
    add.append(ta, row);
    cd.appendChild(add);
    K.rev.context = cd;
    layout();
  }

  /* ══ the repo bar, the tabs, and the layout ═══════════════════════════════
   *
   * GitHub's information architecture, not its skin: identity at the top, the
   * repo's controls directly under it where you cannot miss them, tabs for the
   * different questions, and a repo home that is two columns — the code (here,
   * the geometry and the tree) on the left, what-you-need-to-know on the right.
   *
   * It exists because the git surface was BELOW THE FOLD. Geoff found the
   * compare view only from a URL somebody handed him: "how do I get to that
   * screen from the main part page — it should be similar to how GitHub does
   * it."
   */
  const barMount = el('div'); barMount.id = 'repobarTop';
  const tabsMount = el('div'); tabsMount.id = 'itabs';
  const bodyMount = el('div'); bodyMount.id = 'itabbody';
  page.append(barMount, tabsMount, bodyMount);

  const go = (t, r) => {
    const rr = r === undefined ? ref : r;
    navigate(t === 'part'
      ? (rr && rr !== defRef ? treePath(number, rr) : itemPath(number))
      : tabPath(number, t));
  };

  function topBar() {
    barMount.innerHTML = '';
    if (gitOff) return;      // no repository here, so no repo band
    if (!info || info.error) {
      const b = el('div', 'repobar flat');
      b.appendChild(el('span', 'note', /no repo/i.test(info && info.error || '')
        ? `No repo of its own — ${number} is a catalog item and lives in `
          + `catalog.git as ${number}.toml (one shared repo, not a repo per screw).`
        : `Repository unavailable: ${(info && info.error) || 'unknown'}`));
      barMount.appendChild(b);
      return;
    }
    const b = el('div', 'repobar flat');
    // Ref picker. REPO may not be mounted yet (other tabs never mount the
    // band), so the popover is driven from `info` directly.
    const wrap = el('span', 'refwrap');
    const btn = el('button', 'refbtn');
    btn.append(el('span', 'lbl', 'Ref'), el('span', 'rf', ref || info.default_ref),
               el('span', 'caret', '▾'));
    btn.title = 'Pick a branch or a tag — the whole Part tab re-reads at it';
    btn.disabled = !info.counts.commits;
    btn.onclick = () => popToggle(wrap, () => barRefPop(), 'refpop');
    wrap.appendChild(btn);
    b.appendChild(wrap);

    // GitHub's "123 commits / 12 tags": counts that are LINKS, because the
    // number is only interesting as a way in.
    const c = info.counts;
    const cnt = el('span', 'counts');
    const lk = (n, word, t) => {
      const pl = word === 'branch' ? 'es' : 's';
      const a = el('a', null, `${n} ${word}${n === 1 ? '' : pl}`);
      a.href = tabPath(number, t);
      return a;
    };
    const nBr = branchCount(info);
    cnt.append(lk(nBr, 'branch', 'drafts'), document.createTextNode(' · '),
               lk(c.releases, 'tag', 'releases'), document.createTextNode(' · '),
               lk(c.commits, 'commit', 'history'));
    b.appendChild(cnt);
    // PLM_GIT_MODE=read (stage 9): this repository is a projection of the
    // registry that follows its writes (plm/gitsync.py). Say so — and when a
    // write is newer than the last sync, say THAT, rather than presenting old
    // refs as the present. The page re-reads itself until the sync lands.
    if (info.git_mode === 'read') {
      const sy = info.sync || {};
      const chip = el('span', 'syncchip ' + (sy.state || 'synced'));
      chip.title = (info.git_mode_label || '') + '\n\nA git push to this '
        + 'project is refused with instructions; write through plm.';
      if (sy.state === 'syncing') {
        chip.textContent = 'read-only · syncing… as of '
          + (sy.as_of ? fmtAgo(sy.as_of) : 'the migration');
        if ((at.syncTries || 0) < 12) {
          setTimeout(() => {
            if (CURRENT_ITEM === number) renderItem(number,
              { fresh: true, ref: at.ref, tab: at.tab, syncTries: (at.syncTries || 0) + 1 });
          }, 2500);
        }
      } else if (sy.state === 'error') {
        chip.textContent = 'read-only · sync FAILED as of '
          + (sy.as_of ? fmtAgo(sy.as_of) : '—');
        chip.title += '\n\n' + (sy.error || '');
      } else {
        chip.textContent = 'read-only · in sync'
          + (sy.as_of ? ' as of ' + fmtAgo(sy.as_of) : '');
      }
      b.appendChild(chip);
    }
    b.appendChild(el('span', 'spacer'));

    const cw = el('span', 'refwrap');
    const cb = el('button', 'btn', 'Compare ▾');
    cb.title = 'Diff two refs — files, BOM, attributes and geometry';
    cb.disabled = !info.counts.commits;
    cb.onclick = () => popToggle(cw, () => comparePop(number, info, ref), 'cmppop');
    cw.appendChild(cb);
    b.appendChild(cw);

    const kw = el('span', 'refwrap');
    const kb = el('button', 'btn', 'Clone ▾');
    kb.onclick = () => popToggle(kw, () => clonePopFor(info.clone_url, number),
                                 'clonepop');
    kw.appendChild(kb);
    b.appendChild(kb.parentNode === kw ? kw : kw);
    barMount.appendChild(b);
  }

  /* The ref popover on the top bar. Same two tabs and the same rows as the
   * one in the band — one definition of what a ref list looks like. */
  function barRefPop() {
    const saved = REPO;
    REPO = { number, info, ref, tab: (saved && saved.tab) || '' };
    const pop = refPop();
    REPO = saved;
    return pop;
  }

  function tabs() {
    tabsMount.innerHTML = '';
    const nav = el('nav', 'itabs');
    const c = (info && info.counts) || { releases: 0, drafts: 0, commits: 0 };
    const nBranches = info && !info.error ? branchCount(info) : 0;
    const nBom = (item.bom[selected && selected.id] || []).length || '';
    const nCtx = (item.context || []).length || '';
    const defs = gitOff ? [
      ['record', 'Part', ''],
      ['bom', 'BOM', nBom],
      ['context', 'Context', nCtx],
    ] : [
      ['part', 'Part', ''],
      ['bom', 'BOM', nBom],
      // GitHub's words (Geoff, 2026-10-24): a draft IS a branch (draft/<topic>),
      // a release IS a tag (vX.Y.Z). `main` counts as a branch once it exists.
      ['drafts', 'Branches', nBranches || ''],
      ['releases', 'Tags', c.releases || ''],
      ['pulls', 'Pull requests', ''],          // the count arrives below (U.openPulls)
      ['history', 'History', item.revisions.length || ''],
      ['record', 'Record', ''],
      ['context', 'Context', nCtx],
    ];
    for (const [key, label, count] of defs) {
      const a = el('a', 'itab' + (key === tab ? ' on' : ''), label);
      a.href = key === 'part' || (gitOff && key === 'record')
        ? (onDefault ? itemPath(number) : treePath(number, ref))
        : tabPath(number, key, onDefault ? '' : ref);
      if (count !== '') a.appendChild(el('span', 'n', String(count)));
      if (key === 'pulls' && window.CHANGES_UI) {
        const n = el('span', 'n');
        CHANGES_UI.openPulls({ number }).then(k => { if (k) { n.textContent = String(k); a.appendChild(n); } });
      }
      nav.appendChild(a);
    }
    tabsMount.appendChild(nav);
  }

  /* "You are looking at v0.2.0, which is not the latest release." GitHub puts
   * this on every tag view, and it is the difference between reading history
   * and thinking you are reading the present. */
  function refBanner() {
    if (onDefault || !info || info.error) return null;
    const n = el('div', 'refbanner');
    n.append(el('span', 'lbl', 'Viewing'), el('span', 'rf', ref));
    const why = revOf(ref)
      ? ` — v${revOf(ref).version}, ${revOf(ref).state.toLowerCase()}`
      : ' — no revision in the database matches this ref';
    n.append(document.createTextNode(why + '. This is not '
      + (info.default_release ? 'the latest release' : 'the default ref') + '.'));
    // A draft ref is a PROPOSAL, and the thing you want from it is the review,
    // not the file tree. The banner is where you already are when you notice
    // you are looking at one.
    const isDraft = (info.drafts || []).some(d => d.ref === ref);
    if (isDraft) {
      const links = el('span', 'props');
      if (info.default_release) {
        const c = el('a', null, 'what does it change?');
        c.href = cmpPath(number, info.default_release, ref);
        links.appendChild(c);
      }
      n.appendChild(links);
      // Which proposal, if any, is open on it — one call, only on a draft ref.
      repoGet(number, 'drafts').then(out => {
        if (CURRENT_ITEM !== number) return;
        const d = (out.drafts || []).find(x => x.ref === ref);
        if (!d || !d.proposal || d.proposal.decision) {
          links.appendChild(el('span', 'note',
            '· pushed, not submitted — the Branches tab can submit it'));
          return;
        }
        const r = el('a', null, d.review ? `review proposal ${PLM_ROUTES.proposalLabel(d.review.id)} →` : 'review proposal →');
        r.href = roundPath(number, d.proposal.id);
        links.appendChild(r);
      }).catch(() => {});
    }
    const a = el('a', null, `back to ${defRef}`);
    a.href = itemPath(number);
    n.appendChild(a);
    return n;
  }

  function layout() {
    topBar();
    tabs();
    bodyMount.innerHTML = '';
    // The banner belongs on every REF-SCOPED tab. Releases, Drafts and History
    // are lists of refs and are ref-independent by nature, so a "you are
    // looking at v0.2.0" on them would be a claim about nothing.
    const ban = refBanner();
    if (ban && ['part', 'bom', 'record', 'context'].includes(tab)) {
      bodyMount.appendChild(ban);
    }
    const put = (host, ...nodes) => {
      for (const n of nodes) if (n) host.appendChild(n);
    };
    if (tab === 'part') {
      const cols = el('div', 'cols repo');
      const main = el('div'), side = el('div');
      cols.append(main, side);
      bodyMount.appendChild(cols);
      // MAIN: what the thing IS — the actions on it, its geometry, its tree.
      put(main, bar, K.fixed.hero, K.rev.flat, K.rev.norev);
      const repoMount = el('div'); repoMount.id = 'repo';
      main.appendChild(repoMount);
      if (info && !info.error && info.counts.commits) {
        mountRepo(repoMount, number, ref, at.path || '', { bare: true, open: at.open || '' });
      } else if (info && info.error) {
        repoAbsent(repoMount, number, { message: info.error });
      }
      // SIDE: what you need to know about it.
      put(side, aboutCard(), releasesCard(), draftsCard(),
          K.fixed.whereused, K.rev.related, bomSummaryCard(), K.rev.document);
    } else if (tab === 'bom') {
      put(bodyMount, K.rev.bom, K.rev.plan, K.fixed.whereused);
    } else if (tab === 'releases') {
      put(bodyMount, releasesTab());
    } else if (tab === 'drafts') {
      put(bodyMount, draftsTab());
    } else if (tab === 'pulls' && window.CHANGES_UI) {
      put(bodyMount, CHANGES_UI.pullList({ number }));
    } else if (tab === 'history') {
      put(bodyMount, historyTab());
    } else if (tab === 'record' && gitOff) {
      // The registry's own page (PLM_GIT_MODE=off): the actions, the geometry
      // and the revision record on the left; files and what it relates to on
      // the right; the flat pattern full width under both — master's layout.
      const cols = el('div', 'cols');
      const main = el('div'), side = el('div');
      cols.append(main, side);
      bodyMount.appendChild(cols);
      put(main, bar, K.fixed.hero, K.rev.norev, K.rev.revhead, K.rev.attributes,
          K.rev.routing, K.fixed.sourcing);
      put(side, K.rev.files, K.rev.tags, K.rev.related, K.fixed.whereused,
          K.rev.document, K.fixed.olddiff);
      put(bodyMount, K.rev.flat);
    } else if (tab === 'record') {
      const cols = el('div', 'cols');
      const main = el('div'), side = el('div');
      cols.append(main, side);
      bodyMount.appendChild(cols);
      put(main, K.rev.revhead, K.rev.attributes, K.rev.routing, K.fixed.sourcing);
      put(side, K.rev.files, K.rev.tags, K.fixed.olddiff);
      put(bodyMount, K.rev.flat);
    } else if (tab === 'context') {
      put(bodyMount, K.rev.context);
    }
  }

  /* ── the Part tab's right-hand column ─────────────────────────────────── */

  // About, from plm.toml AT THE REF — the repo's own description of the part,
  // beside the repo's own tree. The DB's version of the same facts lives on
  // the Record tab; when they disagree that is a finding, not a bug.
  function aboutCard() {
    const card = el('div', 'doc');
    card.innerHTML = '<h3>About</h3>';
    const body = el('div', 'pad');
    card.appendChild(body);
    if (!info || info.error || !info.counts.commits) {
      body.appendChild(el('div', 'note', item.name || '(unnamed)'));
      return card;
    }
    body.appendChild(el('div', 'note', 'reading plm.toml…'));
    repoGet(number, 'manifest', `ref=${encodeURIComponent(ref)}`).then(({ manifest: m }) => {
      if (CURRENT_ITEM !== number) return;
      body.innerHTML = '';
      if (m.summary) body.appendChild(el('div', 'note', m.summary));
      const facts = el('table', 'led');
      const tb2 = el('tbody'); facts.appendChild(tb2);
      const attrs = m.attributes || {};
      const row = (k, v) => {
        if (v === '' || v == null) return;
        const tr = el('tr');
        tr.append(el('td', 'dim', k), el('td', 'ver', String(v)));
        tb2.appendChild(tr);
      };
      row('version', m.version && 'v' + m.version);
      row('kind', m.kind);
      row('project', m.project);
      row('process', m.process);
      for (const n of ['material', 'mass_g', 'assembled_bbox_mm',
                       'bbox_x_mm', 'bbox_y_mm', 'bbox_z_mm', 'print_time_min']) {
        const a = attrs[n];
        if (a) row(n, `${a.value}${a.unit ? ' ' + a.unit : ''}`);
      }
      if (tb2.children.length) body.appendChild(facts);
      if ((m.tags || []).length) {
        const w = el('div', 'tagrow');
        w.style.marginTop = '8px';
        for (const t of m.tags) w.appendChild(el('span', 'tagchip', t));
        body.appendChild(w);
      }
      const more = el('div', 'note');
      more.style.marginTop = '8px';
      const a = el('a', null, 'the whole record →');
      a.href = tabPath(number, 'record', onDefault ? '' : ref);
      more.appendChild(a);
      body.appendChild(more);
    }).catch(e => { body.innerHTML = ''; body.appendChild(
      el('div', 'note', 'No plm.toml at this ref: ' + e.message)); });
    return card;
  }

  function releasesCard() {
    if (!info || info.error) return null;
    const card = el('div', 'doc');
    const h = el('h3', null, 'Tags');
    if (info.releases.length) h.appendChild(el('span', 'right'))
      .appendChild(el('span', 'note', String(info.releases.length)));
    card.appendChild(h);
    if (!info.releases.length) {
      card.appendChild(el('div', 'empty',
        'Never released. Every revision here is still a draft, so this item has '
        + 'no tags and no main.'));
      return card;
    }
    card.appendChild(releaseRow(info.releases[0], info.releases[1], true));
    if (info.releases.length > 1) {
      const more = el('div', 'pad');
      const a = el('a', null, `+ ${info.releases.length - 1} release`
        + (info.releases.length === 2 ? '' : 's'));
      a.href = tabPath(number, 'releases');
      more.appendChild(a);
      card.appendChild(more);
    }
    return card;
  }

  function releaseRow(r, older, latest = false) {
    const box = el('div', 'relrow');
    const top = el('div', 'hd');
    const a = el('a', 'rf', r.ref);
    a.href = treePath(number, r.ref);
    // `state` here is what the revision IS NOW — the refs route folds
    // `refs/notes/plm` `[status]` over the tag annotation, because a promotion
    // never moves the tag and the rail would otherwise still say PROTOTYPE.
    top.append(a, stamp(r.state));
    if (latest) top.appendChild(el('span', 'latestchip', 'newest tag'));
    if (r.ref === (info && info.latest_released)) {
      const l = el('span', 'latestchip', 'latest release');
      l.title = 'The newest revision that is actually RELEASED — what a BOM '
        + 'line marked `track = "release"` follows.';
      top.appendChild(l);
    }
    if (r.status_source === 'note' && r.promoted) {
      const p = el('span', 'targetchip',
        `promoted by ${r.promoted.approver || '—'}`);
      p.title = `${r.promoted.from_maturity || '—'} → ${r.maturity} on `
        + `${r.promoted.date || '—'}, release #${r.promoted.release}. The `
        + 'decision is a note on the release commit; the tag did not move.';
      top.appendChild(p);
      const c = whoChip(r.promoted.approver_kind);
      if (c) top.appendChild(c);
    }
    top.appendChild(when(isoAt(r.date)));
    box.appendChild(top);
    if (r.summary) box.appendChild(el('div', 'notes', r.summary));
    const acts = el('div', 'acts');
    if (older) {
      const c = el('a', 'btn tiny', `compare to ${older.ref}`);
      c.href = cmpPath(number, older.ref, r.ref);
      acts.appendChild(c);
    }
    const b = el('a', 'btn tiny', 'browse files');
    b.href = treePath(number, r.ref);
    acts.appendChild(b);
    // ⭐ PROMOTE — the door §2.1b adds. A revision that was built and proven is
    // promoted in place: no new version, no file change, no fake revision.
    if (!r.released) {
      // `refwrap` is the positioning context every popover in this page hangs
      // off (`.pop` is absolute); a bare <button> is not one.
      const wrap = el('span', 'refwrap');
      const pr = el('button', 'btn tiny primary', 'Promote…');
      pr.type = 'button';
      pr.title = `Ask for v${r.version} to be promoted. No files change and no `
        + 'version is minted — it is a decision about this exact tag.';
      pr.onclick = (e) => {
        e.preventDefault(); e.stopPropagation();
        popToggle(wrap, () => promotePop(number, r), 'promote');
      };
      wrap.appendChild(pr);
      acts.appendChild(wrap);
    } else if (r.promoted && r.promoted.release) {
      const lk = el('a', 'btn tiny', `release #${r.promoted.release} →`);
      lk.href = relPath(number, r.promoted.release);
      acts.appendChild(lk);
    }
    box.appendChild(acts);
    return box;
  }

  /* The Promote popover: pick the maturity, read the notes plm drafted, send.
   * The notes are fetched only after the release exists (the draft is built
   * from what merged since the previous RELEASED revision, which needs the
   * release row to know where the range starts), so this asks for the promotion
   * and lands you on the page where you edit them. */
  function promotePop(n, r) {
    const box = el('div', 'pad');
    box.appendChild(el('div', 'note',
      `Promote ${n} v${r.version} — currently ${r.state}. This changes no `
      + 'files and mints no version: it asks for a decision about this exact '
      + 'tag, and the answer is recorded as a note on the release commit.'));
    const lbl = el('label', 'fld');
    lbl.appendChild(el('span', 'k', 'to maturity'));
    const sel = el('select', 'btn');
    for (const m of ['released', 'obsolete']) {
      if (m === (r.maturity || '').toLowerCase()) continue;
      sel.appendChild(new Option(m, m));
    }
    lbl.appendChild(sel);
    box.appendChild(lbl);
    const note = el('textarea', 'decnote');
    note.placeholder = 'Release notes — leave this empty and plm drafts them '
      + 'from what merged since the last release (you can edit them on the '
      + 'release page).';
    note.style.minHeight = '90px';
    box.appendChild(note);
    const acts = el('div', 'acts');
    const go = el('button', 'btn primary', 'Request the promotion');
    acts.appendChild(go);
    box.appendChild(acts);
    const out = el('div', 'out');
    box.appendChild(out);
    go.onclick = async () => {
      go.disabled = true;
      out.innerHTML = '';
      try {
        const res = await post(`/api/items/${encodeURIComponent(n)}/releases`,
          { version: r.version, to_maturity: sel.value,
            notes: note.value.trim(), actor: ACTOR });
        popClose();
        navigate(relPath(n, res.release.id, res.release.tag));
      } catch (e) {
        go.disabled = false;
        out.appendChild(refusalBox(e));
      }
    };
    return box;
  }

  function draftsCard() {
    if (!info || info.error || !info.drafts.length) return null;
    const card = el('div', 'doc');
    const h = el('h3', null, 'Branches');
    h.appendChild(el('span', 'right')).appendChild(
      el('span', 'note', `${info.drafts.length} open`));
    card.appendChild(h);
    const d = info.drafts[0];
    const box = el('div', 'relrow');
    const top = el('div', 'hd');
    const a = el('a', 'rf', d.ref);
    a.href = treePath(number, d.ref);
    top.append(a, stamp(d.state || 'DRAFT'), when(isoAt(d.date)));
    box.appendChild(top);
    if (d.summary) box.appendChild(el('div', 'notes', d.summary));
    // The PR-shaped question. A draft is only interesting next to what it
    // departs from.
    const base = info.default_release;
    if (base) {
      const acts = el('div', 'acts');
      const c = el('a', 'btn tiny', 'what does it change?');
      c.href = cmpPath(number, base, d.ref);
      c.title = `Compare ${base} → ${d.ref}`;
      acts.appendChild(c);
      box.appendChild(acts);
    }
    card.appendChild(box);
    if (info.drafts.length > 1) {
      const more = el('div', 'pad');
      const a2 = el('a', null, `+ ${info.drafts.length - 1} more`);
      a2.href = tabPath(number, 'drafts');
      more.appendChild(a2);
      card.appendChild(more);
    }
    return card;
  }

  function bomSummaryCard() {
    const lines = item.bom[selected && selected.id] || [];
    if (!lines.length) return null;
    const card = el('div', 'doc');
    card.innerHTML = '<h3>Bill of materials</h3>';
    const body = el('div', 'pad');
    const kinds = {};
    for (const l of lines) kinds[l.child_kind || 'part'] = (kinds[l.child_kind || 'part'] || 0) + 1;
    body.appendChild(el('div', 'note', `${lines.length} line`
      + `${lines.length === 1 ? '' : 's'} · `
      + Object.entries(kinds).map(([k, n]) => `${n} × ${k}`).join(' · ')));
    const a = el('a', null, 'the whole structure →');
    a.href = tabPath(number, 'bom', onDefault ? '' : ref);
    const p = el('div', 'note'); p.style.marginTop = '6px';
    p.appendChild(a);
    body.appendChild(p);
    card.appendChild(body);
    return card;
  }

  /* ── whole tabs ───────────────────────────────────────────────────────── */

  function releasesTab() {
    const card = el('div', 'doc');
    card.innerHTML = '<h3>Tags</h3>';
    if (!info || info.error) {
      card.appendChild(el('div', 'empty', 'No repo — nothing to release from.'));
      return card;
    }
    if (!info.releases.length) {
      card.appendChild(el('div', 'empty',
        `${number} has never been released. Its revisions are all drafts, so `
        + 'there are no tags; the Branches tab has them.'));
      return card;
    }
    for (let i = 0; i < info.releases.length; i++) {
      card.appendChild(releaseRow(info.releases[i], info.releases[i + 1], i === 0));
    }
    return card;
  }

  /* The Branches tab — every open proposal on THIS item.
   *
   * A draft branch is a proposal whether or not anybody has been asked to
   * decide about it: GitHub's draft PR. So one list, with the state on each row
   * (DRAFT = pushed, nobody asked yet; IN_REVIEW = a decision is pending) and
   * the two questions a reviewer has next — what does it change, and can I act
   * on it — as buttons rather than as a separate card.
   *
   * It used to end in the GLOBAL approval queue, which put other items'
   * submissions on this item's page. `/api/repo/<n>/drafts` carries each
   * branch's own approval, so the queue here is this item's by construction. */
  function draftsTab() {
    const wrap = el('div');
    const card = el('div', 'doc');
    const h = el('h3', null, 'Branches');
    card.appendChild(h);
    if (!info || info.error) {
      card.appendChild(el('div', 'empty',
        'No repo, so no branches — this item has nothing to propose against.'));
      wrap.appendChild(card);
      return wrap;
    }
    // `main` is a branch like on GitHub — the default one, released work only,
    // written by plm when a human approves. It exists once anything is released.
    if (info.main) card.appendChild(mainRow());
    if (!info.drafts.length) {
      card.appendChild(el('div', 'empty',
        'No draft branches. Every revision in this repo is released — push a '
        + 'draft/<topic> branch (Clone ▾ has the recipe) and it appears here '
        + 'as a proposal.'));
      wrap.appendChild(card);
      return wrap;
    }
    const body = el('div');
    body.appendChild(el('div', 'empty', 'reading the branches…'));
    card.appendChild(body);
    wrap.appendChild(card);
    repoGet(number, 'drafts').then(out => {
      if (CURRENT_ITEM !== number) return;
      body.innerHTML = '';
      h.appendChild(el('span', 'right')).appendChild(
        el('span', 'note', `${out.drafts.length} draft/* open`));
      for (const d of out.drafts) body.appendChild(draftRow(out, d));
    }).catch(e => {
      body.innerHTML = '';
      body.appendChild(el('div', 'empty', 'Could not read the branches: '
        + e.message));
    });
    return wrap;
  }

  /* The default branch as a row: where it points (the newest tag) and that
   * nothing lands on it except through an approval. */
  function mainRow() {
    const box = el('div', 'draftrow');
    const top = el('div', 'hd');
    const a = el('a', 'rf', 'main');
    a.href = treePath(number, 'main');
    a.title = 'Browse main';
    top.append(a, el('span', 'vchip', 'default'));
    const rt = el('span', 'rt');
    rt.appendChild(el('span', 'who', info.main.at ? `at ${info.main.at}` : info.main.commit.slice(0, 7)));
    top.appendChild(rt);
    box.appendChild(top);
    box.appendChild(el('div', 'notes',
      'Released work only. A push cannot move it; an approved pull request does.'));
    return box;
  }

  /* One proposal row. The numbers are the review's first question ("how big is
   * this?") and every one of them is a link to the answer. */
  function draftRow(out, d) {
    const box = el('div', 'draftrow');
    const top = el('div', 'hd');
    const a = el('a', 'rf', d.ref);
    a.href = treePath(number, d.ref);
    a.title = 'Browse this branch';
    top.append(a);
    if (d.version) top.appendChild(el('span', 'vchip', 'v' + d.version));
    top.appendChild(stamp((d.revision && d.revision.state) || d.state || 'DRAFT'));
    if (d.proposal && !d.proposal.decision) {
      top.appendChild(el('span', 'targetchip',
        '→ ' + (d.proposal.target_state || '').toLowerCase()));
    }
    const rt = el('span', 'rt');
    if (d.author) rt.appendChild(el('span', 'who', d.author));
    rt.appendChild(when(isoAt(d.committed || d.date)));
    top.appendChild(rt);
    box.appendChild(top);
    if (d.summary) box.appendChild(el('div', 'notes', d.summary));

    // "What does it change?" as a sentence of numbers, before the button that
    // opens the diff — so the row answers the cheap version of the question.
    const facts = el('div', 'facts');
    const s = d.stats;
    if (s) {
      const n = el('span', 'n');
      n.append(el('span', 'plus', `+${s.additions}`),
               document.createTextNode(' '),
               el('span', 'minus', `−${s.deletions}`));
      facts.append(el('span', null, `${s.files_changed} file`
        + `${s.files_changed === 1 ? '' : 's'}`), n);
      if (s.pins_changed) {
        facts.appendChild(el('span', 'pinbump',
          `${s.pins_changed} pin bump${s.pins_changed === 1 ? '' : 's'}`));
      }
    }
    if (d.ahead != null && d.counted_against) {
      const ab = el('span', 'ahead',
        `${d.ahead} ahead of ${d.counted_against}`
        + (d.behind ? ` · ${d.behind} behind` : ''));
      ab.title = `git rev-list --left-right --count ${d.counted_against}...${d.ref}`;
      facts.appendChild(ab);
    }
    if (d.commit) {
      const sha = el('span', 'sha', d.commit.slice(0, 10));
      sha.title = d.commit;
      facts.appendChild(sha);
    }
    // STAGE 13: the proposal (#n) this branch IS, and its threads — open and
    // BLOCKING counted, because a review you cannot see from the list is a
    // review somebody approves past.
    const rv = d.review;
    if (rv) {
      const pn = el('a', 'pnumlink', PLM_ROUTES.proposalLabel(rv.id));
      pn.href = pullPath(number, rv.id);
      pn.title = `proposal ${PLM_ROUTES.proposalLabel(rv.id)} — ${rv.state}`;
      facts.appendChild(pn);
      const tc = rv.threads || {};
      const th = el('span', 'thrbadge' + (tc.blocking ? ' blocks' : '')
        + (tc.open ? ' open' : ''),
        tc.open ? `💬 ${tc.open} open` + (tc.blocking ? ` · ${tc.blocking} blocking` : '')
                : (tc.total ? `💬 ${tc.total} resolved` : '💬 0'));
      th.title = 'Review threads on this proposal (open / blocking)';
      facts.appendChild(th);
    }
    const ph = el('span', 'soon');
    facts.appendChild(ph);
    if (window.CHECKS) CHECKS.rollupInto(ph, number, d.commit, { href: d.proposal && !d.proposal.decision ? roundPath(number, d.proposal.id) : '' });  // STAGE 6 HOOK (web/checks.js)
    box.appendChild(facts);

    const acts = el('div', 'acts');
    if (d.base) {
      const c = el('a', 'btn tiny primary', 'what does it change?');
      c.href = cmpPath(number, d.base, d.ref);
      c.title = `Compare ${d.base} → ${d.ref}`;
      acts.appendChild(c);
    }
    if (d.review) {
      const p = el('a', 'btn tiny', `${d.proposal && !d.proposal.decision
        ? 'Review' : 'Open'} proposal ${PLM_ROUTES.proposalLabel(d.review.id)} →`);
      p.href = pullPath(number, d.review.id);
      acts.appendChild(p);
    } else if (d.proposal && !d.proposal.decision) {
      const p = el('a', 'btn tiny', `Review proposal →`);
      p.href = roundPath(number, d.proposal.id);
      acts.appendChild(p);
    }
    const b = el('a', 'btn tiny', 'browse files');
    b.href = treePath(number, d.ref);
    acts.appendChild(b);
    // GitHub's "Compare & pull request": a draft branch in NO pull request
    // offers to open one — the banner's own action (web/changes.js)
    const rv2 = d.review;
    if (rv2 && rv2.change_id) {
      const pr = el('a', 'btn tiny', `in pull request ${PLM_ROUTES.ref(rv2.change_id)} →`);
      pr.href = PLM_ROUTES.P.pull(number, rv2.change_id);
      acts.appendChild(pr);
    } else if (rv2 && rv2.state === 'draft' && window.CHANGES_UI) {
      acts.appendChild(CHANGES_UI.openPullButton({ proposal: rv2.id, item: number,
        title: d.summary || d.ref, branch: d.ref, project: (item && item.project) || '',
        push: d.push }, box));
    }
    box.appendChild(acts);

    // A pushed draft nobody has submitted is GitHub's draft PR: real, diffable,
    // and not in anybody's queue until its author says it is ready.
    if (d.revision && !(d.proposal && !d.proposal.decision)) {
      box.appendChild(submitBar(d));
    } else if (!d.revision) {
      box.appendChild(el('div', 'why',
        'This branch has no revision in the registry — it was not indexed by a '
        + 'push. Browse and diff it; there is nothing to submit.'));
    }
    return box;
  }

  /* Submit for release, with the target-maturity picker, on the row itself.
   * The target is the whole question ("what are you claiming this IS?") and it
   * decides which gate the approval meets — §5.1 refuses RELEASED over an
   * unreleased child and only warns at PROTOTYPE. */
  function submitBar(d) {
    const bar = el('div', 'submitbar');
    bar.appendChild(el('span', 'lbl', 'Submit for release'));
    const sel = el('select', 'btn');
    for (const t of ['PROTOTYPE', 'RELEASED']) sel.appendChild(new Option(t, t));
    sel.title = 'What this revision would BE if approved';
    const note = el('input', 'subnote');
    note.placeholder = 'Note for the approver (optional)';
    const go = el('button', 'btn primary tiny', 'Submit');
    go.onclick = async () => {
      go.disabled = true;
      try {
        const r = await postSubmit(number,
          { version: d.revision.version, target_state: sel.value,
            note: note.value.trim(), actor: actorName() });
        const pid = r.proposal && r.proposal.id;
        toast(`${number} v${r.version} is ${r.state}`
          + (pid ? ` — proposal ${PLM_ROUTES.proposalLabel(pid)}` : ''));
        navigate(pid ? pullPath(number, pid) : roundPath(number, r.approval_id));
      } catch (e) {
        go.disabled = false;
        bar.appendChild(refusalBox(e));
      }
    };
    bar.append(sel, note, go);
    return bar;
  }

  /* One list, not two. The revision ledger and the commit log are the same
   * events seen from the two sides of the POC, so they are merged and every
   * row leads to the diff that made it — GitHub's "click a commit, see the
   * change", which is how Geoff expects to get into a diff. */
  function historyTab() {
    const card = el('div', 'doc');
    const h = el('h3', null, 'History');
    h.appendChild(el('span', 'right')).appendChild(
      el('span', 'note', 'revisions, newest first'));
    card.appendChild(h);
    const byVer = {};
    for (const r of item.revisions) byVer[r.version] = r;
    const rows = [];
    if (info && !info.error) {
      for (const r of [...info.releases, ...info.drafts]) {
        const v = r.ref.replace(/^draft\//, '').replace(/^v/, '');
        rows.push({ ref: r.ref, version: v, date: isoAt(r.date),
                    state: r.state, summary: r.summary, rev: byVer[v] || null,
                    commit: r.commit });
        delete byVer[v];
      }
    }
    for (const v of Object.keys(byVer)) {
      const r = byVer[v];
      rows.push({ ref: '', version: v, date: r.created_at, state: r.state,
                  summary: r.summary, rev: r, commit: '' });
    }
    rows.sort((a, b) => (b.date || 0) - (a.date || 0));
    const relOrder = (info && !info.error ? info.releases : []).map(r => r.ref);
    for (const r of rows) {
      const row = el('div', 'histrow');
      const left2 = el('div', 'who');
      const vr = el('a', 'num', 'v' + r.version);
      vr.href = r.ref ? treePath(number, r.ref)
        : itemPath(number);
      left2.append(vr, stamp(r.state));
      // WHO DECIDED THIS ROW. An auto-merge is the normal path for a prototype
      // now, and the one thing a ledger owes you is not pretending a person
      // looked. `approver_kind` comes off the revision's decided approval.
      const rk = r.rev && r.rev.approver_kind;
      const chip = whoChip(rk, rk === 'bot' && r.rev.approved_by
        ? `Approved by ${r.rev.approved_by} — an agent. The project's approval `
          + 'policy admits agents at this maturity; no human reviewed it.' : '');
      if (chip) left2.appendChild(chip);
      if (r.rev && r.rev.release && r.rev.release.decided_by) {
        const pr = el('span', 'targetchip',
          `promoted · ${r.rev.release.decided_by}`);
        pr.title = `Release #${r.rev.release.id}: promoted to `
          + `${r.rev.release.to_maturity} without a file change. The tag did `
          + 'not move.';
        left2.appendChild(pr);
      }
      if (!r.ref) {
        const only = el('span', 'pinwarn', 'database only');
        only.title = 'This revision has no tag and no draft branch in the repo.';
        left2.appendChild(only);
      }
      row.appendChild(left2);
      const mid = el('div', 'what');
      mid.textContent = r.summary || (r.rev && r.rev.summary) || '';
      if (r.commit) {
        const sha = el('div', 'tr');
        sha.textContent = r.commit.slice(0, 10);
        mid.appendChild(sha);
      }
      row.appendChild(mid);
      const rt = el('div', 'rt');
      rt.appendChild(when(r.date));
      // "Changes" — this revision against the one before it. The whole point
      // of the row.
      const i = relOrder.indexOf(r.ref);
      const base = i >= 0 ? relOrder[i + 1]
        : (info && !info.error ? info.default_release : '');
      if (r.ref && base && base !== r.ref) {
        const b = el('a', 'btn tiny', 'changes');
        b.href = cmpPath(number, base, r.ref);
        b.title = `Compare ${base} → ${r.ref}`;
        rt.appendChild(b);
      }
      row.appendChild(rt);
      card.appendChild(row);
    }
    if (!rows.length) card.appendChild(el('div', 'empty', 'No revisions yet.'));
    return card;
  }

  drawSelected();     // builds K.rev, then lays the page out
}

/* The Compare popover — the entry point the compare view never had. Base and
 * head default to previous release → the ref you are on, and the two shortcuts
 * are the questions people actually ask. */
function comparePop(number, info, ref) {
  const pop = el('div', 'clonepop');
  const all = [...info.releases, ...info.drafts];
  const rel = info.releases.map(r => r.ref);
  const i = rel.indexOf(ref);
  const defBase = i >= 0 ? (rel[i + 1] || rel[0])
    : (info.default_release || rel[0] || (all[1] && all[1].ref) || '');
  const row = el('div', 'cmppick');
  const mk = (val) => {
    const s = el('select', 'btn');
    for (const r of all) s.appendChild(new Option(r.ref, r.ref));
    if (val && !all.some(r => r.ref === val)) s.appendChild(new Option(val, val));
    s.value = val || (all[0] && all[0].ref) || '';
    return s;
  };
  const a = mk(defBase), b = mk(ref || info.default_ref);
  const go = el('button', 'btn primary', 'Compare');
  go.onclick = () => {
    popClose();
    navigate(cmpPath(number, a.value, b.value));
  };
  row.append(el('span', 'lbl', 'Old'), a, el('span', 'rl-to', '→'),
             el('span', 'lbl', 'New'), b, go);
  pop.appendChild(row);

  const shortcut = (label, base, head, why) => {
    if (!base || !head || base === head) return;
    const r = el('div', 'cmpshort');
    const lk = el('a', null, label);
    lk.href = cmpPath(number, base, head);
    lk.onclick = () => popClose();
    r.append(lk, el('span', 'why', why));
    pop.appendChild(r);
  };
  shortcut('previous release → this one', defBase, ref || info.default_ref,
           `${defBase} → ${ref || info.default_ref}`);
  if (info.drafts.length && info.default_release) {
    shortcut('latest release → newest draft', info.default_release,
             info.drafts[0].ref,
             `what ${info.drafts[0].ref} would change if it were released`);
  }
  if (info.releases.length > 1) {
    shortcut('first release → latest', info.releases[info.releases.length - 1].ref,
             info.releases[0].ref, 'everything that ever changed');
  }
  return pop;
}

function clonePopFor(url, number = '') {
  const saved = REPO;
  REPO = { number, info: { clone_url: url } };
  const pop = clonePop();
  REPO = saved;
  return pop;
}

/* ══════════════════════════════════════════ FILES: one row, one menu, one
 * opener, one set of widgets.
 *
 * Geoff: "we want to start to be clean with 'actions' you can perform on files
 * when viewing an item — this part can be viewed, sent to printer, or
 * downloaded … a registry of what types of files allow which actions … then
 * consolidate those actions into a simple clean dropdown menu."
 *
 * ⛔ NOTHING IN THIS FILE DECIDES WHAT A FILE IS. There are two registries and
 * they both live on the server:
 *
 *   plm/roles.py   MEDIA_KINDS   what a file IS (measured from its bytes),
 *                                which room shows it (`view`), and which
 *                                FORMS a changed pair of it has (`diff`)
 *   plm/actions.py ACTIONS       what you may DO with it — and, when you may
 *                                not, the sentence that says why
 *
 * Every file payload — a database row, a git tree entry, an `[[files.external]]`
 * row, a compare row — arrives carrying `{kind, preview, view, diff, actions}`.
 * This file used to keep a SECOND opinion (`previewKind()` over its own
 * `EXT_IMAGE`/`EXT_TEXT` sets) and five separate renderings of a file row; both
 * are gone, and the two that remain are the half that can only live in a
 * browser:
 *
 *   WIDGETS     how ONE file of a kind is DRAWN — one entry per name the
 *               server's `view` slot can hold.
 *   PLM_DIFF    how a changed PAIR is drawn, in each of its forms
 *               (web/diffwidgets.js — the `diff` slot's ids).
 *   ACTION_RUN  how a CLIENT-side verb runs. An `integration` action needs no
 *               entry at all — it posts itself.
 */


const fileName = (f) => f.name || (f.path || '').split('/').pop() || 'file';
/* Where the bytes are. A tree or external row was served with its own `url`;
 * a database row resolves through /api/blob. ONE definition, so a widget never
 * has to know which surface its file came from. */
const fileUrl = (f) => f.url || f.raw_url || blobUrl(f);

/* ⭐ THE SANDBOXED HALF (H2 / M11). Every blob route now answers
 * `Content-Disposition: attachment` + `nosniff` + `default-src 'none'; sandbox`
 * — a blob is bytes. Anything that puts a URL in an ELEMENT ATTRIBUTE (an
 * <img>, an <iframe>) asks for `/preview` instead, which opts in per media type
 * and answers under `script-src 'none'; sandbox` (plm/websec.py). Widgets that
 * fetch() and parse the bytes themselves — text, markdown, the STL viewer —
 * keep using fileUrl: Content-Disposition means nothing to fetch(). */
const previewHref = (u) => {
  if (!u) return u;
  const [path, qs] = String(u).split('?');
  return path.replace(/\/blob$/, '/preview')
             .replace(/^\/api\/blob$/, '/api/preview')
       + (qs ? '?' + qs : '');
};
const previewUrl = (f) => previewHref(fileUrl(f));
/* A megabyte of monospace is already more than anyone reads on screen; past it
 * the preview says so and the ⋯ menu still has Download. */
const TEXT_CAP = 1 << 20;

/* One quiet glyph per kind — the only thing about a file this file decides,
 * and it is decoration, not classification. */
const KIND_ICON = {
  mesh: '◆', cad: '◇', image: '▣', markdown: '¶', text: '≡', manifest: '⊞', pattern: '✂',
  pdf: '▤', archive: '▨', binary: '▪', submodule: '◈', tree: '▸',
};

/* The server said what this file is. A payload that somehow carries no `kind`
 * is shown as an opaque download rather than guessed at — which is the whole
 * point of deleting the guesser. */
function fileInfo(f) {
  if (!f.kind) { f.kind = 'binary'; f.preview = ''; f.view = ''; }
  if (!Array.isArray(f.diff)) f.diff = [];
  // `menu` is the whole ⋯ menu (plm/actions.menu_for); `actions` is master's
  // send-to contract, a subset of it. A row from an older server has only the
  // latter, which is still a correct (if short) menu.
  if (!Array.isArray(f.menu)) f.menu = Array.isArray(f.actions) ? f.actions : [];
  return f;
}
const primaryAction = (f) => (f.menu || []).find(a => a.primary) || null;

function downloadFile(f) {
  const a = el('a');
  a.href = fileUrl(f); a.download = fileName(f);
  document.body.appendChild(a); a.click(); a.remove();
}

/* ── THE ONE OPENER ────────────────────────────────────────────────────────
 * Every call site that used to decide "3D viewer? lightbox? download?" now
 * calls this, and it dispatches on the KIND'S registered view widget. */
async function openFile(f, ctx = {}) {
  fileInfo(f);
  const w = WIDGETS[f.view || ''];
  if (!f.preview || !w) return downloadFile(f);
  if (w.room === 'dark') { closeLightbox(); return w.mount(null, { file: f, ctx }); }
  return openLightbox(f, ctx);
}

let lbObjectUrl = null;   // a client-side blob URL to revoke on close (SVG)
const lbOpen = () => !!$('lbox') && $('lbox').classList.contains('on');
function closeLightbox() {
  const box = $('lbox');
  if (!box || !box.classList.contains('on')) return;
  box.classList.remove('on');
  box.setAttribute('aria-hidden', 'true');
  $('lbbody').innerHTML = '';
  $('lbmenu').innerHTML = '';
  // A popover lives inside the header; a closed lightbox must not leave one
  // hanging over the page it was opened from.
  if (POP && $('lbmenu') && $('lbmenu').contains(POP.wrap)) popClose();
  if (lbObjectUrl) { URL.revokeObjectURL(lbObjectUrl); lbObjectUrl = null; }
}

/* The preview room. It is a FRAME — name, role, size, the same ⋯ menu every
 * file row has — and the body is whatever widget the kind registered. */
async function openLightbox(f, ctx = {}) {
  fileInfo(f);
  const w = WIDGETS[f.view || ''] || WIDGETS.text;
  const name = fileName(f);
  $('lbname').textContent = name;
  $('lbname').title = f.path || name;
  $('lbrole').textContent = f.role_label || f.role || f.kind;
  $('lbrole').title = f.media ? `${f.kind} · ${f.media}` : f.kind;
  $('lbsize').textContent = f.size ? fmtSize(f.size) : '';
  const menu = $('lbmenu');
  menu.innerHTML = '';
  menu.appendChild(fileMenu(f, ctx));
  const body = $('lbbody');
  body.className = 'lb-' + (f.preview || 'text');
  body.innerHTML = '';
  body.appendChild(el('div', 'lbwait', 'loading ' + name + '…'));
  $('lbox').classList.add('on');
  $('lbox').setAttribute('aria-hidden', 'false');
  try {
    await w.mount(body, { file: f, ctx });
  } catch (e) {
    body.innerHTML = '';
    body.appendChild(el('div', 'lbnote', 'Could not load this file: '
      + e.message + '. The ⋯ menu still has Download.'));
  }
}

// Guarded: a browser holding a hard-cached index.html without the lightbox
// markup should lose the previews, not the whole registry.
if ($('lbox')) {
  $('lbclose').onclick = closeLightbox;
  $('lbox').addEventListener('click', (e) => {
    if (e.target.id === 'lbox') closeLightbox();  // the backdrop, as in #viewer
  });
}

/* ── THE WIDGETS ───────────────────────────────────────────────────────────
 * `mount(host, ctx)` and that is the whole contract: ctx = {file, ctx}, one
 * file. (Two sides of one file are PLM_DIFF's — web/diffwidgets.js.)
 * Adding a STEP viewer is one cell in plm/roles.py MEDIA_KINDS and one entry
 * here — no edits to the tree, the compare view or the lightbox. */
const WIDGETS = {};

/* -- viewing --------------------------------------------------------------- */

/* Geometry opens in the dark room, not in a lightbox: it is the same vBuild the
 * revision viewer and the shared snippet use, given one body. */
WIDGETS.viewer3d = { label: 'View in 3D', room: 'dark', mount(host, c) {
  const f = c.file, ctx = c.ctx || {};
  const number = ctx.number || f.number || CURRENT_ITEM;
  vBegin(fileName(f));
  const top = el('div');
  top.appendChild(el('h3', null, 'File'));
  top.appendChild(el('div', 'row', fileName(f)));
  const sub = [number, f.ref ? '@ ' + f.ref : (f.version ? 'v' + f.version : ''),
               f.role, f.size ? fmtSize(f.size) : ''].filter(Boolean).join('  ·  ');
  if (sub) top.appendChild(el('div', 'row', sub));
  if (f.inherited) {
    const note = el('div', 'row', `carried forward from v${f.from_version}`);
    note.style.color = '#8d939b';
    top.appendChild(note);
  }
  const dl = el('a', 'vbtn', 'Download');
  dl.href = fileUrl(f); dl.download = fileName(f); dl.style.textDecoration = 'none';
  top.appendChild(dl);
  return vBuild(fileName(f), [{ path: fileName(f), url: fileUrl(f), number }], top);
} };

WIDGETS.image = { label: 'Image', async mount(host, c) {
  const f = c.file, url = fileUrl(f);
  host.innerHTML = '';
  const img = el('img'); img.alt = fileName(f);
  // SVG is a document that can carry script, so plm serves it as text and never
  // offers to execute it (see api.INLINE_MEDIA). Rendering it through a
  // client-side blob URL in an <img> gets the picture with scripting disabled
  // by spec — the capability without the hole. The test is the MEASURED media,
  // not the extension.
  if ((f.media || '') === 'model/svg+xml' || (f.media || '') === 'image/svg+xml') {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    lbObjectUrl = URL.createObjectURL(
      new Blob([await r.text()], { type: 'image/svg+xml' }));
    img.src = lbObjectUrl;
  } else img.src = previewUrl(f);
  img.onerror = () => { host.innerHTML = ''; host.appendChild(
    el('div', 'lbnote', 'Could not display this image. Use Download.')); };
  host.appendChild(img);
} };

async function lbFetchText(f) {
  const r = await fetch(fileUrl(f), (f.size || 0) > TEXT_CAP
    ? { headers: { Range: `bytes=0-${TEXT_CAP - 1}` } } : {});
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  let text = await r.text();
  const cut = text.length > TEXT_CAP || (f.size || 0) > TEXT_CAP;
  if (text.length > TEXT_CAP) text = text.slice(0, TEXT_CAP);
  return { text, cut };
}
const lbCut = (host, f, text) => host.appendChild(el('div', 'lbnote',
  `Showing the first ${fmtSize(TEXT_CAP)} of ${fmtSize(f.size || text.length)}`
  + ' — download to see all.'));

WIDGETS.text = { label: 'Text', async mount(host, c) {
  const { text, cut } = await lbFetchText(c.file);
  host.innerHTML = '';
  host.appendChild(el('pre', 'filepre', text));
  if (cut) lbCut(host, c.file, text);
} };

WIDGETS.markdown = { label: 'Rendered', async mount(host, c) {
  const { text, cut } = await lbFetchText(c.file);
  host.innerHTML = '';
  const doc = el('div', 'mddoc');
  doc.innerHTML = mdToHtml(text);
  host.appendChild(doc);
  if (cut) lbCut(host, c.file, text);
} };

/* A flat pattern (`*.flat.json`, format plm.flat/1 — sewing patterns and plasma
 * profiles alike) is drawn natively by web/flat.js. `ctx.files` is the
 * revision's file list when the caller has one, so the pattern's named
 * derivatives (PDF tiles, DXF) become download links; without it, no links. */
WIDGETS.flat = { label: 'Flat pattern', async mount(host, c) {
  const r = await fetch(fileUrl(c.file));
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  const doc = await r.json();
  if (!window.renderFlat) throw new Error('the pattern viewer (flat.js) did not load');
  host.innerHTML = '';
  window.renderFlat(host, doc, { files: (c.ctx || {}).files || [], urlFor: fileUrl,
                                 onFull: c.onFull });
} };

WIDGETS.pdf = { label: 'PDF', mount(host, c) {
  host.innerHTML = '';
  const fr = el('iframe', 'lbpdf');
  // ⛔ /preview, not /blob: `Content-Disposition: attachment` makes an <iframe>
  // a download rather than a document, and a PDF is the one thing here that is
  // genuinely rendered by the browser rather than parsed by us.
  fr.src = previewUrl(c.file);
  fr.title = fileName(c.file);
  host.appendChild(fr);
} };

/* -- diffing --------------------------------------------------------------- */
/* A changed PAIR is not drawn here: every form a changed file can be read in —
 * the 3-D overlay, the pixel difference, the split diff, plm.toml's fields, a
 * BOM pin — is a registration in web/diffwidgets.js (PLM_REVIEW_MODEL §2.9).
 * WIDGETS above draw ONE file; PLM_DIFF draws two sides of one. */

/* ── THE MENU ──────────────────────────────────────────────────────────────
 * One ⋯ per file, in all five surfaces. Grouped with separators, keyboard
 * navigable, flipped to stay on screen by the same popover helper the repo bar
 * uses — and a DISABLED item carries its reason INLINE, because a tooltip is
 * not an answer on a phone. */
function fileMenu(f, ctx = {}) {
  fileInfo(f);
  const wrap = el('span', 'refwrap fmwrap');
  const b = el('button', 'fmenu', '⋯');
  b.type = 'button';
  b.title = 'What you can do with ' + fileName(f);
  b.setAttribute('aria-label', b.title);
  b.setAttribute('aria-haspopup', 'menu');
  b.onclick = (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    popToggle(wrap, () => actionPanel(f, ctx, wrap), 'actpop');
  };
  wrap.appendChild(b);
  return wrap;
}

function actionPanel(f, ctx, wrap) {
  const pop = el('div');
  pop.setAttribute('role', 'menu');
  pop.setAttribute('aria-label', fileName(f));
  const head = el('div', 'pophead');
  head.append(el('span', 'nm', fileName(f)),
              el('span', 'kd', f.kind + (f.media ? ' · ' + f.media : '')));
  pop.appendChild(head);
  const items = [];
  let last = '';
  for (const a of f.menu || []) {
    if (items.length && a.group !== last) pop.appendChild(el('div', 'actsep'));
    last = a.group;
    const it = el('button', 'actitem' + (a.enabled ? '' : ' off')
                            + (a.danger ? ' danger' : ''));
    it.type = 'button';
    it.setAttribute('role', 'menuitem');
    it.tabIndex = -1;
    it.appendChild(el('span', 't', a.label));
    if (a.enabled) it.title = a.help;
    // ⭐ THE REASON IS THE INTERFACE, and it is in the DOM, not in a tooltip:
    // "this file has never been classified — say what it is" is one click away
    // from being fixed, and nobody hovers on a phone.
    else it.appendChild(el('span', 'why', a.reason));
    it.setAttribute('aria-disabled', String(!a.enabled));
    it.onclick = (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      if (!a.enabled) return;
      runAction(a, f, ctx, wrap);
    };
    pop.appendChild(it);
    items.push(it);
  }
  if (!items.length) pop.appendChild(el('div', 'empty', 'No actions.'));
  // Arrow keys, Enter, Escape. (Escape and outside-click are the popover
  // helper's, so they behave the same as every other popover on the page.)
  pop.addEventListener('keydown', (ev) => {
    const i = items.indexOf(document.activeElement);
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      const n = items.length;
      const step = ev.key === 'ArrowDown' ? 1 : -1;
      items[((i < 0 ? (step > 0 ? -1 : 0) : i) + step + n) % n].focus();
    } else if (ev.key === 'Home') { ev.preventDefault(); items[0].focus(); }
    else if (ev.key === 'End') { ev.preventDefault(); items[items.length - 1].focus(); }
  });
  queueMicrotask(() => (items.find(x => !x.classList.contains('off'))
                        || items[0] || pop).focus());
  return pop;
}

/* How a CLIENT verb runs. An `integration` action is absent from this table on
 * purpose: it posts itself through `serverAction`, so "send this DXF to the
 * plasma table" is still one dict entry on the server and zero lines here. */
const ACTION_RUN = {
  open: (f, ctx) => openFile(f, ctx),
  download: (f) => downloadFile(f),
  copy_link: (f) => copyLine(new URL(fileUrl(f), location.href).href),
  compare: (f, ctx, a, wrap) => filePickRef(wrap, f, ctx),
  history: (f, ctx, a, wrap) => fileHistory(wrap, f, ctx),
  set_role: (f, ctx, a, wrap) => declareRole(f, wrap, ctx.refresh),
  remove: async (f, ctx) => {
    if (!confirm(`Remove ${fileName(f)} from this revision?\n\n`
      + 'The bytes are not destroyed — the record stops claiming them.')) return;
    try {
      // ⚠️ `DELETE /api/files/{id}` takes the actor as a QUERY parameter, not a
      // body — a body here would be silently dropped and the event would be
      // attributed to "api" instead of to the person who clicked.
      const out = await post(
        `/api/files/${f.id}?actor=${encodeURIComponent(actorName())}`, {}, 'DELETE');
      toast(`Removed ${out.removed || fileName(f)}`);
      if (ctx.refresh) ctx.refresh();
    } catch (e) { toast(e.message); }
  },
};

async function runAction(a, f, ctx, wrap) {
  popClose();
  const run = ACTION_RUN[a.name];
  if (run) return run(f, ctx, a, wrap);
  return serverAction(a, f, ctx);
}

/* Everything the server performs: one POST, and the file is named rather than
 * guessed at. A database row goes by id; a file in a repo goes by
 * number + ref + path, which is the address git already gives it. */
async function serverAction(a, f, ctx) {
  const number = ctx.number || f.number || CURRENT_ITEM;
  try {
    const out = f.id
      ? await post(`/api/files/${f.id}/actions/${a.name}`, { actor: actorName() })
      : await post(`/api/repo/${encodeURIComponent(number)}/actions/${a.name}`, {
          ref: f.ref || ctx.ref || '', path: f.path || fileName(f),
          external: f.source === 'external', actor: actorName() });
    const said = out && out.result && (out.result.message || out.result.detail);
    toast(said || `${a.label}: ${fileName(f)}`, out && out.link);
  } catch (err) {
    const info = err && err.detail && typeof err.detail === 'object' ? err.detail : null;
    toast((info && info.error) || err.message);
  }
}

/* "Compare this file with…" — pick another ref, land on the compare view with
 * that row open and highlighted. The ref list comes from the page when it has
 * one and is fetched when it does not, so the menu item behaves the same in the
 * tree, in the lightbox and on the Record tab. */
async function filePickRef(wrap, f, ctx) {
  const number = ctx.number || f.number || CURRENT_ITEM;
  const path = f.path || fileName(f);
  const ref = f.ref || ctx.ref || gitKeyRef(f);
  const pop = el('div', 'refpop');
  pop.appendChild(el('div', 'pophead',
    `Compare ${Path_basename(path)} at ${ref || 'this revision'} with…`));
  const list = el('div', 'reflist');
  list.appendChild(el('div', 'empty', 'reading refs…'));
  pop.appendChild(list);
  popToggle(wrap, () => pop, 'refpop');
  let info = ctx.refs || (REPO && REPO.info) || null;
  if (!info) info = await repoGet(number, 'refs').catch(() => null);
  list.innerHTML = '';
  if (!info) { list.appendChild(el('div', 'empty', 'No repo for this item.')); return; }
  const all = [...info.releases, ...info.drafts].filter(r => r.ref !== ref);
  if (!all.length) {
    list.appendChild(el('div', 'empty', 'There is no other ref to compare with.'));
  }
  // Releases are ordered newest-first, so "is the other one older?" is an index
  // comparison — and base must be the older side or the diff reads backwards.
  const order = info.releases.map(r => r.ref);
  const mine = order.indexOf(ref);
  for (const r of all) {
    const row = el('div', 'refrow');
    row.append(el('span', 'rf', r.ref));
    const st = el('span');
    if (r.state) st.appendChild(stamp(r.state));
    row.appendChild(st);
    row.appendChild(el('span', 'dt', r.date ? fmtDate(isoAt(r.date)) : ''));
    row.appendChild(el('span'));
    const theirs = order.indexOf(r.ref);
    const older = theirs < 0 ? true : (mine < 0 ? false : theirs > mine);
    const mine_ = ref || info.default_ref;
    row.onclick = () => {
      popClose();
      navigate(older ? cmpPathQ(number, r.ref, mine_, { file: path })
                            : cmpPathQ(number, mine_, r.ref, { file: path }));
    };
    list.appendChild(row);
  }
}

/* A git-origin database row knows its commit: `backend='git'`,
 * `key='<NUMBER>@<commit>:<path>'` (plm/storage.py). That commit IS a ref. */
function gitKeyRef(f) {
  if ((f.backend || '') !== 'git') return '';
  const at = String(f.key || '').indexOf('@');
  const colon = String(f.key || '').lastIndexOf(':');
  return at > 0 && colon > at ? f.key.slice(at + 1, colon) : '';
}

/* Every commit that touched this path. The tree row has shown the LAST one
 * since stage 2a; this is the rest of the answer, in the same popover. */
async function fileHistory(wrap, f, ctx) {
  const number = ctx.number || f.number || CURRENT_ITEM;
  const path = f.path || fileName(f);
  const ref = f.ref || ctx.ref || gitKeyRef(f) || 'HEAD';
  const pop = el('div', 'refpop');
  pop.appendChild(el('div', 'pophead', `History of ${Path_basename(path)}`));
  const list = el('div', 'reflist');
  list.appendChild(el('div', 'empty', 'reading the log…'));
  pop.appendChild(list);
  popToggle(wrap, () => pop, 'refpop');
  let out;
  try {
    out = await repoGet(number, 'commits', `ref=${encodeURIComponent(ref)}`
      + `&limit=40&path=${encodeURIComponent(path)}`);
  } catch (e) {
    list.innerHTML = '';
    list.appendChild(el('div', 'empty', 'Could not read the log: ' + e.message));
    return;
  }
  list.innerHTML = '';
  if (!out.commits.length) {
    list.appendChild(el('div', 'empty', 'No commit in this history touched it.'));
  }
  for (const c of out.commits) {
    const row = el('div', 'cmtrow');
    row.appendChild(el('span', 'sha', c.short));
    row.appendChild(el('span', 'sub', c.subject));
    const tr = el('div', 'tr');
    if (c.version) tr.appendChild(el('span', 'num', 'v' + c.version));
    if (c.state) tr.appendChild(stamp(c.state));
    tr.appendChild(when(isoAt(c.date)));
    row.appendChild(tr);
    list.appendChild(row);
  }
}

/* ── THE ONE FILE ROW ──────────────────────────────────────────────────────
 * kind icon · name (click = the primary action) · role chip · the caller's own
 * middle cells · size · ⋯ menu. Used by the Record tab, the repo tree and the
 * externals list; the compare rows and the lightbox use `fileMenu` alone,
 * because a diff row and a modal header are not lists. */
function fileRow(f, ctx = {}) {
  fileInfo(f);
  const tr = el('tr', 'frow');
  const ic = el('td', 'gticon', f.type === 'tree' ? KIND_ICON.tree
                                : KIND_ICON[f.kind] || KIND_ICON.binary);
  ic.title = f.type === 'tree' ? 'directory'
    : f.type === 'submodule' ? 'submodule — a BOM child, pinned'
    : `${f.kind}${f.media ? ' · ' + f.media : ''}`;
  // The repo tree folds a directory in place from its triangle (Geoff,
  // 2026-10-24); the name still navigates into it. Other surfaces pass no
  // `fold` and keep the triangle as a plain icon.
  if (f.type === 'tree' && ctx.fold) {
    ic.classList.add('gtfold');
    ic.title = 'expand';
    ic.onclick = () => ctx.fold(f, tr, ic);
  }
  tr.appendChild(ic);
  tr.appendChild(fileNameCell(f, ctx));
  if (ctx.depth) tr.dataset.depth = ctx.depth;
  for (const c of (ctx.cells ? ctx.cells(f) : [])) tr.appendChild(c);
  tr.appendChild(el('td', 'gtsz', f.size != null ? fmtSize(f.size) : ''));
  const menu = el('td', 'gtmenu');
  if (f.type !== 'tree') menu.appendChild(fileMenu(f, ctx));
  tr.appendChild(menu);
  return tr;
}

function fileNameCell(f, ctx) {
  const cell = el('td', 'gtname');
  cell.title = f.path || fileName(f);
  // The row's content is a flex line inside the cell; the cell keeps the table
  // sizing trick (see `td.gtname > .nmrow` in index.html).
  const td = el('div', 'nmrow');
  cell.appendChild(td);
  if (f.type === 'tree') {
    const a = el('a', null, f.name);
    a.href = treePath(ctx.number, ctx.ref, f.path);
    td.appendChild(a);
    return cell;
  }
  if (f.type === 'submodule') {
    td.appendChild(submoduleName(f));
    return cell;
  }
  // The name stays a real link (copy-link, middle-click, no-JS download); the
  // click is intercepted only when there is a primary action to run.
  const prim = primaryAction(f);
  const a = el('a', null, ctx.label ? ctx.label(f) : fileName(f));
  a.href = fileUrl(f);
  if (prim && prim.enabled) {
    a.title = prim.label;
    a.onclick = (ev) => { ev.preventDefault(); runAction(prim, f, ctx); };
  } else {
    a.download = fileName(f);
    a.title = (prim && prim.reason) || ('Download ' + fileName(f));
  }
  td.appendChild(a);
  if (f.source === 'external') {
    const x = el('span', 'extchip', 'external');
    x.title = `${f.backend || 'external'}  ${f.key || ''}`;
    td.appendChild(x);
  }
  // stage 17: a Git LFS file — the tree holds a pointer, the row is the file
  // (size, view, diff and download are the object's; the server resolves it)
  if (f.lfs) {
    const x = el('span', 'extchip', f.lfs.present === false ? 'lfs · missing' : 'lfs');
    x.title = `Git LFS object sha256:${f.lfs.oid || ''}`
      + (f.lfs.present === false ? ' — NOT in plm’s store' : '')
      + '\nA clone with git-lfs gets the bytes; GIT_LFS_SKIP_SMUDGE=1 gets the pointer.';
    td.appendChild(x);
  }
  if (f.role) td.appendChild(roleChip(f, ctx));
  if (f.role_warning) {
    const w = el('span', 'rolewarn', '⚠');
    w.title = f.role_warning;
    td.appendChild(w);
  }
  return cell;
}

/* WHAT IS THIS FILE? The chip shows the declared purpose. When plm INFERRED it
 * rather than being told, the chip says so and offers the real alternatives —
 * an unconfirmed guess must never look like a fact. */
function roleChip(f, ctx = {}) {
  const chip = el('span', 'rolechip' + (f.role_confirmed ? '' : ' guess')
    + (f.role_legacy ? ' legacy' : ''), f.role_label || f.role);
  if (f.role_confirmed) chip.title = `Declared: ${f.role_label || f.role}`;
  else if ((f.role_candidates || []).length) {
    chip.title = `plm guessed "${f.role}" from the filename — nobody has `
      + `confirmed it. These bytes (${f.media || 'unknown'}) could be: `
      + f.role_candidates.join(', ') + '. Click to say which.';
  } else {
    chip.title = `plm inferred "${f.role}" from the filename; it has not been `
      + 'confirmed. Click to declare it.';
  }
  const set = (f.menu || []).find(a => a.name === 'set_role');
  if (!set || !set.enabled) { chip.style.cursor = 'default'; return chip; }
  chip.onclick = (e) => { e.preventDefault(); e.stopPropagation();
                          declareRole(f, chip.parentNode, ctx.refresh); };
  return chip;
}

/* -------------------------------------------------- markdown (minimal) */
/* Small on purpose: headings, emphasis, code, lists, links, pipe tables —
 * enough for work instructions and the READMEs plm generates (a `| | |` table
 * of kind / project / ref: without table support it rendered as one line of
 * pipes, stage 18). Input is escaped before any markup is applied. */
function mdToHtml(src) {
  const esc = escapeHtml(src);
  const lines = esc.split('\n');
  const out = [];
  let list = null, para = [], code = false;
  const flushPara = () => {
    if (para.length) { out.push('<p>' + para.join(' ') + '</p>'); para = []; }
  };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const inline = (s) => s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  const isRow = (s) => /^\s*\|.*\|\s*$/.test(s);
  const isRule = (s) => /^\s*\|[\s|:-]+\|\s*$/.test(s) && s.includes('-');
  const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim());
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    if (raw.startsWith('```')) {
      flushPara(); flushList();
      out.push(code ? '</pre>' : '<pre>'); code = !code; continue;
    }
    if (code) { out.push(raw); continue; }
    // a one-line HTML comment (plm's README marker) is not shown — as on GitHub
    if (/^\s*&lt;!--.*--&gt;\s*$/.test(raw)) continue;
    if (isRow(raw) && i + 1 < lines.length && isRule(lines[i + 1])) {
      flushPara(); flushList();
      const head = cells(raw);
      const body = [];
      i += 2;
      while (i < lines.length && isRow(lines[i])) { body.push(cells(lines[i])); i += 1; }
      i -= 1;
      // a header row of empty cells (`| | |`, plm's generated README) is no header
      const th = head.some(x => x) ? '<thead><tr>' + head.map(x => `<th>${inline(x)}</th>`).join('') + '</tr></thead>' : '';
      out.push('<table class="mdtable">' + th + '<tbody>'
        + body.map(r => '<tr>' + r.map(x => `<td>${inline(x)}</td>`).join('') + '</tr>').join('')
        + '</tbody></table>');
      continue;
    }
    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    const li = raw.match(/^\s*[-*]\s+(.*)$/);
    const ol = raw.match(/^\s*\d+\.\s+(.*)$/);
    if (h) { flushPara(); flushList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); }
    else if (li) { flushPara(); if (list !== 'ul') { flushList(); out.push('<ul>'); list = 'ul'; } out.push('<li>' + inline(li[1]) + '</li>'); }
    else if (ol) { flushPara(); if (list !== 'ol') { flushList(); out.push('<ol>'); list = 'ol'; } out.push('<li>' + inline(ol[1]) + '</li>'); }
    else if (!raw.trim()) { flushPara(); flushList(); }
    else para.push(inline(raw));
  }
  flushPara(); flushList();
  if (code) out.push('</pre>');
  return out.join('\n');
}

/* ------------------------------------------------------------- viewer */
/* The dark room. Lazy three.js scene, parts in shared CAD coordinates,
 * explode / x-ray / solo — the fit-check interactions, attached to a
 * revision rather than being the application. */
const V = {
  inited: false, renderer: null, scene: null, camera: null, controls: null,
  group: null, parts: [], geos: new Map(), explode: 0, xray: false, grid: null,
  // Labelled axes + the measure tool. The triad and the tick labels live in
  // MODEL space (children of V.group, so the Z-up → Y-up transform is applied
  // for us); the scale bar lives on the world ground plane with the grid.
  axesOn: true, axesModel: null, axesWorld: null, aBox: null,
  measure: false, mPts: [], mGroup: null, mDown: null, mBox: null,
  // The live half of the measure tool. mAt is the last cursor position on the
  // canvas and mNeed asks the render loop for exactly one raycast on the next
  // frame — a pointermove can fire many times per frame and a raycast is the
  // expensive part. mArt holds the drawn measurement's objects so a drag can
  // move them instead of rebuilding four canvas textures per frame.
  mAt: null, mNeed: false, mHover: null, mDrag: null,
  mLive: null, mArt: null, hDot: null, hBand: null, hChip: null,
  hudTitle: '', hudMeta: '',
};
const V_COLORS = [0xff7a2f, 0x4f9cf0, 0x4ade80, 0xffcf3f, 0xec4899,
                  0x8b5cf6, 0x2dd4bf, 0xf0564a, 0xa3e635, 0x818cf8];

function vInit() {
  if (V.inited) return;
  V.inited = true;
  const wrap = $('vcanvas');
  V.renderer = new THREE.WebGLRenderer({ antialias: true });
  V.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  wrap.appendChild(V.renderer.domElement);
  // The overlay is inset from the window, so window resize is not the only way
  // its box changes (panel content, zoom, a rotated tablet). Watch the box.
  if (window.ResizeObserver) new ResizeObserver(() => vResize()).observe(wrap);
  V.scene = new THREE.Scene();
  V.scene.background = new THREE.Color(0x101314);
  V.camera = new THREE.PerspectiveCamera(45, 1, 1, 8000);
  // Orbit and pick share one button: a drag is an orbit, a click that did not
  // travel is a pick — and a drag that STARTED on a measurement endpoint is
  // neither, it moves the point. Our listeners go on before OrbitControls'
  // so that last case can stop the event dead and the orbit never begins.
  const cv = V.renderer.domElement;
  cv.addEventListener('pointerdown', vOnDown);
  cv.addEventListener('pointermove', vOnMove);
  cv.addEventListener('pointerup', vOnUp);
  cv.addEventListener('pointercancel', () => { if (V.mDrag) vDragEnd(false); });
  cv.addEventListener('pointerleave', () => {
    if (!V.mDrag) { V.mAt = null; V.mNeed = true; }
  });
  V.controls = new THREE.OrbitControls(V.camera, V.renderer.domElement);
  V.controls.enableDamping = true; V.controls.dampingFactor = 0.08;
  V.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const key = new THREE.DirectionalLight(0xffffff, 0.85);
  key.position.set(200, 400, 250); V.scene.add(key);
  const rim = new THREE.DirectionalLight(0x8fb0c0, 0.32);
  rim.position.set(-250, 150, -200); V.scene.add(rim);
  (function loop() {
    requestAnimationFrame(loop);
    const vw = $('viewer');
    if (!vw || !vw.classList.contains('on')) return;
    // At most one raycast per frame, and only if the cursor actually moved.
    if (V.mNeed) { V.mNeed = false; vHoverTick(); }
    V.controls.update();
    if (V.cmp) vCompareDepth();          // the overlay's removed/added reading
    V.renderer.render(V.scene, V.camera);
  })();
  addEventListener('resize', vResize);
}

function vResize() {
  const w = $('vcanvas').clientWidth, h = $('vcanvas').clientHeight;
  if (!w || !h) return;
  // setSize WITH updateStyle (no third argument). The `false` that used to be
  // here is why the viewer was a black screen on every HiDPI display — and only
  // on HiDPI, which is why it survived so long.
  //
  // updateStyle:false sets the DRAWING BUFFER and leaves the canvas's CSS size
  // alone. With setPixelRatio(2) the buffer is 2w x 2h, and a canvas with no CSS
  // size LAYS OUT at its buffer size — so the canvas became twice the width of
  // its container and the model was rendered outside the visible box. Then it
  // compounded: the oversized canvas grew its own grid cell, the ResizeObserver
  // below saw the bigger box and asked for a bigger buffer, which grew the cell
  // again. Measured: a 1013px-tall box ran away to 16208px in a few frames.
  //
  // At devicePixelRatio 1 the buffer and the CSS size are the same number, so
  // everything looked perfect on a 1x screen and in every headless test.
  V.renderer.setSize(w, h);
  V.camera.aspect = w / h; V.camera.updateProjectionMatrix();
}

function vDispose() {
  vMeasureClear();
  if (window.REVIEW_UI) REVIEW_UI.clearPins();    // review pins (web/review.js)
  if (V.axesModel) { vKill(V.axesModel); V.axesModel = null; }
  if (V.axesWorld) { vKill(V.axesWorld); V.axesWorld = null; }
  if (V.group) { V.scene.remove(V.group); V.parts.forEach(p => p.mesh.material.dispose()); }
  if (V.grid) { V.scene.remove(V.grid); V.grid = null; }
  V.group = null; V.parts = []; V.assembled = null; V.official = false; V.oBox = null;
  V.exSlider = null; V.exVal = null;
  V.cmp = null;
}

/* Tear down anything we built ourselves: line geometries and canvas textures
 * are ours to dispose, but a Sprite's geometry is a three.js singleton shared
 * by every sprite in the app — disposing that would take the others with it.
 * Same for a texture flagged vShared: the marker dots draw from a two-entry
 * cache, because a hover preview must not paint a canvas on every mouse move.
 * (Our own flag, not Texture.userData — three r128 has no such field.) */
function vKill(obj) {
  obj.traverse((o) => {
    if (o.geometry && !o.isSprite) o.geometry.dispose();
    const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
    for (const m of mats) {
      if (m.map && !m.map.vShared) m.map.dispose();
      m.dispose();
    }
  });
  if (obj.parent) obj.parent.remove(obj);
}

/* Two routes opened the dark room: a revision (the retired #/item/N/view/V) and
 * a disposable snippet (/snip/T). Both hand a body list to vBuild, so the snippet viewer
 * inherits explode / x-ray / solo / fit without a second scene builder. */
function vBegin(title) {
  $('viewer').classList.add('on');
  $('viewer').setAttribute('aria-hidden', 'false');
  vInit();
  // Reset INTERACTION state, not just the scene. A measure-drag disables
  // OrbitControls and re-enables it on pointerup — but a pointerup that
  // happens outside the window (or while the overlay is being closed) never
  // arrives, and orbit stays dead until a page reload. That is the "I have to
  // refresh to get the part back" symptom; it was never the camera.
  if (V.controls) V.controls.enabled = true;
  V.mDrag = null; V.mNeed = false; V.mAt = null;
  // Size before build: the box is inset from the window, so a resize that
  // happened while the viewer was CLOSED left camera.aspect stale (vResize
  // bails when the element has no layout), and vFit would frame for the old
  // shape — cropping a tall part on a wide screen.
  vResize();
  requestAnimationFrame(vResize);
  vDispose();
  $('vhud').innerHTML = `<b>${escapeHtml(title)}</b>`;
  $('vpanel').innerHTML = '';
  $('vbusy').textContent = 'loading geometry…';
  $('vbusy').classList.add('on');
}

function vFail(msg) {
  $('vbusy').classList.add('on');
  $('vbusy').textContent = msg;
}

async function openViewer(number, version, title) {
  vBegin(title);
  let geoms;
  try { geoms = await api(`/api/items/${encodeURIComponent(number)}/geometry?version=${encodeURIComponent(version)}`); }
  catch (e) { vFail('failed: ' + e.message); return; }
  if (!geoms.length) {
    // Say WHY there is nothing, rather than leaving a dark room: this is the
    // genuinely-empty case (no mesh here, none inherited through the BOM).
    vFail(`${number} v${version} has no geometry: no STL on the revision itself, `
      + 'and no unsuppressed BOM child carries one. Nothing to show — check in '
      + 'a mesh (role viz for an assembled representation). Press Esc to close.');
    return;
  }
  return vBuild(title, geoms);
}

async function vBuild(title, geoms, panelTop = null, opts = {}) {
  try {
    await Promise.all(geoms.map(async (g) => {
      // The server says where each mesh lives (g.url); older payloads only
      // had a vault path. Cache under that url, which is unique per row.
      g.url = g.url || blobUrl(g);
      if (V.geos.has(g.url)) return;
      const r = await fetch(g.url);
      if (!r.ok) throw new Error(`${g.path.split('/').pop()}: ${r.status}`);
      const geo = new THREE.STLLoader().parse(await r.arrayBuffer());
      geo.computeVertexNormals(); geo.computeBoundingBox();
      V.geos.set(g.url, geo);
    }));
  } catch (e) { vFail('failed: ' + e.message); return; }
  $('vbusy').classList.remove('on');

  // Shared CAD coordinates: one union bbox, one group transform (Z-up → Y-up).
  const union = new THREE.Box3();
  for (const g of geoms) union.union(V.geos.get(g.url).boundingBox);
  const c = union.getCenter(new THREE.Vector3());
  V.group = new THREE.Group();
  V.group.rotation.x = -Math.PI / 2;
  V.group.position.set(-c.x, -union.min.z, c.y);
  V.scene.add(V.group);

  V.parts = geoms.map((g, i) => {
    const geo = V.geos.get(g.url);
    // COLOUR COMES FROM THE SERVER, keyed to the FIRST-LEVEL COMPONENT this body
    // belongs to (thumbs.BODY_COLORS). So every part of the same sub-assembly
    // shares a colour — three colours for a lantern's dome/cage/internals, not
    // thirteen for its thirteen meshes — and the 3D view matches the thumbnail
    // exactly, because both are told the colour rather than choosing one.
    // V_COLORS stays as the fallback for a payload from before this existed.
    const color = g.color ? new THREE.Color(g.color).getHex()
                          : V_COLORS[i % V_COLORS.length];
    const mesh = new THREE.Mesh(geo, new THREE.MeshPhongMaterial({
      color, specular: 0x2a3033, shininess: 26, side: THREE.DoubleSide }));
    const pc = geo.boundingBox.getCenter(new THREE.Vector3());
    let dir = pc.clone().sub(c);
    if (dir.length() < 1) {
      const a = (i / geoms.length) * Math.PI * 2;
      dir = new THREE.Vector3(Math.cos(a) * 30, Math.sin(a) * 30, 0);
    }
    V.group.add(mesh);
    return { ...g, mesh, color, dir };
  });

  const sz = union.getSize(new THREE.Vector3());
  const r = Math.max(sz.length() / 2, 20);
  // A ground grid whose square is a round number of millimetres: the floor
  // doubles as a ruler, so its cell has to mean something. 1 / 2 / 5 × 10ⁿ.
  const cell = vNice(r * 4 / 24);
  const divs = Math.max(8, Math.round(r * 4 / cell));
  // Quieter floor for big sparse models: at 2 m the grid used to be the only
  // thing you could see (see vFit's note). Part first, ruler second.
  V.grid = new THREE.GridHelper(cell * divs, divs, 0x2a3336, 0x1c2224);
  V.scene.add(V.grid);
  // THE VIEWER IS THE INDIVIDUAL PARTS, ASSEMBLED (Geoff, 2026-10-26): every
  // posed component drawn in place, the explode slider live. The revision's
  // own mesh — the manifest's primary file, the official picture, the only
  // place geometry modelled in the assembly's own source exists — is here too
  // but as the "official mesh" toggle in Inspect (vPanel), not as a part:
  // off by default, and it swaps with the components when switched on.
  const comps = V.parts.filter(p => p.view === 'component');
  V.assembled = (comps.length && V.parts.find(p => p.view === 'assembled')) || null;
  V.official = false;
  if (V.assembled) V.assembled.mesh.visible = false;
  V.hudTitle = title;
  V.hudMeta = `${V.assembled ? comps.length : V.parts.length} bodies · ` +
    `${sz.x.toFixed(1)} × ${sz.y.toFixed(1)} × ${sz.z.toFixed(1)} mm`;
  // NOT-AN-ASSEMBLY WARNING. plm's BOM lines carry no occurrence transform, so
  // bodies reached through the BOM are drawn at their own modelled origin. When
  // the parts were modelled individually they land in a heap at Z=0, and a heap
  // that is presented silently reads as "my assembly is broken" — which is how
  // this looked for every revision of KML-ASM-0000 except the one whose children
  // are pre-assembled viz meshes. The server decides (thumbs.placement); this
  // says it out loud.
  const unplaced = geoms.find(g => g.assembled === false);
  V.hudWarn = unplaced ? (unplaced.placement_note || 'positions are assumed') : '';
  V.explode = 0; V.xray = false; V.measure = false; V.mPts = []; V.mAt = null;
  vCursor();
  vApplyExplode(); vApplyXray();
  // In overlay mode the x-ray checkbox is hidden: it sets depthWrite=false,
  // which is precisely what the red/green/blend reading cannot survive, and
  // the card's opacity slider already does that job properly.
  V.noXray = !!opts.noXray;
  vResize();                   // aspect first — vFit divides by it
  vFit();                      // fit next: the scale bar picks a spot the
  vAxes(union, cell);          // fitted camera can actually see (vSeen)
  vHud();
  vPanel(panelTop);
}

function vPanel(panelTop = null) {
  const p = $('vpanel');
  p.innerHTML = '';
  if (panelTop) p.appendChild(panelTop);
  p.appendChild(el('h3', null, 'Inspect'));
  const exRow = el('div', 'row');
  const exLab = el('span', null, 'explode');
  const exVal = el('span', null, '0%'); exVal.style.marginLeft = 'auto';
  exRow.append(exLab, exVal); p.appendChild(exRow);
  const ex = el('input'); ex.type = 'range'; ex.min = 0; ex.max = 200; ex.step = 5; ex.value = 0;
  ex.oninput = () => { V.explode = Number(ex.value); exVal.textContent = ex.value + '%'; vApplyExplode(); };
  p.appendChild(ex);
  V.exSlider = ex; V.exVal = exVal;       // vOfficial resets them when it takes over
  if (V.assembled) {
    const ofRow = el('label', 'row');
    const of = el('input'); of.type = 'checkbox'; of.checked = V.official;
    of.onchange = () => vOfficial(of.checked);
    V.oBox = of;
    const lbl = el('span', null, 'official mesh — the manifest\'s one body');
    lbl.title = `${V.assembled.path}\nThe assembly's own mesh as the manifest declares it: `
      + 'the thumbnail, and the only place geometry modelled in the assembly\'s '
      + 'own source shows. Swaps with the parts; explode shows the parts.';
    ofRow.append(of, lbl);
    p.appendChild(ofRow);
  }
  if (!V.noXray) {
    const xrRow = el('label', 'row');
    const xr = el('input'); xr.type = 'checkbox';
    xr.onchange = () => { V.xray = xr.checked; vApplyXray(); };
    xrRow.append(xr, el('span', null, 'x-ray — check fits'));
    p.appendChild(xrRow);
  }
  const axRow = el('label', 'row');
  const ax = el('input'); ax.type = 'checkbox'; ax.id = 'vAxes'; ax.checked = V.axesOn;
  ax.onchange = () => vAxesShow(ax.checked);
  axRow.append(ax, el('span', null, 'axes — X/Y/Z + scale'));
  p.appendChild(axRow);
  V.aBox = ax;
  const msRow = el('label', 'row');
  const ms = el('input'); ms.type = 'checkbox'; ms.id = 'vMeasure'; ms.checked = V.measure;
  ms.onchange = () => vMeasureToggle(ms.checked);
  msRow.append(ms, el('span', null, 'measure — point to point'));
  p.appendChild(msRow);
  V.mBox = ms;
  const fit = el('button', 'vbtn', 'Fit view'); fit.onclick = vFit;
  p.appendChild(fit);

  const bodies = V.parts.filter(part => !(V.assembled && part.view === 'assembled'));
  p.appendChild(el('h3', null, `Bodies (${bodies.length})`));
  for (const part of bodies) {
    const row = el('div', 'row');
    const cb = el('input'); cb.type = 'checkbox'; cb.checked = part.mesh.visible;
    cb.onchange = () => { part.mesh.visible = cb.checked; };
    part.box = cb;
    const sw = el('span', 'sw');
    sw.style.background = '#' + part.color.toString(16).padStart(6, '0');
    // An overlay body says WHICH REVISION it is ("old · assembly.stl" /
    // "new · assembly.stl", the ref @ commit in the tooltip) — two bodies with
    // the same bare filename and nothing to tell them apart was the bug.
    const nm = el('span', 'nm', part.bodyLabel || part.name || part.group_name || part.path.split('/').pop());
    if (part.which) row.classList.add('side-' + part.which);
    // Name the component the colour stands for, so the swatch is legible: with
    // several parts sharing one colour, "which sub-assembly is this?" is the
    // question the list has to answer.
    nm.title = (part.bodyTitle ? part.bodyTitle + '\n' : '') + `${part.number || ''} — click to solo\n${part.path}`
      + (part.group && part.group !== part.number
         ? `\npart of ${part.group}${part.group_name ? ' · ' + part.group_name : ''}` : '');
    nm.onclick = () => {
      const only = bodies.every(q => q.mesh.visible === (q === part));
      bodies.forEach(q => { q.mesh.visible = only ? true : q === part; });
      // Each body's own checkbox, by reference: the panel has other checkboxes
      // above this list now, so counting rows would tick the wrong boxes.
      V.parts.forEach(q => { if (q.box) q.box.checked = q.mesh.visible; });
    };
    row.append(cb, sw, nm);
    p.appendChild(row);
  }
}

/* The official mesh on or off. On: the manifest's one body, parts hidden.
 * Off: the parts, assembled. Explode is a parts view, so leaving zero turns
 * it off. The body list's boxes follow, so they never claim a hidden body is
 * shown. */
function vOfficial(on) {
  if (!V.assembled) return;
  V.official = !!on;
  if (V.oBox) V.oBox.checked = V.official;
  // The official mesh is one body: it has no explode. Switching it on while
  // the slider is up would draw it displaced by its own p.dir * k, so the
  // slider comes back to zero with it.
  if (V.official && V.explode > 0) {
    V.explode = 0;
    if (V.exSlider) V.exSlider.value = 0;
    if (V.exVal) V.exVal.textContent = '0%';
    for (const p of V.parts) p.mesh.position.set(0, 0, 0);
    if (V.mPts.length) vMeasureSync();
  }
  V.assembled.mesh.visible = V.official;
  for (const p of V.parts) if (p.view === 'component') p.mesh.visible = !V.official;
  V.parts.forEach(q => { if (q.box) q.box.checked = q.mesh.visible; });
}
function vApplyExplode() {
  const k = V.explode / 100;
  if (V.official && V.explode > 0) vOfficial(false);
  for (const p of V.parts) p.mesh.position.copy(p.dir).multiplyScalar(k);
  if (V.mPts.length) vMeasureSync();   // markers ride their own bodies out
}
function vApplyXray() {
  for (const p of V.parts) {
    const m = p.mesh.material;
    m.transparent = V.xray; m.opacity = V.xray ? 0.45 : 1;
    m.depthWrite = !V.xray; m.needsUpdate = true;
  }
}
function vFit() {
  const box = new THREE.Box3();
  if (V.group) {
    V.group.updateMatrixWorld(true);
    // Bodies only — the triad, its labels and the scale bar are furniture and
    // must not pull the camera back.
    for (const p of V.parts) box.expandByObject(p.mesh);
  }
  if (box.isEmpty()) return;
  const c = box.getCenter(new THREE.Vector3());
  const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 20);
  // Frame the WHOLE object regardless of window shape: distance from the
  // narrower field of view (vertical fov vs the aspect-derived horizontal),
  // with a 15% margin — a tall lantern in a wide window no longer crops.
  // Fit the BOX, not its bounding sphere. A sphere overshoots by up to ~70% on
  // anything long or flat — CCR-ASM-0001 is a 2371 x 2252 x 1016 mm rack of thin
  // tube, and sphere-fitting put the camera 5 m away, where 25 mm tube is one
  // pixel on a dark background and reads as an empty screen (Geoff, 2026-08-13:
  // "it's just black screen"). Project the 8 corners onto the camera's own axes
  // and solve for the distance where the widest of them just fits.
  const fovV = V.camera.fov * Math.PI / 180;
  const dir = new THREE.Vector3(0.72, 0.7, 0.95).normalize();
  const up = new THREE.Vector3(0, 1, 0);
  const right = new THREE.Vector3().crossVectors(dir, up).normalize();
  const trueUp = new THREE.Vector3().crossVectors(right, dir).normalize();
  let halfW = 0, halfH = 0, halfD = 0;
  for (let i = 0; i < 8; i++) {
    const corner = new THREE.Vector3(
      i & 1 ? box.max.x : box.min.x,
      i & 2 ? box.max.y : box.min.y,
      i & 4 ? box.max.z : box.min.z).sub(c);
    halfW = Math.max(halfW, Math.abs(corner.dot(right)));
    halfH = Math.max(halfH, Math.abs(corner.dot(trueUp)));
    halfD = Math.max(halfD, Math.abs(corner.dot(dir)));
  }
  const tanV = Math.tan(fovV / 2);
  const tanH = tanV * V.camera.aspect;
  const dist = Math.max(halfH / tanV, halfW / tanH) * 1.08 + halfD;
  V.controls.target.copy(c);
  V.camera.position.copy(c).addScaledVector(dir, dist);
  // Generous zoom range: near-plane and minDistance small enough to inspect
  // a bore up close without clipping ("really hard to zoom in").
  V.camera.near = Math.max(0.01, r / 1000); V.camera.far = dist * 40;
  V.controls.minDistance = r / 100;
  V.controls.maxDistance = dist * 8;
  V.controls.enabled = true;   // a fit always leaves the camera drivable
  V.camera.updateProjectionMatrix(); V.controls.update();
}

/* --------------------------------------------------- the HUD (top-left)
 * Title + bbox, plus the measure tool's one line of instruction. Rebuilt from
 * state rather than patched, so nothing can leave a stale hint behind. */
function vHud() {
  let h = `<b>${escapeHtml(V.hudTitle)}</b>  ${V.hudMeta}`;
  if (V.hudWarn) {
    h += `<div class="warn">⚠ not an assembled view — ${escapeHtml(V.hudWarn)}</div>`;
  }
  if (V.measure) {
    const n = V.mPts.length;
    const hint = V.mDrag ? `dragging ${'AB'[V.mDrag.i]} — release to set · Esc cancels`
      : n >= 2 ? 'drag either end to adjust · click elsewhere for a new one · Esc clears'
      : n === 1 ? 'click the second point — the line follows the cursor'
      : 'hover previews the snap · click two points';
    h += `<div class="hint">${hint}</div>`;
  }
  $('vhud').innerHTML = h;
}

/* ------------------------------------------------ labelled axes + scale
 * Two problems, one section: which way is which, and how big is this thing.
 * The vault's models are CAD Z-up, and the viewer rotates the whole group to
 * three.js Y-up — so anything that claims to say "Z" has to be built as a
 * child of V.group, in model coordinates. Text is a canvas texture on a
 * sprite: there is no font loader in vendor/, and a sprite reads from every
 * camera angle anyway.
 */
const V_AX = [
  { k: 'X', v: [1, 0, 0], c: 0xe2564d, hex: '#e8756c' },
  { k: 'Y', v: [0, 1, 0], c: 0x5fae5a, hex: '#79c274' },
  { k: 'Z', v: [0, 0, 1], c: 0x4f8fd6, hex: '#6fa6e4' },
];

/* 1 / 2 / 5 × 10ⁿ — the round numbers a scale is allowed to be. */
function vNice(x) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(x, 1e-6))));
  const n = x / p;
  return (n >= 5 ? 5 : n >= 2 ? 2 : 1) * p;
}
const vMm = (n) => (Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(1)).replace(/\.0$/, '');

/* A canvas as a texture, on the terms every label and marker here wants:
 * linear filtering, no mipmaps (they blur crisp text at a distance). */
function vTex(cv) {
  const t = new THREE.CanvasTexture(cv);
  t.minFilter = THREE.LinearFilter; t.generateMipmaps = false;
  return t;
}

/* A text label as a canvas-textured sprite. sizeAttenuation:false keeps it the
 * same size on screen at any camera distance, which is what a label wants.
 * Drawing is split out so a live chip can be re-lettered in place (vSpriteSet)
 * without churning a Sprite and a material every time a number changes. */
function vSpriteCanvas(lines, opt = {}) {
  const arr = Array.isArray(lines) ? lines : [lines];
  const fs = 28, lh = Math.round(fs * 1.34), pad = opt.pad ?? 6;
  const font = `${opt.weight || 400} ${fs}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;
  const cv = document.createElement('canvas');
  let g = cv.getContext('2d');
  g.font = font;
  const w = Math.ceil(Math.max(...arr.map(t => g.measureText(t).width)));
  cv.width = w + pad * 2; cv.height = arr.length * lh + pad * 2;
  g = cv.getContext('2d');                    // resizing reset the state
  if (opt.bg) {
    const rr = 5;
    g.beginPath();
    g.moveTo(rr, 0.5); g.lineTo(cv.width - rr, 0.5);
    g.quadraticCurveTo(cv.width - 0.5, 0.5, cv.width - 0.5, rr);
    g.lineTo(cv.width - 0.5, cv.height - rr);
    g.quadraticCurveTo(cv.width - 0.5, cv.height - 0.5, cv.width - rr, cv.height - 0.5);
    g.lineTo(rr, cv.height - 0.5);
    g.quadraticCurveTo(0.5, cv.height - 0.5, 0.5, cv.height - rr);
    g.lineTo(0.5, rr); g.quadraticCurveTo(0.5, 0.5, rr, 0.5);
    g.closePath();
    g.fillStyle = opt.bg; g.fill();
    if (opt.border) { g.strokeStyle = opt.border; g.lineWidth = 1; g.stroke(); }
  }
  g.font = font; g.textBaseline = 'top';
  g.lineJoin = 'round'; g.miterLimit = 2;
  arr.forEach((t, i) => {
    // A dark halo, unless the label already sits on a chip: a label lands on
    // whatever body colour the part happens to have, and red on orange is not
    // a label. This keeps every label legible without shouting.
    if (!opt.bg) {
      g.strokeStyle = 'rgba(11,14,15,0.9)'; g.lineWidth = 4.5;
      g.strokeText(t, pad, pad + i * lh);
    }
    g.fillStyle = (opt.colors && opt.colors[i]) || opt.color || '#e5e9e7';
    g.fillText(t, pad, pad + i * lh);
  });
  return cv;
}

/* Shape a sprite to a freshly lettered canvas: scale (from the label's height
 * fraction and the canvas aspect) and anchor. Shared by new and re-lettered. */
function vSpriteFit(s, cv, opt) {
  const h = opt.h ?? 0.026;                   // ≈ fraction of viewport height
  s.scale.set(h * (cv.width / cv.height), h, 1);
  s.center.set(opt.cx ?? 0.5, opt.cy ?? 0.5);
}

function vSprite(lines, opt = {}) {
  const cv = vSpriteCanvas(lines, opt);
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: vTex(cv), transparent: true,
    depthTest: opt.depthTest === true, depthWrite: false, sizeAttenuation: false }));
  vSpriteFit(s, cv, opt);
  s.renderOrder = opt.renderOrder ?? 12;
  return s;
}

/* Re-letter an existing sprite. One texture in, one texture out — the old map
 * is disposed here, so a chip that updates every frame while you drag does not
 * leak a GPU texture per frame. */
function vSpriteSet(s, lines, opt = {}) {
  const cv = vSpriteCanvas(lines, opt);
  if (s.material.map) s.material.map.dispose();
  s.material.map = vTex(cv);
  s.material.needsUpdate = true;
  vSpriteFit(s, cv, opt);
}

/* Can the camera see this world point right now — inside the frame, and not
 * behind a body? Used to park the scale bar somewhere it will be read. */
function vSeen(p) {
  // vFit moved the camera a moment ago and nothing has rendered since, so its
  // world matrix (and the inverse project() needs) is still the old one.
  V.camera.updateMatrixWorld();
  const ndc = p.clone().project(V.camera);
  if (ndc.z > 1 || Math.abs(ndc.x) > 0.92 || Math.abs(ndc.y) > 0.92) return false;
  const dir = p.clone().sub(V.camera.position);
  const len = dir.length();
  const ray = new THREE.Raycaster(V.camera.position, dir.normalize(), len * 0.02, len * 0.98);
  return !ray.intersectObjects(V.parts.map(q => q.mesh), false).length;
}

function vLine(pts, color, opt = {}) {
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const mat = new THREE.LineBasicMaterial({ color,
    transparent: opt.opacity != null, opacity: opt.opacity ?? 1,
    depthTest: opt.depthTest !== false, depthWrite: false });
  const l = new (opt.segments ? THREE.LineSegments : THREE.Line)(geo, mat);
  l.renderOrder = opt.renderOrder ?? 6;
  return l;
}

/* The triad at the bounding box's origin corner, plus a labelled scale bar on
 * the ground just in front of the part. `cell` is the grid square, in mm.
 *
 * Two decisions worth keeping: each arm is as long as its own bbox edge
 * (snapped to the tick step), so the triad traces the corner of the part
 * instead of shooting out of frame; and an arm that runs into the solid is
 * drawn twice — properly occluded, then ghosted on top at low opacity. That is
 * the drawing-office answer to a hidden line, and it beats both a triad that
 * vanishes into the part and floating letters with no line leading to them.
 */
function vAxes(union, cell) {
  const sz = union.getSize(new THREE.Vector3());
  const ext = Math.max(sz.x, sz.y, sz.z, 1);
  const step = vNice(ext / 5);                          // tick spacing, in mm
  const tick = step * 0.18;
  const o = union.min.clone();                          // the origin corner
  const ghost = { depthTest: false, renderOrder: 8, opacity: 0.5 };

  const G = new THREE.Group();
  V.axesModel = G;
  // Nudged a hair off the corner (0.6 % of the part) so an arm that lies along
  // a bbox edge is not coplanar with the face it hugs: coincident lines
  // z-fight and disappear. Far too small to matter to the numbers.
  const eps = ext * 0.006;
  G.position.set(-eps, -eps, -eps);
  V.group.add(G);
  for (const a of V_AX) {
    const dir = new THREE.Vector3(...a.v);
    const own = Math.max(sz.x * a.v[0], sz.y * a.v[1], sz.z * a.v[2]);
    const len = step * Math.max(1, Math.round((own * 0.9) / step));
    // Ticks lean out of the axis in a direction that is not another axis line:
    // the two ground axes tick upward, the vertical one ticks along +X.
    const off = a.k === 'Z' ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 0, 1);
    const arm = [o, o.clone().addScaledVector(dir, len)];
    G.add(vLine(arm, a.c, { opacity: 0.95 }));
    G.add(vLine(arm, a.c, ghost));
    const tk = [];
    for (let d = step; d <= len + 1e-6; d += step) {
      const p = o.clone().addScaledVector(dir, d);
      tk.push(p, p.clone().addScaledVector(off, tick));
    }
    if (tk.length) {
      G.add(vLine(tk, a.c, { segments: true, opacity: 0.9 }));
      G.add(vLine(tk, a.c, { ...ghost, segments: true, opacity: 0.28 }));
    }
    const lab = vSprite(a.k, { color: a.hex, weight: 600, h: 0.036 });
    lab.position.copy(o).addScaledVector(dir, len + step * 0.5);
    G.add(lab);
    const num = vSprite(`${vMm(len)}`, { color: '#8d939b', h: 0.022 });
    num.position.copy(o).addScaledVector(dir, len).addScaledVector(off, tick * 3);
    G.add(num);
  }
  // Say out loud where the frame is pinned, in model coordinates — otherwise
  // "ΔX 12.4" is a number without a datum.
  const org = vSprite(`(${vMm(o.x)}, ${vMm(o.y)}, ${vMm(o.z)})`,
    { color: '#7b858a', h: 0.020, cx: 1.05, cy: 1.15 });
  org.position.copy(o);
  G.add(org);

  // The scale bar: world space, on the ground plane just outside the part's
  // footprint. (World x/z are the model's x/y, centred; world y is model z.)
  const W = new THREE.Group();
  V.axesWorld = W;
  V.scene.add(W);
  const bar = Math.max(vNice(sz.x * 0.6), cell);
  const segs = Number.isInteger(bar / cell) && bar / cell <= 10 ? bar / cell : 4;
  const y = ext * 0.002, drop = Math.max(bar * 0.06, cell * 0.4);
  // Which patch of ground is free is a question about the camera, not the part:
  // in front of a tall part the ground is already at the bottom edge of the
  // frame, and a wide round one covers it with its own belly. So offer a few
  // anchors — front-right, front-left, front-centre, back-left, tucked in —
  // and take the first one the fitted camera can see end to end.
  const m = Math.max(cell, Math.min(sz.x, sz.y) * 0.08);
  const cand = [[sz.x / 2 - bar, sz.y / 2 + m], [-sz.x / 2, sz.y / 2 + m],
                [-bar / 2, sz.y / 2 + m], [-sz.x / 2, -sz.y / 2 - m],
                [sz.x / 2 - bar, sz.y / 2 + m / 3]];
  const [x0, z0] = cand.find(([cx, cz]) => [0, 0.5, 1].every(f =>
    vSeen(new THREE.Vector3(cx + bar * f, y, cz)))) || cand[0];
  W.add(vLine([new THREE.Vector3(x0, y, z0), new THREE.Vector3(x0 + bar, y, z0)],
    0x9aa4a2, { opacity: 0.9 }));
  const tk = [];
  for (let i = 0; i <= segs; i++) {
    const x = x0 + (bar * i) / segs;
    const h = (i === 0 || i === segs) ? drop * 1.8 : drop;
    tk.push(new THREE.Vector3(x, y, z0), new THREE.Vector3(x, y + h, z0));
  }
  W.add(vLine(tk, 0x9aa4a2, { segments: true, opacity: 0.9 }));
  // Both labels live at the far end of the bar. The near end is where the
  // origin triad and its coordinate tend to be, and three captions in one
  // corner is a pile, not a legend.
  const bl = vSprite(`${vMm(bar)} mm`, { color: '#9aa4a2', h: 0.022, cy: -0.3 });
  bl.position.set(x0 + bar * 0.72, y + drop * 1.8, z0);
  W.add(bl);
  const cl = vSprite(`grid ${vMm(cell)} mm`, { color: '#6b7574', h: 0.019, cx: 0, cy: 1.3 });
  cl.position.set(x0 + bar, y, z0);
  W.add(cl);

  vAxesShow(V.axesOn);
}

function vAxesShow(on) {
  V.axesOn = on;
  if (V.axesModel) V.axesModel.visible = on;
  if (V.axesWorld) V.axesWorld.visible = on;
  if (V.aBox) V.aBox.checked = on;
}

/* ----------------------------------------------------- the measure tool
 * Two picked points and the distance between them. Picks snap to the nearest
 * mesh vertex inside a small screen radius, because somebody aiming at a
 * corner means the corner — not the 0.2 mm of triangle they actually hit.
 * Points are stored as (mesh, local) pairs: `local` IS the model coordinate,
 * so the readout stays as-modelled while the drawn line follows the mesh
 * wherever explode has pushed it.
 *
 * The tool is live, because a blind two-click flow is a guess: hovering shows
 * the mark you would actually get, the first click starts a band that follows
 * the cursor with the number already on it, and either end can be dragged
 * afterwards instead of thrown away. Three layers make that cheap —
 *   V.mGroup / V.mArt : the committed measurement, its objects kept so a drag
 *                       moves them (vMeasureSync) rather than rebuilding
 *   V.mLive           : preview dot, rubber band, live chip
 *   the render loop    : one raycast per frame, only after the cursor moved
 */
const V_SNAP_PX = 16;                 // vertex snap radius, CSS px
const V_GRAB_PX = 12;                 // endpoint grab radius, CSS px
const V_SNAP_C = '#f4e7c4';           // cream: landed on a vertex
const V_SURF_C = '#8d939b';           // grey: somewhere on a face

function vMeasureToggle(on) {
  V.measure = !!on;
  if (!V.measure) vMeasureClear();
  if (V.mBox) V.mBox.checked = V.measure;
  vCursor();
  vHud();
}

/* The canvas cursor says what the next press will do: aim, grab, or orbit.
 * Called from the hover tick, so it only writes when the answer changed. */
function vCursor() {
  if (!V.renderer) return;
  const want = !V.measure ? ''
    : V.mDrag ? 'grabbing'
    : (V.mPts.length && V.mAt && vMarkerAt(V.mAt.px, V.mAt.py) != null) ? 'grab'
    : 'crosshair';
  const cv = V.renderer.domElement;
  if (cv.style.cursor !== want) cv.style.cursor = want;
}

function vMeasureClear() {
  if (V.mDrag) { V.mDrag = null; if (V.controls) V.controls.enabled = true; }
  V.mPts = []; V.mArt = null; V.mHover = null;
  if (V.mGroup) { vKill(V.mGroup); V.mGroup = null; }
  if (V.mLive) { vKill(V.mLive); V.mLive = null; }
  V.hDot = V.hBand = V.hChip = null;
  vCursor();
}

/* Canvas pixel coordinates in, a pick out. Exposed on its own so the maths can
 * be exercised without synthesising mouse events. */
function vPickAt(px, py) {
  if (!V.parts.length) return null;
  const cv = V.renderer.domElement;
  const w = cv.clientWidth, h = cv.clientHeight;
  // A pick can happen before the frame that would have refreshed these (a drag
  // frame, or a test driving the maths directly), and a stale matrix picks the
  // wrong triangle.
  V.camera.updateMatrixWorld();
  if (V.group) V.group.updateMatrixWorld(true);
  const ray = new THREE.Raycaster();
  ray.setFromCamera(new THREE.Vector2((px / w) * 2 - 1, -(py / h) * 2 + 1), V.camera);
  // Only what you can actually see: solo and per-body checkboxes hide meshes,
  // while x-ray keeps them visible (and pickable) on purpose.
  const meshes = V.parts.filter(p => p.mesh.visible).map(p => p.mesh);
  const hits = ray.intersectObjects(meshes, false);
  // A review note on an overlay is about what will SHIP: with base and head
  // both drawn, prefer the head body the ray passes through (stage 13).
  const pref = V.pickPrefer && hits.find(h => (V.parts.find(p => p.mesh === h.object)
    || {}).side === V.pickPrefer);
  const hit = pref || hits[0];
  if (!hit) return null;
  const snap = vSnapVertex(hit, px, py, w, h);
  return {
    mesh: hit.object,
    part: V.parts.find(p => p.mesh === hit.object) || null,
    local: snap || hit.object.worldToLocal(hit.point.clone()),
    snapped: !!snap,
    // The facet: its normal (geometry space = the STL's own frame, which is
    // MODEL space for these meshes — PLM_MARKUP §7.1) and its index, which is
    // only a WITNESS: valid while the mesh's bytes are the same bytes.
    normal: hit.face ? hit.face.normal.clone() : null,
    tri: hit.faceIndex != null ? hit.faceIndex : null,
  };
}

/* Nearest vertex of the hit body within V_SNAP_PX on screen, in model coords.
 * A world-space radius derived from the hit depth rejects almost everything
 * with one squared-distance test, so the scan is cheap even on a 100k-triangle
 * STL; only the survivors get projected. */
function vSnapVertex(hit, px, py, w, h) {
  const pos = hit.object.geometry.attributes.position;
  if (!pos) return null;
  const dist = V.camera.position.distanceTo(hit.point);
  const perPx = (2 * Math.tan((V.camera.fov * Math.PI / 360)) * dist) / h;
  const wr = perPx * V_SNAP_PX * 4;
  const wr2 = wr * wr;
  const m = hit.object.matrixWorld;
  const v = new THREE.Vector3(), s = new THREE.Vector3();
  let best = null, bestD = V_SNAP_PX;
  for (let i = 0, n = pos.count; i < n; i++) {
    v.fromBufferAttribute(pos, i).applyMatrix4(m);
    if (v.distanceToSquared(hit.point) > wr2) continue;
    s.copy(v).project(V.camera);
    const d = Math.hypot(((s.x + 1) / 2) * w - px, ((1 - s.y) / 2) * h - py);
    if (d < bestD) { bestD = d; best = v.clone(); }
  }
  return best ? hit.object.worldToLocal(best) : null;
}

/* A surveyor's mark: ring, crosshair, centre dot. Drawn rather than typed so it
 * is the same size everywhere and has a real centre to trust. Two colours ever,
 * so the artwork is cached and shared: the hover preview swaps between them on
 * every mouse move, and painting a canvas that often would be silly. */
const V_DOT_TEX = new Map();

function vDotTex(hex) {
  let tex = V_DOT_TEX.get(hex);
  if (tex) return tex;
  const cv = document.createElement('canvas');
  cv.width = cv.height = 64;
  const g = cv.getContext('2d');
  g.strokeStyle = hex; g.fillStyle = hex; g.lineWidth = 3;
  g.beginPath(); g.arc(32, 32, 20, 0, Math.PI * 2); g.stroke();
  g.lineWidth = 2;
  for (const [a, b, c, d] of [[32, 2, 32, 14], [32, 50, 32, 62], [2, 32, 14, 32], [50, 32, 62, 32]]) {
    g.beginPath(); g.moveTo(a, b); g.lineTo(c, d); g.stroke();
  }
  g.beginPath(); g.arc(32, 32, 5, 0, Math.PI * 2); g.fill();
  tex = vTex(cv);
  tex.vShared = true;                      // vKill must not dispose this
  V_DOT_TEX.set(hex, tex);
  return tex;
}

function vDot(hex) {
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: vDotTex(hex),
    transparent: true, depthTest: false, depthWrite: false, sizeAttenuation: false }));
  s.scale.set(0.024, 0.024, 1);
  s.renderOrder = 22;
  return s;
}

/* Repaint a mark in the other colour without touching anything else. */
function vDotSet(s, hex) {
  const tex = vDotTex(hex);
  if (s.material.map === tex) return;
  s.material.map = tex; s.material.needsUpdate = true;
}

/* The distance chip's wording and colours, from two model coordinates. Shared
 * by the committed chip and the live one so the number never changes shape as
 * it freezes. */
function vChipText(a, b, snapA, snapB) {
  const d = b.clone().sub(a);
  const dp = (n) => (Math.abs(n) >= 1000 ? n.toFixed(1) : n.toFixed(2));
  const lines = [`${dp(d.length())} mm`,
    `ΔX ${dp(d.x)}  ΔY ${dp(d.y)}  ΔZ ${dp(d.z)}`];
  const colors = ['#eef2ef', '#9aa4a2'];
  const notes = [];
  if (!snapA || !snapB) notes.push('surface pick');
  if (V.explode) notes.push(`explode ${V.explode}% — as modelled`);
  if (notes.length) { lines.push(notes.join(' · ')); colors.push('#7b858a'); }
  return { lines, colors };
}

const V_CHIP = (lines, colors, live) => ({ colors, bg: 'rgba(20,24,25,0.88)',
  border: live ? '#6d6242' : '#3a4448', pad: 9,
  h: 0.030 * lines.length, cy: -0.35, renderOrder: 23 });

/* Move a two-point line to a new pair of ends. In place: the rubber band is
 * redrawn every frame and a fresh BufferGeometry per frame is waste. The
 * bounding sphere has to be recomputed or the frustum cull hides the line, and
 * a dashed material needs its distances (and a dash length that suits the span
 * being measured) recomputed too. */
function vSeg(line, a, b) {
  const p = line.geometry.attributes.position;
  p.setXYZ(0, a.x, a.y, a.z); p.setXYZ(1, b.x, b.y, b.z);
  p.needsUpdate = true;
  line.geometry.computeBoundingSphere();
  if (line.material.isLineDashedMaterial) {
    const dash = Math.max(a.distanceTo(b) / 22, 1e-4);
    line.material.dashSize = dash; line.material.gapSize = dash * 0.8;
    line.computeLineDistances();
  }
}

/* ------------------------------------------------------- the live layer
 * Preview mark, rubber band, live chip: built once, then only positioned and
 * re-lettered. Nothing here is ever part of the measurement — committing is
 * what vMeasureAdd does. */
function vLive() {
  if (V.mLive) return V.mLive;
  const G = new THREE.Group();
  V.mLive = G; V.scene.add(G);
  V.hDot = vDot(V_SURF_C);
  // A shade bigger and a shade fainter than a placed mark: the same drawing,
  // reading as a reticle rather than a decision.
  V.hDot.scale.set(0.031, 0.031, 1);
  V.hDot.material.opacity = 0.85;
  V.hDot.renderOrder = 20;
  G.add(V.hDot);
  V.hBand = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
    new THREE.LineDashedMaterial({ color: 0xd8c68f, transparent: true, opacity: 0.95,
      depthTest: false, depthWrite: false, dashSize: 1, gapSize: 1 }));
  V.hBand.renderOrder = 20;
  G.add(V.hBand);
  V.hChip = vSprite(['', ''], V_CHIP(['', ''], ['#eef2ef', '#9aa4a2'], true));
  G.add(V.hChip);
  V.hDot.visible = V.hBand.visible = V.hChip.visible = false;
  return G;
}

/* Show the live layer for a hovered pick — or hide it (p === null). With one
 * point down the band and its chip come along, which is the whole point: the
 * number exists before the second click, not after it. */
function vLiveApply(p) {
  if (!p) {
    if (V.hDot) V.hDot.visible = V.hBand.visible = V.hChip.visible = false;
    return;
  }
  vLive();
  const w = vPtWorld(p);
  vDotSet(V.hDot, p.snapped ? V_SNAP_C : V_SURF_C);
  V.hDot.position.copy(w);
  V.hDot.visible = true;
  const band = V.mPts.length === 1;
  V.hBand.visible = V.hChip.visible = band;
  if (!band) return;
  const a = V.mPts[0];
  vSeg(V.hBand, vPtWorld(a), w);
  const t = vChipText(a.local, p.local, a.snapped, p.snapped);
  vSpriteSet(V.hChip, t.lines, V_CHIP(t.lines, t.colors, true));
  V.hChip.position.copy(vPtWorld(a)).add(w).multiplyScalar(0.5);
}

/* Which endpoint is under the cursor, if any. Screen space, because that is
 * where the finger is: the marks are drawn depth-test-free at a fixed pixel
 * size, so an endpoint behind the solid is still visibly grabbable. */
function vMarkerAt(px, py) {
  if (!V.mPts.length || !V.group || !V.renderer) return null;
  V.camera.updateMatrixWorld();
  V.group.updateMatrixWorld(true);
  const cv = V.renderer.domElement, w = cv.clientWidth, h = cv.clientHeight;
  let best = null, bd = V_GRAB_PX;
  V.mPts.forEach((p, i) => {
    const s = vPtWorld(p).project(V.camera);
    if (s.z > 1) return;                       // behind the camera
    const d = Math.hypot(((s.x + 1) / 2) * w - px, ((1 - s.y) / 2) * h - py);
    if (d <= bd) { bd = d; best = i; }
  });
  return best;
}

/* One frame's worth of live work: at most one raycast, requested by a
 * pointermove and served by the render loop. */
function vHoverTick() {
  if (!V.measure || !V.mAt || !V.parts.length) { vLiveApply(null); vCursor(); return; }
  const p = vPickAt(V.mAt.px, V.mAt.py);
  if (V.mDrag) {
    // Dragging: the mark being moved IS the preview, so no second one.
    vLiveApply(null);
    if (p) { V.mPts[V.mDrag.i] = p; V.mDrag.moved = true; vMeasureSync(); }
    return;
  }
  V.mHover = p;
  // Over an endpoint the offer is "grab this", not "place another one there".
  const over = vMarkerAt(V.mAt.px, V.mAt.py);
  vLiveApply(over == null ? p : null);
  vCursor();
}

/* ------------------------------------------------------------- gestures */
function vOnDown(e) {
  V.mDown = { x: e.clientX, y: e.clientY, btn: e.button };
  if (!V.measure || e.button !== 0 || !V.mPts.length) return;
  const r = V.renderer.domElement.getBoundingClientRect();
  const i = vMarkerAt(e.clientX - r.left, e.clientY - r.top);
  if (i == null) return;
  // Take the gesture away from OrbitControls completely: this listener was
  // registered before the controls', so stopping it here means the orbit never
  // starts. `enabled` is belted on top in case that ordering ever moves.
  e.stopImmediatePropagation(); e.preventDefault();
  V.mDrag = { i, from: V.mPts[i], moved: false, pid: e.pointerId };
  V.controls.enabled = false;
  try { V.renderer.domElement.setPointerCapture(e.pointerId); } catch (_) {}
  vCursor(); vHud();
}

function vOnMove(e) {
  const r = V.renderer.domElement.getBoundingClientRect();
  V.mAt = { px: e.clientX - r.left, py: e.clientY - r.top };
  if (V.measure) V.mNeed = true;               // the loop does the raycast
}

function vOnUp(e) {
  const d = V.mDown; V.mDown = null;
  if (V.mDrag) { vDragEnd(true); return; }     // a drag is not also a click
  // STAGE 13: the review pin tool (web/review.js) — a click that did not
  // travel drops a note's anchor where it landed. Same 5 px rule as measure.
  if (window.REVIEW_UI && REVIEW_UI.pinMode && d && d.btn === 0
      && Math.hypot(e.clientX - d.x, e.clientY - d.y) <= 5) {
    const r = V.renderer.domElement.getBoundingClientRect();
    const p = vPickAt(e.clientX - r.left, e.clientY - r.top);
    if (!p) { toast('no geometry under that point — tap the part itself'); return; }
    REVIEW_UI.onPick(p);
    return;
  }
  if (!V.measure || !d || e.button !== 0 || d.btn !== 0) return;
  if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > 5) return;   // an orbit
  const r = V.renderer.domElement.getBoundingClientRect();
  vMeasureClick(e.clientX - r.left, e.clientY - r.top);
}

/* Release (commit = true) or abandon (Esc, pointercancel). Either way the
 * camera gets its button back. */
function vDragEnd(commit) {
  const d = V.mDrag;
  if (!d) return;
  V.mDrag = null;
  if (!commit) { V.mPts[d.i] = d.from; vMeasureSync(); }
  V.controls.enabled = true;
  try { V.renderer.domElement.releasePointerCapture(d.pid); } catch (_) {}
  V.mNeed = true;                              // hover preview comes back
  vCursor(); vHud();
}

function vMeasureClick(px, py) {
  const p = vPickAt(px, py);
  if (!p) { toast('no geometry under the cursor'); return; }
  vMeasureAdd(p);
}

function vMeasureAdd(p) {
  // A click on the model with a finished measurement starts a new one. Only the
  // committed drawing goes; the live layer is furniture and stays.
  if (V.mPts.length >= 2) {
    V.mPts = []; V.mArt = null;
    if (V.mGroup) { vKill(V.mGroup); V.mGroup = null; }
  }
  V.mPts.push(p);
  vMeasureDraw();
  vLiveApply(null);                            // the band froze into the line
  V.mNeed = true;
  vHud();
}

/* World position of a pick right now — model coordinate pushed back out
 * through its own mesh, so markers ride along with explode. */
const vPtWorld = (p) => p.mesh.localToWorld(p.local.clone());

/* Build the committed measurement, keeping every object in V.mArt: a drag and
 * the explode slider then move what is already there (vMeasureSync) instead of
 * repainting four canvas textures per frame. */
function vMeasureDraw() {
  if (V.mGroup) { vKill(V.mGroup); V.mGroup = null; }
  V.mArt = null;
  if (!V.mPts.length || !V.group) return;
  V.group.updateMatrixWorld(true);
  const G = new THREE.Group();
  V.mGroup = G; V.scene.add(G);

  const w = V.mPts.map(vPtWorld);
  const art = { n: V.mPts.length, dots: [], tags: [], line: null, chip: null };
  V.mArt = art;
  V.mPts.forEach((p, i) => {
    const dot = vDot(p.snapped ? V_SNAP_C : V_SURF_C);
    dot.position.copy(w[i]); G.add(dot); art.dots.push(dot);
    const tag = vSprite('AB'[i], { color: '#c8cfcd', h: 0.020,
      cx: -0.4, cy: -0.3, renderOrder: 22 });
    tag.position.copy(w[i]); G.add(tag); art.tags.push(tag);
  });
  if (V.mPts.length < 2) return;

  art.line = vLine([w[0], w[1]], 0xe4e9e6,
    { depthTest: false, renderOrder: 21, opacity: 0.95 });
  G.add(art.line);
  const t = vChipText(V.mPts[0].local, V.mPts[1].local,
    V.mPts[0].snapped, V.mPts[1].snapped);
  art.chip = vSprite(t.lines, V_CHIP(t.lines, t.colors));
  art.chip.position.copy(w[0]).add(w[1]).multiplyScalar(0.5);
  G.add(art.chip);
}

/* The same measurement, moved: new endpoint positions, new colours, new number.
 * Falls back to a full rebuild if the shape of the thing changed. */
function vMeasureSync() {
  const art = V.mArt;
  if (!art || art.n !== V.mPts.length) { vMeasureDraw(); return; }
  if (!V.group) return;
  V.group.updateMatrixWorld(true);
  const w = V.mPts.map(vPtWorld);
  V.mPts.forEach((p, i) => {
    vDotSet(art.dots[i], p.snapped ? V_SNAP_C : V_SURF_C);
    art.dots[i].position.copy(w[i]);
    art.tags[i].position.copy(w[i]);
  });
  if (art.n < 2) return;
  vSeg(art.line, w[0], w[1]);
  const t = vChipText(V.mPts[0].local, V.mPts[1].local,
    V.mPts[0].snapped, V.mPts[1].snapped);
  vSpriteSet(art.chip, t.lines, V_CHIP(t.lines, t.colors));
  art.chip.position.copy(w[0]).add(w[1]).multiplyScalar(0.5);
}

function closeViewer() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(e => console.debug('fullscreen exit refused', e));
  }
  $('viewer').classList.remove('max');
  $('viewer').classList.remove('on');
  $('viewer').setAttribute('aria-hidden', 'true');
  vDispose();
  // A docked viewer belongs to a card that is about to be thrown away; put the
  // overlay back on the body before that happens, or the next open has nowhere
  // to appear (see vDock in the compare view).
  vUndock();
}
function exitViewer() {
  // Over an item page the viewer is a plain overlay: just close it, the URL
  // never moved. A snippet is the exception — the link IS the page, so step
  // back to its card instead of leaving an empty screen.
  const s = location.pathname.match(/^\/snip\/([^/]+)$/);
  if (s) { navigate(`/snip/${s[1]}/info`); return; }
  closeViewer();
}
$('vclose').onclick = exitViewer;
/* Full screen (Geoff, 2026-10-25): the whole window for the room, overlay or
 * docked. The Fullscreen API on #vbox where the browser grants it (Esc leaves,
 * and the browser tells us through fullscreenchange); where it does not (iOS
 * Safari on anything but <video>), the `max` class pins the room over the
 * window instead and Esc is handled below. Either way the canvas cell changes
 * size and the ResizeObserver re-frames the model. */
function vFullOn() {
  return !!document.fullscreenElement || $('viewer').classList.contains('max');
}
function vFullToggle() {
  const box = $('vbox');
  if (vFullOn()) {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(e => console.debug('fullscreen exit refused', e));
    }
    $('viewer').classList.remove('max');
    vFullPaint();
    return;
  }
  const go = box.requestFullscreen ? box.requestFullscreen()
    : Promise.reject(new Error('requestFullscreen unavailable'));
  go.catch((e) => {
    console.debug('fullscreen refused, pinning the room instead', e);
    $('viewer').classList.add('max'); vFullPaint();
  });
}
function vFullPaint() {
  const on = vFullOn();
  $('vfull').textContent = on ? '⤡' : '⛶';
  $('vfull').title = on ? 'Leave full screen (Esc)' : 'Full screen (Esc leaves)';
  vResize();
  requestAnimationFrame(vResize);
}
$('vfull').onclick = vFullToggle;
document.addEventListener('fullscreenchange', vFullPaint);
// Phone: the panel is a sheet under the canvas (index.html, max-width 760px).
// The strip toggles it; the class lives on #vbox so CSS drives both the sheet
// and the canvas cell, and the ResizeObserver re-frames the model.
$('vptoggle').onclick = () => {
  const open = $('vbox').classList.toggle('popen');
  $('vptoggle').setAttribute('aria-expanded', String(open));
  vResize();
};
const vDocked = () => $('viewer').classList.contains('docked');
$('viewer').addEventListener('click', (e) => {
  // A DOCKED viewer has no backdrop — it is part of the page — so there is
  // nothing to click through and nothing to leave.
  if (e.target.id === 'viewer' && !vDocked()) exitViewer();
});
addEventListener('keydown', (e) => {
  // The lightbox sits above everything and is not routed, so it gives way first.
  if (e.key === 'Escape' && lbOpen()) {
    closeLightbox();
    return;
  }
  if (e.key === 'Escape' && $('viewer').classList.contains('on')) {
    // Escape gives up one thing at a time: the drag in your hand, then the
    // measurement, then the tool, then the room. Losing a measurement should
    // not also lose your place.
    if (V.mDrag) { vDragEnd(false); return; }
    if (V.mPts.length) { vMeasureClear(); vHud(); return; }
    if (V.measure) { vMeasureToggle(false); return; }
    // Full screen is one more thing to give up before the room. A browser
    // usually takes the Esc that leaves its own full screen and never hands it
    // to the page (fullscreenchange tells us); one that does hand it over, or
    // the `max` fallback, lands here — either way, leave full screen, keep
    // the room.
    if (vFullOn()) { vFullToggle(); return; }
    if (!vDocked()) exitViewer();
    return;
  }
  if (e.key === '/' && !$('viewer').classList.contains('on') &&
      !lbOpen() &&
      !e.target.matches('input, select, textarea') && $('q')) {
    e.preventDefault(); $('q').focus();
  }
});

/* --------------------------------------------------- disposable 3D snippet
 * /snip/<token> — a temporary STL somebody (usually an agent) shared to ask
 * "do you like this?". It is deliberately NOT a part: no number, no revision,
 * no history, and it deletes itself when the TTL runs out. The card says so in
 * as many words, because the worst outcome would be treating a snippet as a
 * record. Two routes: the token alone opens the 3D view; /info is the card on
 * its own, which is also where Close lands.
 */
const snipPath = (t) => PLM_ROUTES.P.snip(t);
const fmtBytes = (n) => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
  : n >= 1024 ? (n / 1024).toFixed(0) + ' kB' : n + ' B';
/* fmtWhen/fmtExact live at the top of the file with fmtDate — one definition of
 * "when", so a timestamp cannot be UTC in one view and local in another. */
function fmtLeft(secs) {
  if (secs <= 0) return 'expired';
  if (secs < 3600) return `in ${Math.round(secs / 60)} min`;
  if (secs < 172800) return `in ${Math.round(secs / 3600)} h`;
  return `in ${Math.round(secs / 86400)} days`;
}

async function renderSnip(token, { view = true } = {}) {
  CURRENT_ITEM = null;
  const page = $('page');
  page.innerHTML = '';
  let snip;
  try { snip = await api(`/api/snippets/${encodeURIComponent(token)}`); }
  catch (e) {
    closeViewer();
    const box = el('div', 'doc');
    box.appendChild(el('h3', null, 'Snippet unavailable'));
    box.appendChild(el('div', 'empty', e.message));
    const pad = el('div', 'pad');
    const home = el('a', null, '← registry'); home.href = '/';
    pad.appendChild(home);
    box.appendChild(pad);
    page.appendChild(box);
    return;
  }

  const crumbs = el('div', 'crumbs');
  const back = el('a', null, '← registry'); back.href = '/';
  crumbs.append(back, document.createTextNode('  /  shared 3D snippet — '
    + 'temporary, not a tracked part'));
  page.appendChild(crumbs);

  const tb = el('div'); tb.id = 'tblock';
  const cell = (label, value, opts = {}) => {
    const c = el('div', 'cell' + (opts.wide ? ' wide' : ''));
    c.appendChild(el('div', 'lbl', label));
    const v = el('div', 'v' + (opts.title ? ' title' : ''));
    if (value instanceof Node) v.appendChild(value); else v.textContent = value;
    c.appendChild(v);
    return c;
  };
  tb.appendChild(cell('File', snip.name, { title: true }));
  tb.appendChild(cell('Note', snip.note || '(no note)', { wide: true, title: true }));
  tb.appendChild(cell('Expires', fmtLeft(snip.expires_in_seconds)));
  tb.appendChild(cell('Shared by', snip.actor || '—'));
  tb.appendChild(cell('Size', fmtBytes(snip.size)));
  tb.appendChild(cell('Gone after', fmtWhen(snip.expires_at)));
  tb.appendChild(cell('sha256', snip.sha256.slice(0, 12)));
  page.appendChild(tb);

  const bar = el('div');
  bar.style.cssText = 'display:flex;gap:8px;margin:-6px 0 18px;align-items:center;flex-wrap:wrap';
  const viewBtn = el('button', 'btn primary', 'View 3D');
  viewBtn.onclick = () => { navigate(snipPath(token)); };
  const dl = el('a', 'btn', 'Download STL');
  dl.href = snip.blob_url; dl.download = snip.name;
  dl.style.textDecoration = 'none';
  const copy = el('button', 'btn', 'Copy link');
  copy.onclick = async () => {
    // The path form, not the hash form: it survives an OIDC login redirect.
    const url = `${location.origin}/snip/${encodeURIComponent(token)}`;
    try { await navigator.clipboard.writeText(url); toast('Link copied'); }
    catch { toast(url); }
  };
  bar.append(viewBtn, dl, copy);
  page.appendChild(bar);

  const box = el('div', 'doc');
  box.appendChild(el('h3', null, 'What this is'));
  const pad = el('div', 'pad');
  pad.appendChild(el('div', 'note',
    'A disposable snippet: geometry shared for a quick look. It has no part '
    + 'number, no revision and no approval, and both the file and this page '
    + 'disappear when it expires. If it turns out to matter, it should be '
    + 'checked into the registry as a real part — then it gets a number, a '
    + 'history and a backup.'));
  box.appendChild(pad);
  page.appendChild(box);

  if (view) openSnipViewer(token, snip);
}

// The overlay for a snippet: one body, straight from the snippet blob route,
// with the note repeated in the panel so the question stays in front of you
// while you spin the model.
async function openSnipViewer(token, snip) {
  vBegin(snip.name);
  const top = el('div');
  top.appendChild(el('h3', null, 'Shared snippet'));
  top.appendChild(el('div', 'row', snip.name));
  if (snip.note) {
    const n = el('div', 'row', snip.note);
    n.style.cssText = 'display:block;color:#c8cfcd;line-height:1.45;white-space:normal';
    top.appendChild(n);
  }
  const exp = el('div', 'row', `expires ${fmtLeft(snip.expires_in_seconds)}`);
  exp.style.color = '#8d939b';
  top.appendChild(exp);
  const infoBtn = el('button', 'vbtn', 'Details / download');
  infoBtn.onclick = () => { navigate(`${snipPath(token)}/info`); };
  top.appendChild(infoBtn);
  return vBuild(snip.name, [{ path: snip.name, url: snip.blob_url,
                              number: 'snippet' }], top);
}

/* ==========================================================================
 * THE REPOSITORY — the part page IS a repo page (docs/PLM_GIT_FIRST.md §6c)
 *
 * Everything below reads `/api/repo/*` (the repo API reference), which is `git ls-tree`,
 * `cat-file`, `log` and `diff` on the bare repo and nothing else. There is no
 * second index, so what this page shows and what `git clone` gives you cannot
 * disagree — which is the whole argument for the band existing.
 *
 * Three traps the API solves and this code must respect:
 *   1. `main` DOES NOT ALWAYS EXIST (59 items have never been released). The
 *      ref to open at is `default_release || default_ref`, never the literal
 *      string "main".
 *   2. `binary: true` on a compare file means DO NOT RENDER `patch`. An ASCII
 *      STL yields 90 000 "added lines" that mean nothing; the size delta is
 *      the honest number, and it is why `size_base`/`size_head` exist.
 *   3. An UNRELEASED submodule pin must not look like a released one. §5.1's
 *      rule is that you cannot release above your least-mature child, and a
 *      rule nobody can see on the page is not a rule.
 * ========================================================================= */

/* The item page's tabs. `ref` and `compare` are verbs in the same slot, so the
 * names may never collide — which is why they are a set rather than a regex. */
const ITEM_TABS = new Set(['part', 'bom', 'releases', 'drafts', 'pulls', 'history',
                           'record', 'context']);

const repoGet = (n, route, qs = '') =>
  api(`/api/repo/${encodeURIComponent(n)}/${route}` + (qs ? '?' + qs : ''));
const rawUrl = (n, ref, path) =>
  `/api/repo/${encodeURIComponent(n)}/blob?ref=${encodeURIComponent(ref)}`
  + `&path=${encodeURIComponent(path)}`;
const extUrl = (n, ref, name) =>
  `/api/repo/${encodeURIComponent(n)}/external?ref=${encodeURIComponent(ref)}`
  + `&name=${encodeURIComponent(name)}`;
/* THE PAGES' ADDRESSES (stage 22: real paths, GitHub's shape). Every builder
 * is a thin name over web/routes.js, which holds the whole table and is the one
 * place a shape changes. They return PATHS (`/CID-ASM-0001/tree/v0.1.0/cad`),
 * used as real `<a href>`s — middle-click and copy-link work — and given to
 * `navigate()` for in-app moves.
 *
 * A ref may be `draft/v0.17.0`: its slash is written LITERALLY, like GitHub,
 * and the router reads it back longest-first against the repo's refs. */
const itemPath = (n) => PLM_ROUTES.P.item(n);
const treePath = (n, ref, path = '') => PLM_ROUTES.P.tree(n, ref, path);
const blobPath = (n, ref, path) => PLM_ROUTES.P.blob(n, ref, path);
const cmpPath = (n, base, head) => PLM_ROUTES.P.compare(n, base, head);
/* A compare can be pointed at ONE file (`?file=`) or one mesh (`?mesh=`), so
 * "compare this file with…" from the tree lands on the row it meant and the
 * geometry chooser is linkable. */
const cmpPathQ = (n, base, head, q = {}) => PLM_ROUTES.P.compare(n, base, head, q);
/* One review ROUND (an approval id): `/<n>/proposal/<approval_id>` — resolves
 * to the proposal it belongs to and replaces itself with `/<n>/pull/<pid>`. */
const roundPath = (n, id) => PLM_ROUTES.P.round(n, id);
/* The proposal OBJECT (stage 13): `/<n>/pull/<pid>`, GitHub's pull request. */
const pullPath = (n, pid) => PLM_ROUTES.P.pull(n, pid);

/* The release page: one promotion of an EXISTING tagged revision —
 * `/<n>/releases/tag/<vX.Y.Z>` when the tag is known (GitHub's shape),
 * `/<n>/releases/<id>` otherwise. The sibling of the proposal route, because
 * they are siblings: one asks "is this change right?", the other "is this
 * revision ready to build against?" (docs/PLM_REVIEW_MODEL.md §2.1b). */
const relPath = (n, id, tag = '') => PLM_ROUTES.P.release(n, id, tag);

/* A tab, optionally AT a ref (`?ref=`). Without it, picking v0.2.0 on Part
 * and then clicking BOM silently showed you the latest revision's BOM (stage
 * 2c's known gap 4). */
const tabPath = (n, tab, ref = '') => PLM_ROUTES.P.tab(n, tab, ref);

/* ⭐ A REFUSAL IS THE USER INTERFACE. The write path answers a merge conflict
 * or a release-gate violation with a 409 whose `detail` is a whole paragraph of
 * what happened, why, and the git commands that fix it. Flattening that into a
 * toast — "Conflict" — throws away the only part anybody can act on, so it is
 * rendered verbatim, in monospace, with its line breaks intact. */
function refusalBox(e, title = '') {
  const box = el('div', 'refusal');
  const hd = el('div', 'hd');
  hd.append(el('span', 'x', '✕'),
            el('span', 'lbl', title || `Refused${e.status ? ' · ' + e.status : ''}`));
  box.appendChild(hd);
  const pre = el('pre');
  pre.textContent = typeof e === 'string' ? e
    : (typeof e.detail === 'string' ? e.detail : e.message || String(e));
  box.appendChild(pre);
  return box;
}

/* What the current compare URL is pointed at. */
function cmpQ() {
  return PLM_ROUTES.parseQuery(location.search);
}
/* API timestamps are ISO-8601 with an offset; `when()` speaks epoch seconds.
 * One converter, so a git date and a DB date are formatted by the same code. */
const isoAt = (s) => (s ? Date.parse(s) / 1000 : 0);
const short = (s, n) => (s || '').length > n ? s.slice(0, n - 1) + '…' : (s || '');

/* ------------------------------------------------------------- popovers
 * One at a time, dismissed by a click outside or Escape. The capture-phase
 * listener runs before the opener's own onclick, so a second press on the
 * button that opened it still toggles rather than closing-then-reopening. */
let POP = null;
function popClose() { if (POP) { POP.node.remove(); POP = null; } }
function popToggle(wrap, build, cls) {
  const was = POP && POP.wrap === wrap;
  popClose();
  if (was) return;
  const node = build();
  node.classList.add('pop');
  if (cls) node.classList.add(cls);
  // Hang it off whichever edge keeps it on screen. Compare and Clone sit at
  // the right end of the repo bar, and a left-anchored 520px panel there runs
  // straight off the page.
  const r = wrap.getBoundingClientRect();
  if (r.left > innerWidth * 0.5) node.classList.add('right');
  wrap.appendChild(node);
  // ...and off whichever EDGE keeps it on screen vertically. A ⋯ menu on the
  // last row of a long file list opened below the fold and could not be read.
  const b = node.getBoundingClientRect();
  if (b.bottom > innerHeight - 8 && r.top > b.height + 8) node.classList.add('up');
  POP = { node, wrap };
}
document.addEventListener('click', (e) => {
  if (POP && !POP.wrap.contains(e.target)) popClose();
}, true);
addEventListener('keydown', (e) => { if (e.key === 'Escape') popClose(); });

function copyLine(text) {
  const done = () => toast('Copied');
  const fail = () => toast('Could not copy — select the line and copy it.');
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, fail);
  else fail();
}

/* ------------------------------------------------- the band on an item page
 * REPO holds what the band is currently showing. Picking a ref sets the hash;
 * the router notices the item did not change and re-renders only this band,
 * so the ref is linkable and back-button safe without reloading the page. */
let REPO = null;

async function mountRepo(mount, number, ref, path, opts = {}) {
  const me = { number, mount, info: null, ref: ref || '', path: path || '',
               tab: '', manP: null, bare: !!opts.bare,
               // a /blob/ URL: the file to preview once its directory is drawn
               open: opts.open || '' };
  REPO = me;
  mount.innerHTML = '';
  if (!me.bare) mount.appendChild(el('div', 'band', 'Repository'));
  const wait = el('div', 'doc');
  wait.appendChild(el('div', 'empty', `reading ${number}.git…`));
  mount.appendChild(wait);
  let info;
  try { info = await repoGet(number, 'refs'); }
  catch (e) { if (REPO === me) repoAbsent(mount, number, e); return; }
  if (REPO !== me) return;          // navigated away while the call was in flight
  me.info = info;
  repoDraw();
}

function repoAbsent(mount, number, e) {
  mount.innerHTML = '';
  mount.appendChild(el('div', 'band', 'Repository'));
  const card = el('div', 'doc');
  card.innerHTML = '<h3>No repo of its own</h3>';
  card.appendChild(el('div', 'pad note', /no repo/i.test(e.message)
    ? `${number} is a catalog item: it lives in catalog.git as ${number}.toml, `
      + 'one shared repo rather than a repo per screw. Its parents pin the '
      + 'catalog as it stood when the revision was made.'
    : `Could not read the repo: ${e.message}`));
  mount.appendChild(card);
}

function repoDraw() {
  const R = REPO, { mount, number, info } = R;
  R.ref = R.ref || info.default_release || info.default_ref;
  mount.innerHTML = '';

  if (!R.bare) {
    const band = el('div', 'band');
    band.append(el('span', null, 'Repository'), el('span', 'src', `${number}.git`));
    mount.appendChild(band);
  }

  const card = el('div', 'doc');
  // On the Part tab the ref / counts / Compare / Clone controls are already at
  // the TOP of the page, where Geoff could not miss them; repeating them above
  // the tree would be the same row twice.
  if (!R.bare) card.appendChild(repoBar());
  mount.appendChild(card);

  if (!info.counts.commits) {
    card.appendChild(el('div', 'empty',
      `${number}.git is empty — this item has no revisions checked in, so there `
      + 'is nothing to browse. The repo exists and can be cloned.'));
    return;
  }

  R.manP = repoGet(number, 'manifest', `ref=${encodeURIComponent(R.ref)}`);
  R.manP.catch(() => {});                  // a rejection nobody awaits is noise

  if (R.bare) card.appendChild(latestCommitStrip());
  const treeBox = el('div');
  card.appendChild(treeBox);
  const readmeBox = el('div');
  mount.appendChild(readmeBox);
  if (!R.bare) {
    const cols = el('div', 'cols');
    const lc = el('div'), rc = el('div');
    cols.append(lc, rc);
    mount.appendChild(cols);
    repoCommits(lc);
    repoManifest(rc);
  }
  repoTree(treeBox, readmeBox);
}

/* GitHub's one-line strip above the tree: the commit this ref points at, who
 * made it and when — and the sha is a link to what it CHANGED, which is the
 * shortest path from "I am looking at a part" to "I am looking at a diff". */
function latestCommitStrip() {
  const R = REPO, { number, ref, info } = R;
  const strip = el('div', 'lcstrip');
  strip.appendChild(el('span', 'sub', 'reading the log…'));
  repoGet(number, 'commits', `ref=${encodeURIComponent(ref)}&limit=1`)
    .then(out => {
      if (REPO !== R || R.ref !== ref) return;
      const c = out.commits[0];
      strip.innerHTML = '';
      if (!c) { strip.appendChild(el('span', 'sub', 'No commits.')); return; }
      const rel = info.releases.map(r => r.ref);
      const i = rel.indexOf(ref);
      const base = i >= 0 ? rel[i + 1] : info.default_release;
      const sha = base && base !== ref ? el('a', 'sha', c.short)
                                       : el('span', 'sha', c.short);
      if (sha.tagName === 'A') {
        sha.href = cmpPath(number, base, ref);
        sha.title = `What changed: ${base} → ${ref}`;
      }
      strip.append(sha, el('span', 'sub', c.subject));
      const rt = el('span', 'rt');
      if (c.author) rt.appendChild(el('span', 'who', c.author));
      rt.appendChild(when(isoAt(c.date)));
      strip.appendChild(rt);
    })
    .catch(e => { strip.innerHTML = ''; strip.appendChild(
      el('span', 'sub', 'log unavailable: ' + e.message)); });
  return strip;
}

/* Navigate the band alone: same item, new ref or new directory. */
function repoGoto(ref, path) {
  if (!REPO) return;
  REPO.ref = ref || REPO.info.default_release || REPO.info.default_ref;
  REPO.path = path || '';
  repoDraw();
}

/* ----------------------------------------------------------- the repo bar */
function repoBar() {
  const R = REPO, info = R.info;
  const bar = el('div', 'repobar');

  const wrap = el('span', 'refwrap');
  const btn = el('button', 'refbtn');
  btn.append(el('span', 'lbl', 'Ref'), el('span', 'rf', R.ref),
             el('span', 'caret', '▾'));
  btn.title = 'Pick a branch or a tag — the tree, README and manifest below '
    + 'all re-read at that ref';
  btn.onclick = () => popToggle(wrap, refPop, 'refpop');
  wrap.appendChild(btn);
  bar.appendChild(wrap);

  const c = info.counts, nBr = branchCount(info);
  bar.appendChild(el('span', 'counts',
    `${nBr} branch${nBr === 1 ? '' : 'es'} · ${c.releases} tag`
    + `${c.releases === 1 ? '' : 's'} · ${c.commits} commit`
    + `${c.commits === 1 ? '' : 's'}`));
  bar.appendChild(el('span', 'spacer'));

  // Compare: this ref against the release before it — the question anyone
  // opening a compare view is actually asking.
  const prev = refBefore(R.ref);
  const cmp = el('button', 'btn', 'Compare…');
  cmp.title = prev ? `Compare ${prev} → ${R.ref}`
    : 'Nothing to compare against — this is the earliest ref';
  cmp.disabled = !prev;
  cmp.onclick = () => { navigate(cmpPath(R.number, prev, R.ref)); };
  bar.appendChild(cmp);

  const cw = el('span', 'refwrap');
  const cb = el('button', 'btn', 'Clone ▾');
  cb.onclick = () => popToggle(cw, clonePop, 'clonepop');
  cw.appendChild(cb);
  bar.appendChild(cw);
  return bar;
}

/* Every ref the dropdown offers, newest first: releases, then drafts, then —
 * defensively — `default_ref` when it is neither (catalog.git has 119 commits,
 * no tags and no draft branches). */
/* THE branch list, GitHub's way: `main` first when it exists (released work
 * only — it is the newest tag's commit), then every draft/* branch. A repo
 * with commits but neither a tag nor a draft still has to be pickable —
 * catalog.git is 119 commits on `main` and nothing else — so the default ref
 * stands in. Every branch count on the page (item tabs, the header counts,
 * the repo bar, the picker) is this list's length, so they cannot disagree. */
function branchList(info) {
  const out = [];
  // `info.main` is the server's fact (plm/gitrepo.py): the default branch,
  // where it points and which tag sits on it. Absent until something is
  // released; the client never invents it.
  if (info.main) {
    out.push({ ...info.main, isDefault: true,
               summary: 'released work only' + (info.main.at ? ` · at ${info.main.at}` : '') });
  }
  out.push(...(info.drafts || []));
  if (!out.length && !(info.releases || []).length && info.default_ref) {
    out.push({ ref: info.default_ref, kind: 'branch', isDefault: true, state: '',
               maturity: '', date: '', summary: '', version: '' });
  }
  return out;
}
const branchesAll = () => branchList(REPO.info);
/* The count the server states (counts.branches); the list's length only for a
 * payload from before the server said it. */
const branchCount = (info) => (info.counts && info.counts.branches != null)
  ? info.counts.branches : branchList(info).length;
/* What `ref` should be compared AGAINST by default — "and what changed since?"
 * For a release that is the release before it. For a draft it is the newest
 * release, because that is what the draft departed from; on an item that has
 * never been released (59 of them) the only honest predecessor is the draft
 * before it. Empty means there is nothing older, and the button says so. */
function refBefore(ref) {
  const info = REPO.info;
  const i = info.releases.findIndex(r => r.ref === ref);
  if (i >= 0) return i + 1 < info.releases.length ? info.releases[i + 1].ref : '';
  const j = info.drafts.findIndex(r => r.ref === ref);
  if (j >= 0) {
    return info.default_release
      || (info.drafts[j + 1] ? info.drafts[j + 1].ref : '');
  }
  return info.default_release || (info.releases[0] ? info.releases[0].ref : '');
}

function refPop() {
  const R = REPO, info = R.info;
  const pop = el('div', 'refpop');
  const tabs = el('div', 'reftabs');
  const flt = el('input', 'refflt');
  const list = el('div', 'reflist');
  let tab = R.tab || 'dft';   // GitHub opens on Branches

  const mkTab = (key, label, n) => {
    const b = el('button', tab === key ? 'on' : null, `${label} ${n}`);
    b.onclick = () => { tab = key; R.tab = key; paint(); };
    return b;
  };
  const paint = () => {
    tabs.innerHTML = '';
    tabs.append(mkTab('dft', 'Branches', branchesAll().length),
                mkTab('rel', 'Tags', info.releases.length));
    flt.placeholder = tab === 'rel' ? 'Filter tags…' : 'Filter branches…';
    const rows = tab === 'rel' ? info.releases : branchesAll();
    const q = flt.value.trim().toLowerCase();
    const show = q ? rows.filter(r =>
      (r.ref + ' ' + (r.summary || '')).toLowerCase().includes(q)) : rows;
    list.innerHTML = '';
    if (!show.length) {
      list.appendChild(el('div', 'empty', q ? 'Nothing matches that.'
        : tab === 'rel'
          ? 'No tags. This item has never been released, so it has no tags '
            + `and no main — ${info.default_ref} is its newest draft branch.`
          : 'No branches.'));
      return;
    }
    for (let i = 0; i < show.length; i++) list.appendChild(refRow(show[i], show[i + 1]));
  };
  flt.oninput = paint;
  paint();
  pop.append(tabs, flt, list);
  setTimeout(() => flt.focus(), 0);
  return pop;
}

function refRow(r, older) {
  const R = REPO;
  const row = el('div', 'refrow' + (r.ref === R.ref ? ' on' : ''));
  row.append(el('span', 'rf', r.ref));
  const st = el('span');
  if (r.state) st.appendChild(stamp(r.state));
  if (r.isDefault) st.appendChild(el('span', 'vchip', 'default'));
  row.appendChild(st);
  const dt = el('span', 'dt', r.date ? fmtDate(isoAt(r.date)) : '');
  if (r.date) dt.title = `${fmtExact(isoAt(r.date))}  ·  ${fmtAgo(isoAt(r.date))}`;
  row.appendChild(dt);
  // Compare THIS release with the one before it, straight from the list.
  const base = older ? older.ref : (r.kind === 'branch' ? refBefore(r.ref) : '');
  const cmp = el('button', 'cmp', '⇄');
  cmp.title = base ? `Compare ${base} → ${r.ref}` : 'Nothing older to compare with';
  cmp.disabled = !base;
  cmp.onclick = (e) => {
    e.stopPropagation(); popClose();
    navigate(cmpPath(R.number, base, r.ref));
  };
  row.appendChild(cmp);
  if (r.summary) row.appendChild(el('span', 'sum', r.summary));
  row.onclick = () => { popClose(); navigate(treePath(R.number, r.ref)); };
  return row;
}

function clonePop() {
  const url = REPO.info.clone_url;
  const number = REPO.number || (REPO.info && REPO.info.number) || '';
  const pop = el('div', 'clonepop');
  const cmdRow = (host, label, cmd, why) => {
    const r = el('div', 'clonerow');
    if (label) r.appendChild(el('span', 'lbl', label));
    const line = el('div', 'cmd');
    // The command is its OWN element. A copy button that scooped up the prose
    // beside it would paste a shell line with an explanation glued to the end.
    line.appendChild(el('code', null, cmd));
    const b = el('button', 'btn', 'Copy');
    b.onclick = () => copyLine(cmd);
    line.appendChild(b);
    r.appendChild(line);
    if (why) r.appendChild(el('div', 'why', why));
    host.appendChild(r);
    return r;
  };
  for (const [label, cmd, why] of [
    ['The whole assembly', `git clone --recurse-submodules ${url}`,
     'BOM children are submodules. Without --recurse-submodules, parts/* are '
     + 'empty directories.'],
    ['This item only', `git clone ${url}`,
     'The tree, its tags and its drafts. No children.'],
    ['Text only, meshes on demand',
     `git clone --filter=blob:none --recurse-submodules ${url}`,
     'A partial clone: history and sources now, blobs fetched when something '
     + 'actually reads them.'],
  ]) cmdRow(pop, label, cmd, why);

  /* ---- and how to push one back (§6d) --------------------------------------
   * Reads need no credential; a push needs the token, and the token is never in
   * this page, this JS or this repo. The line below names the FILE it lives in,
   * on the server, and nothing else — a UI that could print a credential is a
   * UI that will eventually print one into a screenshot. */
  const push = el('div', 'pushrecipe');
  push.appendChild(el('div', 'lbl', 'Propose a change'));
  push.appendChild(el('div', 'why',
    'A push is a PROPOSAL, never a release: the commit you push IS the DRAFT '
    + 'revision, and a human approving in plm is what moves main and writes '
    + 'the tag.'));
  const topic = 'draft/<topic>';
  cmdRow(push, '1 · branch and commit',
    `git switch -c ${topic}\n`
    + '# edit the source, bump `version` in plm.toml, keep state = "DRAFT"\n'
    + 'git commit -am "what you changed and why"',
    'The version must be a single bump over the newest release, and plm.toml '
    + 'must still say DRAFT — plm writes the real state in the release commit '
    + 'at approval.');
  cmdRow(push, '2 · push the draft',
    `git push origin HEAD:refs/heads/${topic}`,
    'Creates or updates the DRAFT. Re-push as often as you like; deleting the '
    + 'branch withdraws it.');
  cmdRow(push, '3 · ask for a decision',
    'git push -o topic=<topic> -o maturity=PROTOTYPE \\\n'
    + '  origin HEAD:refs/for/main',
    'AGit: the same push, plus "submit it". `-o maturity=` is the target '
    + '(PROTOTYPE or RELEASED), `-o title=` the note the approver sees.');
  const tok = el('div', 'clonerow');
  tok.appendChild(el('span', 'lbl', 'Credential'));
  tok.appendChild(el('div', 'why',
    'Reads are anonymous; a push needs a bearer token. plm never shows one — '
    + 'yours is the line ending in your username in the server\'s push-tokens '
    + 'file (PLM_TOKENS_FILE; ask the operator of this instance):'));
  tok.appendChild(el('code', 'tokenpath', '<token>=<your username>'));
  tok.appendChild(el('div', 'why', 'Give it to git per-command, so it never '
    + 'lands in a config file or your shell history:'));
  const gc = el('code', null,
    "git -c 'http.extraHeader=Authorization: Bearer <token>' push …");
  tok.appendChild(gc);
  push.appendChild(tok);
  pop.appendChild(push);
  if (number) {
    const foot = el('div', 'why');
    foot.style.marginTop = '8px';
    foot.textContent = `Pushed drafts of ${number} appear on its Branches tab, `
      + 'with what each one changes.';
    pop.appendChild(foot);
  }
  return pop;
}

/* ------------------------------------------------------------- file tree */
async function repoTree(box, readmeBox) {
  const R = REPO, { number, ref, path } = R;
  box.innerHTML = '';
  box.appendChild(el('div', 'empty', 'reading the tree…'));
  readmeBox.innerHTML = '';
  let t;
  try {
    t = await repoGet(number, 'tree',
      `ref=${encodeURIComponent(ref)}&path=${encodeURIComponent(path)}`);
  } catch (e) {
    box.innerHTML = '';
    box.appendChild(el('div', 'empty', `Could not read the tree: ${e.message}`));
    return;
  }
  if (REPO !== R || R.ref !== ref || R.path !== path) return;
  box.innerHTML = '';

  // breadcrumb — the repo root, then one link per directory
  const crumb = el('div', 'gcrumb');
  const root = el('a', null, number);
  root.href = treePath(number, ref);
  crumb.appendChild(root);
  let acc = '';
  for (const seg of path ? path.split('/') : []) {
    acc = acc ? acc + '/' + seg : seg;
    crumb.append(document.createTextNode(' / '));
    const a = el('a', null, seg);
    a.href = treePath(number, ref, acc);
    crumb.appendChild(a);
  }
  box.appendChild(crumb);

  const tbl = el('table', 'gtree');
  const tb = el('tbody'); tbl.appendChild(tb);
  if (path) {
    const up = path.split('/').slice(0, -1).join('/');
    const tr = el('tr');
    tr.append(el('td', 'gticon', '↰'));
    const nm = el('td', 'gtname');
    const a = el('a', 'up', '..'); a.href = treePath(number, ref, up);
    nm.appendChild(a);
    tr.append(nm, el('td', 'gtmsg'), el('td', 'gtage'), el('td', 'gtsz'),
              el('td', 'gtmenu'));
    tb.appendChild(tr);
  }
  for (const e of t.entries) tb.appendChild(treeRow(e));
  // `/<N>/blob/<ref>/<dir>/<file>` (stage 22): the directory is the page, the
  // file opens over it the way a click on its row would — once.
  if (REPO.open) {
    const want = REPO.open;
    REPO.open = '';
    const hit = t.entries.find(e => e.name === want && e.type !== 'tree' && e.type !== 'submodule');
    if (hit) openFile(hit, { number: REPO.number, ref: REPO.ref });
    else toast(`${want} is not in this directory at ${REPO.ref}`);
  }
  // `[[files.external]]` rows now arrive WITH the tree, classified and with
  // their own actions — the client no longer merges two payloads and guesses
  // what an external file is.
  for (const x of t.external || []) tb.appendChild(externalRow(x));
  if (!tb.children.length) tb.appendChild(el('tr'))
    .appendChild(el('td', 'empty', 'Nothing in this directory.'));
  box.appendChild(tbl);

  if (t.readme) repoReadme(readmeBox, t.readme);
}

function treeRow(e, depth = 0) {
  const { number, ref } = REPO;
  return fileRow(e, {
    number, ref, depth, fold: treeFold,
    // The tree's own two columns: what the last commit that touched this path
    // said, and how long ago it said it.
    cells: (f) => {
      const lc = f.last_commit || {};
      const msg = el('td', 'gtmsg', lc.subject || '');
      if (lc.subject) msg.title = `${lc.short}  ${lc.subject}`;
      const age = el('td', 'gtage', lc.date ? fmtAgo(isoAt(lc.date)) : '');
      if (lc.date) age.title = fmtExact(isoAt(lc.date));
      return [msg, age];
    },
  });
}


/* Expand or collapse a directory row in place. Expanding reads that directory
 * at the same ref and inserts its rows (one level deeper, indented) right under
 * the folder; collapsing removes every row deeper than the folder until the
 * next row at its own depth or above. Nested folders fold the same way, since
 * every inserted row is itself a treeRow. The URL does not move: the folded
 * view is a way of looking at this directory, not a different page. */
async function treeFold(f, tr, ic) {
  const R = REPO, { number, ref } = R;
  const depth = Number(tr.dataset.depth || 0);
  if (tr.classList.contains('open')) {
    tr.classList.remove('open');
    ic.textContent = KIND_ICON.tree; ic.title = 'expand';
    let n = tr.nextElementSibling;
    while (n && Number(n.dataset.depth || 0) > depth) {
      const gone = n; n = n.nextElementSibling; gone.remove();
    }
    return;
  }
  if (tr.classList.contains('busy')) return;
  tr.classList.add('busy');
  let t;
  try {
    t = await repoGet(number, 'tree',
      `ref=${encodeURIComponent(ref)}&path=${encodeURIComponent(f.path)}`);
  } catch (e) {
    tr.classList.remove('busy');
    console.error('treeFold', { number, ref, path: f.path, error: e });
    toast(`Could not read ${f.path}: ${e.message}`);
    return;
  }
  tr.classList.remove('busy');
  if (REPO !== R || R.ref !== ref || !tr.isConnected) return;
  tr.classList.add('open');
  ic.textContent = '▾'; ic.title = 'collapse';
  let after = tr;
  const rows = t.entries.map(e => treeRow(e, depth + 1));
  for (const x of t.external || []) {
    const row = externalRow(x);
    row.dataset.depth = depth + 1;     // so a collapse takes it away too
    rows.push(row);
  }
  if (!rows.length) {
    const e = el('tr', 'frow'); e.dataset.depth = depth + 1;
    e.appendChild(el('td', 'gticon'));
    const nm = el('td', 'gtname'); nm.colSpan = 5;
    nm.appendChild(el('span', 'sub', 'empty directory'));
    e.appendChild(nm);
    rows.push(e);
  }
  for (const row of rows) { after.after(row); after = row; }
}

/* `parts/KML-PRT-1013 @ v0.3.1` — the path, the pin, and whether the pin is a
 * release. An UNRELEASED pin gets a dashed chip and says so in as many words:
 * §5.1 refuses to release a parent above its least-mature child, and this row
 * is where that rule becomes visible. */
function submoduleName(e) {
  const s = e.submodule || {};
  const box = el('span');
  const label = e.path;
  if (s.number === 'catalog') {
    // One gitlink per parent revision, not per BOM line (§5.4) — so there is
    // no single item page to send anyone to.
    const t = el('span', null, label);
    t.title = 'The shared catalog repo, pinned as it stood when this revision '
      + 'was made. Purchased and hardware children are TOMLs inside it.';
    box.appendChild(t);
  } else if (s.known) {
    const a = el('a', null, label);
    a.href = treePath(s.number, s.ref || s.commit);
    a.title = s.released
      ? `${s.number} at ${s.ref}`
      : `${s.number} at ${s.commit.slice(0, 10)} — not a release`;
    box.appendChild(a);
  } else {
    const t = el('span', null, label);
    t.title = `No repo for ${s.number} — the pin cannot be followed.`;
    box.appendChild(t);
  }
  box.append(document.createTextNode(' @ '), pinChip(s));
  return box;
}

function pinChip(s) {
  const wrap = el('span');
  const c = el('span', 'pin' + (s && s.released ? '' : ' loose'),
                s ? (s.label || (s.commit || '').slice(0, 10)) : '—');
  if (s && !s.released) {
    c.title = 'Not a release. This is a pseudo-version: the commit is after '
      + 'the tag it names and carries no tag of its own. A parent cannot be '
      + 'released above an unreleased child (§5.1).';
    wrap.append(c, document.createTextNode(' '),
                el('span', 'pinwarn', 'not a release'));
  } else {
    c.title = 'Released — the gitlink lands on a tagged commit.';
    wrap.appendChild(c);
  }
  return wrap;
}

/* An external file is not in the tree, so it would silently vanish from
 * a page whose whole promise is "this is the part". It is listed here, marked
 * EXTERNAL, and opened through /external — plm serves the bytes. */
function externalRow(x) {
  const { number, ref } = REPO;
  return fileRow(x, {
    number, ref,
    cells: (f) => {
      const msg = el('td', 'gtmsg', f.role ? `role: ${f.role}` : '');
      msg.title = f.sha256 ? 'sha256 ' + f.sha256 : '';
      return [msg, el('td', 'gtage', '')];
    },
  });
}


async function repoReadme(box, name) {
  const { number, ref, path } = REPO;
  const full = path ? `${path}/${name}` : name;
  const card = el('div', 'doc');
  const h = el('h3', null, name);
  const right = el('span', 'right');
  const open = el('button', 'btn', 'Open');
  open.onclick = () => openFile(
    { name, path: full, ref, url: rawUrl(number, ref, full),
      kind: 'markdown', preview: 'md', view: 'markdown' }, { number, ref });
  right.appendChild(open);
  h.appendChild(right);
  card.appendChild(h);
  const body = el('div', 'mddoc');
  body.style.maxHeight = 'none';
  body.textContent = 'loading…';
  card.appendChild(body);
  box.appendChild(card);
  try {
    const text = await api(rawUrl(number, ref, full));
    body.innerHTML = mdToHtml(typeof text === 'string' ? text : String(text));
  } catch (e) { body.textContent = 'Could not read ' + name + ': ' + e.message; }
}

/* --------------------------------------------------------------- commits */
async function repoCommits(box) {
  const R = REPO, { number, ref } = R;
  const card = el('div', 'doc');
  const h = el('h3', null, 'Commits');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note', ref));
  card.appendChild(h);
  const body = el('div');
  body.appendChild(el('div', 'empty', 'reading the log…'));
  card.appendChild(body);
  box.appendChild(card);
  let out;
  try { out = await repoGet(number, 'commits',
    `ref=${encodeURIComponent(ref)}&limit=50`); }
  catch (e) {
    body.innerHTML = '';
    body.appendChild(el('div', 'empty', 'Could not read the log: ' + e.message));
    return;
  }
  if (REPO !== R || R.ref !== ref) return;
  body.innerHTML = '';
  const relRefs = new Set(R.info.releases.map(x => x.ref));
  for (const c of out.commits) {
    const row = el('div', 'cmtrow');
    row.appendChild(el('span', 'sha', c.short));
    row.appendChild(el('span', 'sub', c.subject));
    const tr = el('div', 'tr');
    // `Plm-Version` / `Plm-State` trailers: a commit list here doubles as a
    // revision list, which is exactly what a PLM wants it to be.
    if (c.version) tr.appendChild(el('span', 'num', 'v' + c.version));
    if (c.state) tr.appendChild(stamp(c.state));
    if (c.author) tr.appendChild(el('span', null, c.author));
    tr.appendChild(when(isoAt(c.date)));
    // A commit that IS a release can be compared with the release before it.
    const tag = c.version ? 'v' + c.version : '';
    if (tag && relRefs.has(tag)) {
      const base = refBefore(tag);
      if (base) {
        const b = el('button', 'btn tiny', 'compare');
        b.title = `Compare ${base} → ${tag}`;
        b.onclick = () => { navigate(cmpPath(number, base, tag)); };
        tr.appendChild(b);
      }
    }
    row.appendChild(tr);
    body.appendChild(row);
  }
  if (!out.commits.length) body.appendChild(el('div', 'empty', 'No commits.'));
}

/* -------------------------------------------------------------- manifest
 * plm.toml at the selected ref: the design facts as the REPO holds them,
 * beside the same facts as the database holds them further up the page. When
 * the two disagree, that is a finding, not a rendering bug. */
async function repoManifest(box) {
  const R = REPO, { number, ref } = R;
  const card = el('div', 'doc');
  const h = el('h3', null, 'plm.toml');
  const right = el('span', 'right');
  const raw = el('button', 'btn', 'Raw');
  // The RAW manifest, as text — the structured reading is this card. `view`
  // is named rather than left to the kind, because `plm.toml`'s registered
  // view is the text one and this button is asking for exactly that.
  raw.onclick = () => openFile(
    { name: 'plm.toml', path: 'plm.toml', ref, url: rawUrl(number, ref, 'plm.toml'),
      kind: 'manifest', preview: 'text', view: 'text' }, { number, ref });
  right.append(el('span', 'note', ref), raw);
  h.appendChild(right);
  card.appendChild(h);
  const body = el('div');
  body.appendChild(el('div', 'empty', 'reading the manifest…'));
  card.appendChild(body);
  box.appendChild(card);

  let m;
  try { m = (await R.manP).manifest; }
  catch (e) {
    body.innerHTML = '';
    body.appendChild(el('div', 'empty', 'No manifest at this ref: ' + e.message));
    return;
  }
  if (REPO !== R || R.ref !== ref) return;
  body.innerHTML = '';
  if (!m) { body.appendChild(el('div', 'empty', 'No plm.toml at this ref.')); return; }

  const head = el('div', 'pad');
  const id = el('div');
  id.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap';
  id.append(el('span', 'num', `v${m.version || '—'}`));
  if (m.state) id.appendChild(stamp(m.state));
  if (m.kind) id.appendChild(kindchip(m.kind));
  if (m.process) id.appendChild(el('span', 'kindchip', m.process));
  head.appendChild(id);
  if (m.name) head.appendChild(el('div', 'note', m.name));
  if (m.summary) {
    const s = el('div', 'note', m.summary);
    s.style.marginTop = '6px';
    head.appendChild(s);
  }
  if (m.source && m.source.file) {
    const s = el('div', 'note');
    s.style.marginTop = '6px';
    s.append(el('span', 'lbl', 'Source  '), document.createTextNode(
      m.source.file + (m.source.selector ? `  ·  ${m.source.selector}` : '')));
    head.appendChild(s);
  }
  body.appendChild(head);

  const attrs = m.attributes || {};
  const an = Object.keys(attrs).sort();
  if (an.length) {
    body.appendChild(el('div', 'rellbl', 'Attributes'));
    const t = el('table', 'led'); const tb = el('tbody'); t.appendChild(tb);
    for (const k of an) {
      const a = attrs[k] || {};
      const row = el('tr');
      row.append(el('td', null, k),
                 el('td', 'ver', `${a.value}${a.unit ? ' ' + a.unit : ''}`),
                 el('td', 'dim srccell', short(a.source || '', 34)));
      if (a.source) row.lastChild.title = a.source;
      tb.appendChild(row);
    }
    body.appendChild(t);
  }

  const bom = m.bom || [];
  if (bom.length) {
    body.appendChild(el('div', 'rellbl', `Bill of materials (${bom.length})`));
    const t = el('table', 'led');
    t.innerHTML = '<thead><tr><th>Find</th><th>Path</th><th>Qty</th><th></th></tr></thead>';
    const tb = el('tbody'); t.appendChild(tb);
    for (const l of bom) {
      const row = el('tr');
      if (l.suppressed) row.classList.add('sup');
      row.appendChild(el('td', 'dim', l.find == null ? '' : String(l.find)));
      const pc = el('td', 'ver');
      const child = bomChild(l.path);
      if (child) {
        const a = el('a', null, l.path); a.href = itemPath(child);
        pc.appendChild(a);
      } else pc.textContent = l.path;
      row.appendChild(pc);
      row.appendChild(el('td', null, `${l.qty} ${l.unit || ''}`.trim()));
      const note = el('td', 'dim');
      if (l.track === 'release') {
        const c = el('span', 'pin loose', 'track: release');
        c.title = 'This line named no version. The pin is the newest child '
          + 'release dated on or before this revision.';
        note.appendChild(c);
      }
      if (l.suppressed) note.appendChild(el('span', 'dim', ' suppressed'));
      if (l.notes) note.title = l.notes;
      row.appendChild(note);
      tb.appendChild(row);
    }
    body.appendChild(t);
  }

  const files = m.files || {};
  const roles = Object.keys(files).filter(k => k !== 'primary' && k !== 'external');
  if (files.primary || roles.length || (files.external || []).length) {
    body.appendChild(el('div', 'rellbl', 'File roles'));
    const ul = el('ul', 'filelist');
    const line = (role, name, ext) => {
      const li = el('li');
      li.append(el('span', 'rolechip', role), el('span', null, name));
      if (ext) li.appendChild(el('span', 'extchip', 'external'));
      ul.appendChild(li);
      return li;
    };
    if (files.primary) {
      const li = line('primary', files.primary);
      li.title = 'The geometry of record — what the viewer, the thumbnail and '
        + 'the 3D compare all read.';
    }
    for (const r of roles.sort()) {
      for (const p of (Array.isArray(files[r]) ? files[r] : [files[r]])) line(r, p);
    }
    for (const x of files.external || []) line(x.role || 'file', x.name, true);
    body.appendChild(ul);
  }

  const tags = m.tags || [];
  if (tags.length) {
    body.appendChild(el('div', 'rellbl', 'Tags'));
    const wrap = el('div', 'tagrow');
    wrap.style.padding = '0 12px 8px';
    for (const t of tags) wrap.appendChild(el('span', 'tagchip', t));
    body.appendChild(wrap);
  }

  for (const [key, label, fmt] of [
    ['relations', 'Relations', (r) => `${r.type}  ${r.item}`
      + (r.version ? ` v${r.version}` : '')],
    ['routing', 'Routing', (r) => `${r.seq}  ${r.op}`
      + (r.work_center ? `  ·  ${r.work_center}` : '')],
  ]) {
    const rows = m[key] || [];
    if (!rows.length) continue;
    body.appendChild(el('div', 'rellbl', label));
    const ul = el('ul', 'filelist');
    for (const r of rows) {
      const li = el('li', null, fmt(r));
      if (r.notes || r.note) li.title = r.notes || r.note;
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }
}

/* `parts/KML-PRT-1013` -> `KML-PRT-1013`; `catalog/HDW-0012.toml` -> null
 * (a catalog line has no item page of its own in this POC). */
function bomChild(path) {
  const m = /^parts\/([A-Za-z0-9-]+)$/.exec(path || '');
  return m ? m[1] : null;
}

/* ==========================================================================
 * THE COMPARE VIEW — the centrepiece (§6c, §6e)
 *
 * Three stacked readings of one range, in the order a reviewer asks them:
 *   1. what changed about the PART   (the manifest delta, as redlines)
 *   2. what changed about its SHAPE  (both meshes in one scene, red over green)
 *   3. what changed in the BYTES     (per-file unified diffs)
 * ========================================================================= */

async function renderCompare(number, spec) {
  REPO = null;                      // the band is gone; rebuild it on the way back
  const page = $('page');
  page.innerHTML = '';
  CURRENT_ITEM = number;
  // `<base>...<head>[?file=…&mesh=…]` — the query is what a "compare this file
  // with…" link carries, and what the geometry chooser writes back.
  const qAt = String(spec || '').indexOf('?');
  const q = qAt < 0 ? {} : cmpQ();
  const parts = (qAt < 0 ? String(spec || '') : spec.slice(0, qAt)).split('...');
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    page.appendChild(el('div', 'empty',
      'A compare is two refs: /N/compare/<base>...<head>'));
    return;
  }
  // A git ref cannot contain whitespace, so anything after the first space is
  // prose that rode along with a pasted link ("…v0.3.0 is the diff view").
  // Cut it and fix the address bar, rather than fail on a ref nobody typed.
  const clean = r => decodeURIComponent(r).trim().split(/\s/)[0];
  const base = clean(parts[0]), head = clean(parts[1]);
  if (base + '...' + head !== decodeURIComponent(parts.join('...'))) {
    history.replaceState(null, '', cmpPathQ(number, base, head, q));
  }

  const crumbs = el('div', 'crumbs');
  const back = el('a', null, `← ${number}`);
  back.href = treePath(number, head);
  crumbs.append(back, document.createTextNode(`  /  comparing  ${base} → ${head}`));
  page.appendChild(crumbs);

  const headBox = el('div');
  page.appendChild(headBox);
  const wait = el('div', 'doc');
  wait.appendChild(el('div', 'empty', `diffing ${base} → ${head}…`));
  page.appendChild(wait);

  let cmp, info = null;
  try {
    [cmp, info] = await Promise.all([
      repoGet(number, 'compare',
        `base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}`),
      repoGet(number, 'refs').catch(() => null),
    ]);
  } catch (e) {
    wait.innerHTML = '';
    wait.appendChild(el('div', 'empty', `Could not compare: ${e.message}`));
    return;
  }
  if (CURRENT_ITEM !== number) return;
  wait.remove();
  headBox.appendChild(cmpHead(number, cmp, info));
  cmpStack(page, number, cmp, q);
}

/* The three readings, in order, appended to whatever host asked for them.
 * The compare VIEW is this plus a header with ref pickers; the proposal PAGE is
 * this plus a header that says who is asking and a bar that decides. One
 * definition of "what changed", two places it is read — forking it would mean
 * the page Geoff approves from and the page he explores from could disagree
 * about the same range. */
/* ⭐ THE TWO KINDS THIS PAGE FEATURES AT THE TOP, and the only thing on the
 * page that knows they are special. `plm.toml` is not a text file you scroll
 * past, and a gitlink is not a byte change — they are the PART changing, so
 * they lead. Their rows fold into those cards instead of appearing twice. HOW
 * they are drawn is their registration in web/diffwidgets.js, like every
 * other file type. */
const FEATURED_KINDS = {
  manifest: { title: 'What changed in the design', present: () => true },
  submodule: { title: 'BOM children — pinned versions',
               present: (cmp) => (cmp.submodules || []).length > 0 },
};

function cmpStack(host, number, cmp, q = {}) {
  if (cmp.commits.length) host.appendChild(cmpCommits(cmp));
  const rows = PLM_DIFF.rows(cmp);
  for (const [kind, spec] of Object.entries(FEATURED_KINDS)) {
    if (!spec.present(cmp)) continue;
    // the BOM card is EVERY pin, so it gets a row that names no one path
    const row = (kind !== 'submodule' && rows.find(r => r.kind === kind))
      || { kind, key: '', path: '', fields: [] };
    host.appendChild(PLM_DIFF.card(spec.title, row, cmpCtx(number, cmp)));
  }
  // A `file=` link is pointed at one row, so the top overlay must not grab the
  // one 3-D room out from under it.
  host.appendChild(cmpGeometry(number, cmp, q.mesh, !q.file && !q.quiet));
  host.appendChild(cmpFiles(cmp, number, q, rows));
}

/* ==========================================================================
 * THE PROPOSAL PAGE — /<n>/pull/<pid> (a round: /<n>/proposal/<approval_id>)
 *
 * The charter reserves three things for humans: approve, purchase, visualize.
 * This page is the first one. Everything above it in this file is a way of
 * LOOKING at the repo; this is the one surface where a person decides, and the
 * decision moves `main` and writes a tag.
 *
 * It is the compare view with three things around it — who is asking, what the
 * release gate will say, and the button — and the compare view itself is
 * `cmpStack`, the same code the standalone view uses. Forking it would have
 * meant the page you explore from and the page you sign from could disagree
 * about what changed.
 * ========================================================================= */
async function renderProposal(number, spec) {
  REPO = null;
  CURRENT_ITEM = number;
  if (window.REVIEW_UI) REVIEW_UI.end();
  // ⚠️ UNDOCK FIRST. The 3-D room may be docked INSIDE this page (the overlay
  // card); wiping the page without putting #viewer back on <body> destroys the
  // one viewer element, and the render loop then throws on every frame. A
  // route change always did this (route() → closeViewer); a re-render after a
  // decision did not — found by the stage-13 phone flow (Reject → re-render).
  closeViewer();
  const page = $('page');
  page.innerHTML = '';
  const raw = String(spec || '').split(/[/?]/)[0];
  // STAGE 13: `p<n>` is the PROPOSAL (#n, the object that survives review
  // rounds). A bare number is the stage-4 form — an APPROVAL id, i.e. one
  // round — and is redirected to the proposal it belongs to, so every link
  // ever handed out keeps working.
  let pid = /^p\d+$/.test(raw) ? parseInt(raw.slice(1), 10) : 0;
  const crumbs = el('div', 'crumbs');
  const back = el('a', null, `← ${number}`);
  back.href = tabPath(number, 'drafts');
  crumbs.appendChild(back);
  const crumbTail = document.createTextNode('  /  proposal');
  crumbs.appendChild(crumbTail);
  page.appendChild(crumbs);
  if (!pid) {
    const aid = parseInt(raw, 10);
    if (!aid) {
      page.appendChild(el('div', 'empty',
        'A proposal is /N/pull/<number> (or /N/proposal/<approval id>).'));
      return;
    }
    try {
      const b = await api(`/api/proposals/by-approval/${aid}`);
      pid = b.id;
      history.replaceState(null, '', pullPath(number, pid));
    } catch (e) {
      page.appendChild(el('div', 'empty', `Could not find approval ${aid}: ${e.message}`));
      return;
    }
  }
  crumbTail.textContent = `  /  proposal  ${PLM_ROUTES.proposalLabel(pid)}`;
  const wait = el('div', 'doc');
  wait.appendChild(el('div', 'empty', `reading proposal ${PLM_ROUTES.proposalLabel(pid)}…`));
  page.appendChild(wait);

  let p;
  try { p = await api(`/api/proposals/${pid}`); }
  catch (e) {
    wait.innerHTML = '';
    wait.appendChild(el('div', 'empty', `Could not read proposal ${PLM_ROUTES.proposalLabel(pid)}: ${e.message}`));
    return;
  }
  if (CURRENT_ITEM !== number) return;
  wait.remove();
  await whoami();
  const P = p.proposal;
  pageTitle(P.title, PLM_ROUTES.proposalLabel(pid), number);
  // A draft proposal has no round yet; the header and the gate read the
  // proposal's own fields in the shape a round would have given them.
  if (!p.approval) {
    p.approval = { id: null, target_state: P.target_maturity || 'PROTOTYPE',
                   requested_by: P.author, requested_at: P.created_at,
                   note: P.body_md || '', decision: '' };
  }

  const g = p.git || {}, rev = p.revision;
  // THE REVIEW CONTEXT (web/review.js): one scope, the head and base shas the
  // threads anchor to. A stage-14 change page passes N proposals here.
  const reviewing = window.REVIEW_UI && g.origin;
  if (reviewing) {
    await REVIEW_UI.begin({ scope: [['proposal', pid]], number, pid,
      head: P.head_sha, base: P.base_sha, canApply: P.state === 'draft'
        || P.state === 'rejected' });
  }
  page.appendChild(propHeader(number, p));

  // The gate, BEFORE the click. A rule you only ever meet as a refusal is a
  // trap; §5.1's is knowable from the tree, so it is on the page.
  const gate = propGate(number, p);
  if (gate) page.appendChild(gate);

  // Decision first on a phone-sized flow? No — the bytes come first (you sign
  // what you have seen); but the open-thread count is in the header, so it is
  // impossible to reach the button without having been told.
  const cmpBox = el('div');
  page.appendChild(cmpBox);
  if (reviewing) page.appendChild(REVIEW_UI.panel());
  const timelineBox = el('div');
  page.appendChild(timelineBox);
  timelineBox.appendChild(propTimeline(number, p));
  if (reviewing) {
    REVIEW_UI.on((ts) => {
      const tl = $('propThr');
      if (tl) propThrCount(tl, {
        open: ts.filter(t => t.state === 'open').length,
        blocking: ts.filter(t => t.state === 'open' && t.blocking).length });
    });
    // the timeline follows the threads: a new thread, a resolve, an applied
    // suggestion is an entry the moment it happens
    REVIEW_UI.on(async () => {
      if (!document.body.contains(timelineBox)) return;
      try {
        const t = await api(`/api/timeline?proposal=${pid}`);
        timelineBox.innerHTML = '';
        timelineBox.appendChild(propTimeline(number, { ...p, timeline: t.timeline }));
      } catch (_) { /* keep the old one */ }
    });
  }
  const slot = el('div', 'checks-slot');
  page.appendChild(slot);
  if (window.CHECKS) CHECKS.mountProposal(page, number, { ...p, revision: { ...rev, git_commit: P.head_sha || rev.git_commit } });  // STAGE 6 HOOK (web/checks.js): the §2.6 board, at the head the proposal is about
  const decideBox = el('div');
  page.appendChild(decideBox);
  decideBox.appendChild(propDecideP(number, p));

  if (!g.origin || !g.base || !g.head) {
    const card = el('div', 'doc');
    card.innerHTML = '<h3>What changed</h3>';
    card.appendChild(el('div', 'empty', p.git_mode === 'off'
      ? 'Git-first is off for this project, so there is no branch to diff — '
        + 'the registry is the record. The item page has every file and the '
        + 'revision-to-revision diff.'
      : !g.origin
      ? `v${rev.version} has no commit in ${number}.git — it is a revision plm `
        + 'authored, not one that arrived as a push, so there is no branch to '
        + 'diff. The Record tab has what the database holds.'
      : `Nothing to diff: ${number} has no earlier ref to compare `
        + `${g.head || 'this draft'} against.`));
    cmpBox.appendChild(card);
    propTallyNone('no file changes');
    return;
  }
  const loading = el('div', 'doc');
  loading.appendChild(el('div', 'empty', `diffing ${g.base} → ${g.head}…`));
  cmpBox.appendChild(loading);
  let cmp;
  try {
    cmp = await repoGet(number, 'compare',
      `base=${encodeURIComponent(g.base)}&head=${encodeURIComponent(g.head)}`);
  } catch (e) {
    loading.innerHTML = '';
    loading.appendChild(el('div', 'empty', `Could not diff: ${e.message}`));
    propTallyNone('could not diff');
    return;
  }
  if (CURRENT_ITEM !== number) return;
  loading.remove();
  // A line on the BASE side of this diff anchors at the base commit the diff
  // was actually computed against.
  if (reviewing && REVIEW_UI.ctx) REVIEW_UI.ctx.base = cmp.base.commit || P.base_sha;
  // the one legend, now that the two sides' versions and commits are known
  const refsRow = $('propRefs');
  if (refsRow) {
    const lg = el('div', 'proplegend');
    lg.appendChild(PLM_DIFF.legend(cmpCtx(number, cmp), {}));
    refsRow.after(lg);
  }
  // The header's tally could only be filled once the diff was in.
  const tally = $('propTally');
  if (tally) {
    // A submodule pin bump is not a file edit, and counting it as one is how
    // "1 file changed, +1 −1" comes to mean "this assembly now uses a different
    // child revision". Pins are counted separately here and on the Drafts tab /
    // approval queue, so the same proposal reads the same on all three.
    const subs = new Set((cmp.submodules || []).map(s => s.path));
    let nf = 0, na = 0, nd = 0;
    for (const f of cmp.files || []) {
      if (subs.has(f.path)) continue;
      nf++; na += f.additions || 0; nd += f.deletions || 0;
    }
    tally.innerHTML = '';
    tally.appendChild(el('div', 'lbl', 'Changed'));
    tally.appendChild(el('div', 'n', `${nf} file${nf === 1 ? '' : 's'}`));
    const n = el('div', 'n');
    n.append(el('span', 'plus', `+${na}`), document.createTextNode(' '),
             el('span', 'minus', `−${nd}`));
    tally.appendChild(n);
    if (subs.size) {
      const pb = el('div');
      pb.appendChild(el('span', 'pinbump',
        `${subs.size} pin bump${subs.size === 1 ? '' : 's'}`));
      tally.appendChild(pb);
    }
    const full = el('a', 'btn tiny', 'open the full compare ⇄');
    full.href = cmpPath(number, g.base, g.head);
    full.style.marginTop = '4px';
    tally.appendChild(full);
  }
  cmpStack(cmpBox, number, cmp, {});
}

/* ============================================================== the release
 * A PROMOTION of a revision that already exists. No diff, because nothing
 * changed — that is the whole idea. What there is instead is a workflow: a
 * checklist somebody attests line by line, a quorum of approvals, the
 * released-children gate, generated notes, and (afterwards) a frozen evidence
 * document whose sha256 is in a git note.
 *
 * It reuses the proposal page's furniture deliberately — `refusalBox`, the
 * decision card's shape, `stamp`, `whoChip` — because the page Geoff explores
 * from and the page he signs from must not be able to disagree about anything.
 */
/* A checklist line attested BY REFERENCE (stage 10, plm/release.py): the claim
 * that proves it — a coupon revision whose coupon_for pins the exact commit
 * being released — as a link to that coupon, with what it found. */
function evidenceRef(ref, claim) {
  const box = el('span', 'evref');
  const num = ref.split('@')[0];
  const a = el('a', 'num', ref.replace('@', ' '));
  a.href = itemPath(num);
  box.append(document.createTextNode('by reference to '), a);
  if (claim) {
    box.appendChild(el('span', 'resultchip ' + claim.result, claim.result));
    const m = Object.entries(claim.measured || {})
      .map(([k, v]) => `${k} ${v}`).join(' · ');
    box.title = `pinned at ${claim.commit.slice(0, 10)}`
      + (claim.result_note ? ` — ${claim.result_note}` : '') + (m ? `\n${m}` : '');
  }
  return box;
}

async function renderRelease(number, spec) {
  REPO = null;
  CURRENT_ITEM = number;
  const page = $('page');
  page.innerHTML = '';
  const id = parseInt(String(spec || '').split(/[/?]/)[0], 10);
  const crumbs = el('div', 'crumbs');
  const back = el('a', null, `← ${number}`);
  back.href = tabPath(number, 'releases');
  crumbs.append(back, document.createTextNode(`  /  release  #${id || '?'}`));
  page.appendChild(crumbs);
  if (!id) {
    page.appendChild(el('div', 'empty',
      'A release is /N/releases/tag/<vX.Y.Z> or /N/releases/<id>'));
    return;
  }
  const wait = el('div', 'doc');
  wait.appendChild(el('div', 'empty', `reading release #${id}…`));
  page.appendChild(wait);
  let R;
  try { R = await api(`/api/releases/${id}`); }
  catch (e) {
    wait.innerHTML = '';
    wait.appendChild(el('div', 'empty', `Could not read release #${id}: ${e.message}`));
    return;
  }
  if (CURRENT_ITEM !== number) return;
  wait.remove();
  await whoami();
  if (window.REVIEW_UI) REVIEW_UI.end();
  drawRelease(page, number, id, R);
}

function drawRelease(page, number, id, R) {
  page.innerHTML = '';
  const crumbs = el('div', 'crumbs');
  const back = el('a', null, `← ${number}`);
  back.href = tabPath(number, 'releases');
  crumbs.append(back, document.createTextNode(`  /  release  #${id}`));
  page.appendChild(crumbs);
  const again = async () => {
    const fresh = await api(`/api/releases/${id}`);
    drawRelease(page, number, id, fresh);
  };
  page.appendChild(relHeader(number, R));
  const blockers = R.blockers || [];
  if (blockers.length) page.appendChild(relGate(number, R));
  page.appendChild(relWorkflow(number, id, R, again));
  page.appendChild(relNotes(number, id, R, again));
  if ((R.pins || []).length) page.appendChild(relPins(number, R));
  // STAGE 13: the release's own threads — where "printed 12, all fit" (or "one
  // cracked — hold it") is recorded against the revision. A BLOCKING one holds
  // the promotion (ops.threads_gate_check) until it is resolved, or until an
  // approver ships over it WITH A REASON.
  if (window.REVIEW_UI) {
    if (!REVIEW_UI.ctx || REVIEW_UI.ctx.rid !== id) {
      REVIEW_UI.begin({ scope: [['release', id]], number, rid: id,
                        head: '', base: '' });
    }
    page.appendChild(REVIEW_UI.panel());
    REVIEW_UI.reload();
  }
  page.appendChild(relDecision(number, id, R, again));
}

function relHeader(number, R) {
  const r = R.release, wf = R.workflow || {};
  const box = el('div'); box.id = 'prophead';
  const main = el('div', 'main');
  main.appendChild(el('div', 'ttl',
    `${number} v${r.version} → ${String(r.to_maturity || '').toUpperCase()}`));
  const sub = el('div', 'sub');
  const it = el('a', 'num', number);
  it.href = itemPath(number);
  sub.append(it, el('span', 'nm', r.item_name || ''));
  sub.appendChild(stamp(r.state === 'approved'
    ? String(r.to_maturity || '').toUpperCase()
    : (r.state || '').toUpperCase()));
  sub.appendChild(el('span', 'targetchip',
    `${r.from_maturity || '—'} → ${r.to_maturity}`));
  if (r.decided_kind) {
    const c = whoChip(r.decided_kind);
    if (c) sub.appendChild(c);
  }
  main.appendChild(sub);
  const says = el('div', 'body');
  says.textContent = 'A release changes no files and mints no version. It '
    + 'promotes a revision that already exists, and the decision is recorded '
    + 'as a note on the release commit — the tag does not move.';
  main.appendChild(says);
  const line = el('div', 'range');
  const tag = el('a', 'rf', r.tag || `v${r.version}`);
  tag.href = treePath(number, r.tag || `v${r.version}`);
  line.append(el('span', 'lbl', 'promoting'), tag,
              el('span', 'sha', (r.tag_sha || '').slice(0, 10)));
  main.appendChild(line);
  const meta = el('div', 'range');
  meta.append(el('span', 'lbl', 'workflow'),
              el('span', 'rf', wf.label || wf.key || '—'));
  if (wf.registered === false) {
    meta.appendChild(el('span', 'pinwarn',
      'generic — nobody has written a checklist for this transition yet'));
  }
  meta.append(el('span', 'lbl', 'requested by'),
              el('span', 'rf', r.requested_by || '—'));
  main.appendChild(meta);
  box.appendChild(main);

  const side = el('div', 'side');
  const tally = el('div', 'tally');
  const p = R.progress || {};
  tally.appendChild(el('div', 'lbl', 'Signoffs'));
  tally.appendChild(el('div', 'n',
    `${(p.approvals || {}).have || 0}/${(p.approvals || {}).required || 0} approvals`));
  const done = (p.checklist || []).filter(l => l.done).length;
  tally.appendChild(el('div', 'n',
    `${done}/${(p.checklist || []).length} checklist`));
  if (p.ready) tally.appendChild(el('span', 'latestchip', 'ready'));
  if (R.evidence_url) {
    const ev = el('a', 'btn tiny primary', 'evidence ↓');
    ev.href = R.evidence_url;
    ev.target = '_blank';
    ev.title = `sha256 ${r.evidence_sha256} — the frozen document, byte for `
      + 'byte as it was hashed';
    ev.style.marginTop = '4px';
    tally.appendChild(ev);
    const sha = el('div', 'tr');
    sha.style.cssText = 'font:10.5px var(--mono);color:var(--ink-3);'
      + 'word-break:break-all;margin-top:3px';
    sha.textContent = (r.evidence_sha256 || '').slice(0, 32) + '…';
    tally.appendChild(sha);
  }
  side.appendChild(tally);
  box.appendChild(side);
  return box;
}

/* The §5.1 gate, before the click — the same rule the 409 states, shown while
 * it can still be fixed. Read from each child's `refs/notes/plm` status, not
 * from the presence of a tag: a PROTOTYPE tag is still a tag. */
function relGate(number, R) {
  const card = el('div', 'doc gate');
  const h = el('h3', null, 'Blocked — you cannot release above your least-mature child');
  card.appendChild(h);
  card.appendChild(el('div', 'pad note',
    'Every `parts/*` pin at this tag has to be RELEASED itself, by STATUS and '
    + 'not merely by sitting on a tag (§5.1). Release the child first — it is '
    + 'the same promotion, one level down.'));
  const tbl = el('table', 'led');
  const tb = el('tbody'); tbl.appendChild(tb);
  for (const g of R.blockers || []) {
    const tr = el('tr');
    tr.appendChild(el('td', 'dim', g.path));
    const td = el('td', 'ver');
    const lk = el('a', null, `${g.child} v${g.version || '?'}`);
    lk.href = tabPath(g.child, 'releases');
    td.append(lk, document.createTextNode('  '),
              el('span', 'pinwarn', g.why || 'not released'));
    tr.appendChild(td);
    tb.appendChild(tr);
  }
  card.appendChild(tbl);
  return card;
}

/* THE WORKFLOW AS A SIGNABLE CHECKLIST. Teamcenter's do-tasks, with names on
 * them. Each line is its own row in `release_signoffs`, bound to the tag sha —
 * a signature over the exact bytes, which is what a revision-level signoff
 * never was. */
function relWorkflow(number, id, R, again) {
  const wf = R.workflow || {}, p = R.progress || {};
  const open = ['requested', 'in_review'].includes(R.release.state);
  const card = el('div', 'doc wf');
  const h = el('h3', null, 'The workflow');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    (p.approvals || {}).policy ? (p.approvals.policy.label || '') : ''));
  card.appendChild(h);

  // -- checks -------------------------------------------------------------
  const cbox = el('div', 'pad');
  for (const c of p.checks || []) {
    const row = el('div', 'wfrow');
    row.appendChild(el('span', 'mark ' + c.status,
      c.status === 'pass' ? '✓' : c.status === 'fail' ? '✕' : '–'));
    const t = el('div', 'txt');
    t.appendChild(el('div', 'line', `check · ${c.name}`));
    t.appendChild(el('div', 'by', c.summary));
    row.appendChild(t);
    cbox.appendChild(row);
  }
  if ((p.checks || []).length) card.appendChild(cbox);

  // -- the checklist -------------------------------------------------------
  const box = el('div', 'pad');
  if (!(p.checklist || []).length) {
    box.appendChild(el('div', 'note',
      'This transition has no checklist registered — only the approval quorum '
      + 'applies. Add one in plm/release.py RELEASE_WORKFLOWS.'));
  }
  for (const ln of p.checklist || []) {
    const row = el('div', 'wfrow' + (ln.done ? ' done' : ''));
    row.appendChild(el('span', 'mark ' + (ln.done ? 'pass' : 'todo'),
      ln.done ? '✓' : '☐'));
    const t = el('div', 'txt');
    t.appendChild(el('div', 'line', ln.line));
    if (ln.done) {
      const by = el('div', 'by');
      by.append(document.createTextNode(`attested by ${ln.signed_by}`));
      const c = whoChip(ln.signer_kind === 'bot' ? 'bot' : '');
      if (c) by.appendChild(c);
      if (ln.at) by.appendChild(when(ln.at));
      // Stage 10: attested BY REFERENCE — the claim that proves it, linked.
      if (ln.evidence) by.appendChild(evidenceRef(ln.evidence,
        (ln.by_reference || []).find(c2 => c2.ref === ln.evidence)));
      if (ln.note) by.appendChild(el('div', 'why', ln.note));
      t.appendChild(by);
    } else if ((ln.by_reference || []).length) {
      t.appendChild(el('div', 'by',
        `can be attested by reference to a passing ${ln.evidence_type || 'claim'}`
        + ' on this exact commit:'));
    }
    row.appendChild(t);
    if (open && !ln.done) {
      for (const c2 of ln.by_reference || []) {
        const rb = el('button', 'btn tiny evbtn');
        rb.append(document.createTextNode('Attest by reference · '),
          el('b', null, `${c2.from} v${c2.from_version}`),
          el('span', 'resultchip ' + c2.result, c2.result));
        rb.title = `${c2.from} v${c2.from_version} claims ${ln.evidence_type} `
          + `at ${c2.commit.slice(0, 10)}${c2.exact ? '' : ' (the reviewed commit '
          + 'this release commit was made from)'} — ${c2.result_note || 'no note'}`;
        rb.onclick = async () => {
          rb.disabled = true;
          try {
            await post(`/api/releases/${id}/signoffs`, decisionBody(
              { kind: 'checklist', item: ln.line, verdict: 'attest',
                evidence: c2.ref }));
            again();
          } catch (e) {
            rb.disabled = false;
            row.appendChild(refusalBox(e));
          }
        };
        t.appendChild(rb);
      }
    }
    if (open && !ln.done) {
      const b = el('button', 'btn tiny', 'I attest');
      b.title = 'Sign this line in your name. It becomes a row bound to the '
        + 'tag sha and goes into the evidence file.';
      b.onclick = async () => {
        const note = prompt(`Attest: ${ln.line}\n\nYour evidence (optional):`) ?? null;
        if (note === null) return;
        b.disabled = true;
        try {
          await post(`/api/releases/${id}/signoffs`, decisionBody(
            { kind: 'checklist', item: ln.line, verdict: 'attest', note }));
          again();
        } catch (e) {
          b.disabled = false;
          row.appendChild(refusalBox(e));
        }
      };
      row.appendChild(b);
    }
    box.appendChild(row);
  }
  card.appendChild(box);

  // -- the approvals -------------------------------------------------------
  const ap = p.approvals || {};
  const abox = el('div', 'pad wfquorum');
  const line = el('div', 'wfrow' + (ap.met ? ' done' : ''));
  line.appendChild(el('span', 'mark ' + (ap.met ? 'pass' : 'todo'),
    ap.met ? '✓' : '☐'));
  const t = el('div', 'txt');
  t.appendChild(el('div', 'line',
    `${ap.have || 0} of ${ap.required || 0} approval`
    + `${(ap.required || 0) === 1 ? '' : 's'}`));
  const who = el('div', 'by');
  who.textContent = (ap.by || []).length
    ? (ap.by || []).map(s => `${s.signer} (${s.kind})`).join(', ')
    : `nobody yet — ${(ap.who_may || []).join(' or ')} may sign here`;
  t.appendChild(who);
  if (ap.policy) {
    const why = el('div', 'why');
    why.textContent = `${ap.policy.rule}  [${ap.policy.source}]`;
    t.appendChild(why);
  }
  line.appendChild(t);
  abox.appendChild(line);
  card.appendChild(abox);
  return card;
}

/* Generated, then edited (GitHub's auto-notes). Editable until the release is
 * approved — after that the text is inside the frozen evidence document and
 * changing it would make the recorded sha256 a claim about something that no
 * longer exists, which the API refuses and this says. */
function relNotes(number, id, R, again) {
  const r = R.release;
  const open = ['requested', 'in_review'].includes(r.state);
  const card = el('div', 'doc');
  const h = el('h3', null, 'Release notes');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    open ? 'generated from what merged since the last release — edit it'
         : 'frozen inside the evidence document'));
  card.appendChild(h);
  const body = el('div', 'pad');
  const view = el('div', 'md');
  view.innerHTML = mdToHtml(r.notes_md || '_No notes._');
  body.appendChild(view);
  if (open) {
    const acts = el('div', 'acts');
    const ed = el('button', 'btn tiny', 'Edit');
    const re = el('button', 'btn tiny', 'Re-generate');
    re.title = 'Draft again from the proposals, pin bumps and attribute deltas '
      + 'since the previous RELEASED revision';
    acts.append(ed, re);
    body.appendChild(acts);
    const editor = () => {
      const ta = el('textarea', 'decnote');
      ta.style.minHeight = '260px';
      ta.value = r.notes_md || '';
      const save = el('button', 'btn primary tiny', 'Save');
      const cancel = el('button', 'btn tiny', 'Cancel');
      const row = el('div', 'acts'); row.append(save, cancel);
      view.replaceWith(ta);
      acts.replaceWith(row);
      save.onclick = async () => {
        save.disabled = true;
        try {
          await post(`/api/releases/${id}/notes`,
                     { notes_md: ta.value, actor: ACTOR });
          again();
        } catch (e) { save.disabled = false; body.appendChild(refusalBox(e)); }
      };
      cancel.onclick = () => again();
    };
    ed.onclick = editor;
    re.onclick = async () => {
      re.disabled = true;
      try {
        const d = await api(`/api/releases/${id}/notes-draft`);
        await post(`/api/releases/${id}/notes`,
                   { notes_md: d.notes_md, actor: ACTOR });
        again();
      } catch (e) { re.disabled = false; body.appendChild(refusalBox(e)); }
    };
  }
  card.appendChild(body);
  return card;
}

/* The pin closure with maturity marks — the baseline, written down. A release
 * tag on an assembly pins every descendant by gitlink, so "what exactly was
 * this?" is a `git checkout`; this is the same list the evidence freezes. */
function relPins(number, R) {
  const card = el('div', 'doc');
  const h = el('h3', null, 'What is in it');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    `${(R.pins || []).length} pinned descendant`
    + `${(R.pins || []).length === 1 ? '' : 's'}, by gitlink`));
  card.appendChild(h);
  const tbl = el('table', 'led');
  const tb = el('tbody'); tbl.appendChild(tb);
  for (const p of R.pins || []) {
    const tr = el('tr');
    const td0 = el('td', 'dim');
    td0.textContent = (p.parent === number ? '' : '↳ ') + p.path;
    tr.appendChild(td0);
    const td = el('td', 'ver');
    if (p.child === 'catalog') {
      td.append(el('span', null, 'catalog'), document.createTextNode('  '),
                el('span', 'dim', 'purchased items — never gates a release'));
    } else {
      const lk = el('a', null, `${p.child} v${p.version || '?'}`);
      lk.href = itemPath(p.child);
      td.append(lk, document.createTextNode('  '));
      td.appendChild(p.released
        ? el('span', 'latestchip', 'released')
        : el('span', 'pinwarn', p.state || 'not released'));
    }
    tr.appendChild(td);
    tb.appendChild(tr);
  }
  card.appendChild(tbl);
  return card;
}

function relDecision(number, id, R, again) {
  const r = R.release, p = R.progress || {};
  const card = el('div', 'doc decide');
  const h = el('h3', null, 'Decision');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    (ME && ME.verified && ME.actor) ? `as ${ME.actor}` : `as ${ACTOR}`));
  card.appendChild(h);
  if (!['requested', 'in_review'].includes(r.state)) {
    const done = el('div', 'pad');
    const box = el('div', 'approved');
    const hd = el('div', 'hd');
    const okish = r.state === 'approved';
    hd.append(el('span', 'x', okish ? '✓' : '✕'),
              el('span', 'lbl', okish
                ? `RELEASED — v${r.version} is ${String(r.to_maturity).toUpperCase()}`
                : `${r.state} — nothing was promoted`));
    box.appendChild(hd);
    const tbl = el('table', 'led');
    const tb = el('tbody'); tbl.appendChild(tb);
    const row = (k, v) => {
      if (v == null || v === '') return;
      const tr = el('tr');
      tr.appendChild(el('td', 'dim', k));
      const td = el('td', 'ver');
      if (v instanceof Node) td.appendChild(v); else td.textContent = String(v);
      tr.appendChild(td);
      tb.appendChild(tr);
    };
    const byCell = el('span');
    byCell.append(document.createTextNode(r.decided_by || '—'));
    const c = whoChip(r.decided_kind);
    if (c) byCell.appendChild(c);
    row('decided by', byCell);
    if (r.decided_at) row('when', fmtExact(r.decided_at));
    if (r.decision_note) row('note', r.decision_note);
    if (okish) {
      const tg = el('a', null, r.tag);
      tg.href = treePath(number, r.tag);
      row('tag', tg);
      row('the tag did not move', `${(r.tag_sha || '').slice(0, 10)} — before `
        + 'and after. Nothing was committed on main.');
      if (r.note_commit) {
        row('the decision, in git', `${gitNotesRefLabel()} @ `
          + `${r.note_commit.slice(0, 10)}`);
      }
      if (r.evidence_sha256) {
        const ev = el('a', null, 'evidence.json');
        ev.href = `/api/releases/${id}/evidence?download=1`;
        ev.target = '_blank';
        const wrap = el('span');
        wrap.append(ev, document.createTextNode('  sha256 ' + r.evidence_sha256));
        row('evidence', wrap);
      }
    }
    box.appendChild(tbl);
    done.appendChild(box);
    card.appendChild(done);
    return card;
  }
  const body = el('div', 'pad');
  const why = el('div', 'note');
  why.textContent = 'Approving writes a `[status]` note on the release commit '
    + `and moves v${r.version} to ${r.to_maturity}. No tag moves; nothing is `
    + 'committed on main. Any checklist line nobody has signed is attested in '
    + 'your name, as its own row.';
  body.appendChild(why);
  const note = el('textarea', 'decnote');
  note.placeholder = 'The reason, for the record — it goes into the git note, '
    + 'the event log and the frozen evidence document.';
  body.appendChild(note);
  const acts = el('div', 'acts');
  const ok = el('button', 'btn primary',
                `Approve → ${String(r.to_maturity).toUpperCase()}`);
  const no = el('button', 'btn', 'Reject');
  const wd = el('button', 'btn', 'Withdraw');
  acts.append(ok, no, wd);
  body.appendChild(acts);
  const out = el('div', 'out');
  body.appendChild(out);
  card.appendChild(body);
  const run = async (fn, after) => {
    out.innerHTML = '';
    ok.disabled = no.disabled = wd.disabled = true;
    try { const r2 = await fn(); after(r2); }
    catch (e) {
      ok.disabled = no.disabled = wd.disabled = false;
      out.appendChild(refusalBox(e));
      out.scrollIntoView({ block: 'nearest' });
    }
  };
  ok.onclick = () => run(
    () => post(`/api/releases/${id}/decide`,
               decisionBody({ decision: 'approved', note: note.value.trim() })),
    () => again());
  no.onclick = () => run(
    () => post(`/api/releases/${id}/decide`,
               decisionBody({ decision: 'rejected', note: note.value.trim() })),
    () => again());
  wd.onclick = () => run(
    () => post(`/api/releases/${id}/withdraw`,
               decisionBody({ note: note.value.trim() })),
    () => { toast(`release #${id} withdrawn`);
            navigate(tabPath(number, 'releases')); });
  return card;
}

const gitNotesRefLabel = () => 'refs/notes/plm';

/* Submit, and when the EXPLODE GATE refuses (ops.explode_gate — an assembly
 * of made children that will not pull apart in the viewer), ask the person for
 * the waiver right there instead of leaving them with a 409 and no field to
 * answer it in. The refusal text names every child and its fix; the waiver is
 * recorded on the approval, where the proposal page shows it. */
async function postSubmit(number, body) {
  const url = `/api/items/${encodeURIComponent(number)}/submit`;
  try { return await post(url, body); }
  catch (e) {
    if (!/explode_waiver/.test(e.message || '')) throw e;
    const why = prompt(e.message + '\n\nSubmit anyway? Say why — it is recorded '
      + 'on the approval as an explode waiver:');
    if (!why || !why.trim()) throw e;
    return post(url, { ...body, explode_waiver: why.trim() });
  }
}

function propHeader(number, p) {
  const g = p.git || {}, a = p.approval, rev = p.revision;
  const box = el('div'); box.id = 'prophead';
  const main = el('div', 'main');
  // The title is the commit's subject: what the author said they did. The
  // approver's note (`-o title=`) is the covering letter, not the change.
  main.appendChild(el('div', 'ttl', rev.summary || `${number} v${rev.version}`));
  const sub = el('div', 'sub');
  // The page is linkable from a notification, so it says what it is ABOUT
  // before it says anything else about the proposal.
  const on = el('a', 'on', `${number} · ${p.item.name || ''}`.trim());
  on.href = itemPath(number);
  sub.appendChild(on);
  // The stamp is the REVISION's state, never a translation of the decision: an
  // approval at PROTOTYPE that stamped RELEASED would be the page lying about
  // the one fact the whole gate exists to get right.
  const st = stamp(rev.state || 'IN_REVIEW'); st.id = 'propStamp';
  sub.appendChild(st);
  const P = p.proposal;
  sub.appendChild(el('span', 'pnum', P ? PLM_ROUTES.proposalLabel(P.id) : `#${a.id}`));
  const who = a.requested_by || rev.author || 'somebody';
  if (P && P.state === 'draft' && !a.id) {
    sub.appendChild(el('span', null, `${P.author || who}'s draft of v${rev.version}`
      + ' — not submitted yet'));
  } else {
    sub.appendChild(el('span', null,
      `${who} wants v${rev.version} to be ${(a.target_state || '').toLowerCase()}`));
  }
  sub.appendChild(when(a.requested_at));
  if (P) {
    // The proposal's own state and its rounds: one object across every
    // reject → re-push → resubmit (§2.1), so "round 2" is a fact to show.
    const ps = el('span', 'pstate ps-' + P.state, P.state);
    ps.title = 'The proposal (the pull request), not the revision: draft · open · '
      + 'rejected (re-opens on the next push) · merged · withdrawn';
    sub.appendChild(ps);
    if ((P.rounds || []).length > 1) sub.appendChild(el('span', 'dim',
      `round ${P.rounds.length}`));
    const tl = el('a', 'thrcount');
    tl.id = 'propThr';
    tl.href = 'javascript:void 0';
    tl.onclick = () => { const n = $('rvpanel'); if (n) n.scrollIntoView({ block: 'start', behavior: 'smooth' }); };
    propThrCount(tl, p.threads || {});
    sub.appendChild(tl);
  }
  if (a.decision) {
    sub.appendChild(el('span', 'decided',
      `${a.decision} by ${a.approver || '—'}`));
  }
  main.appendChild(sub);
  // The covering letter, and — set apart, because it is a decision the
  // approver is being asked to accept — the explode waiver, if one was given.
  // It is recorded on the approval's note under master's own marker
  // (ops.EXPLODE_WAIVER_MARK); the API hands it back parsed.
  const letter = a.explode_waiver
    ? (a.note || '').slice(0, (a.note || '').lastIndexOf('[explode waiver]')).trim()
    : a.note;
  if (letter) main.appendChild(el('div', 'letter', letter));
  if (a.explode_waiver) {
    const w = el('div', 'waiver');
    w.appendChild(el('b', null, 'Explode waiver'));
    w.appendChild(document.createTextNode(
      ' — submitted although this assembly does not explode in the viewer'
      + (p.explode && p.explode.reason ? ` (${p.explode.reason})` : '') + ': '));
    w.appendChild(el('q', null, a.explode_waiver));
    main.appendChild(w);
  }

  // old → new, as refs you can go and look at.
  const refsRow = el('div', 'refs');
  refsRow.id = 'propRefs';
  const mk = (label, ref, cls) => {
    if (!ref) return;
    // old / new in the §2.9 side colours, the same words as every diff below
    const which = cls === 'base' ? 'old' : 'new';
    refsRow.appendChild(PLM_DIFF.sideTag({ which, word: which, version: '', ref, sha: '', date: '' },
                                         '', label));
    const r = el('a', 'rf ' + (cls || ''), ref);
    r.href = treePath(number, ref);
    refsRow.appendChild(r);
  };
  mk('old', g.base, 'base');
  refsRow.appendChild(el('span', 'arrow', '→'));
  mk(g.open ? 'new (branch)' : 'new', g.head, 'head');
  const headSha = (p.proposal && p.proposal.head_sha) || rev.git_commit;
  if (headSha) {
    const sha = el('span', 'sha', headSha.slice(0, 10));
    sha.title = 'The head of this proposal. Approval merges THIS sha — the '
      + 'signature is over these exact bytes, and a later push dismisses it.\n'
      + headSha;
    refsRow.appendChild(sha);
  }
  // no base, no head (git-first off): a lone "←" says nothing — leave it out
  if (g.base || g.head) main.appendChild(refsRow);
  // STAGE 19 — the REVIEW BASELINE (what this diff is against, and why), and
  // the two identities (commit author vs authenticated pusher), in a sentence
  const rb = (p.proposal && p.proposal.review_base) || null;
  if (rb && g.origin) {
    const how = { inferred: 'the draft/release this head descends from',
      'push-option': 'named by the pusher (-o base=)',
      main: 'the merge-base with main', none: '' }[rb.how] || rb.how;
    main.appendChild(el('div', 'dim propbase', rb.sha
      ? `Review baseline ${rb.label || ''} @ ${rb.sha.slice(0, 10)} → ${(rb.head || '').slice(0, 10)} — ${how}.`
      : 'No review baseline — nothing this head descends from; every file reads as new.'));
  }
  const ids = p.identities;
  if (ids && (ids.commit_author || ids.pusher)) {
    main.appendChild(el('div', 'dim propids',
      `Commit author: ${ids.commit_author || '—'} · pushed by: ${ids.pusher || '—'} (verified). ${ids.say}`));
  }
  box.appendChild(main);
  const tally = el('div', 'tally'); tally.id = 'propTally';
  tally.appendChild(el('div', 'lbl', 'Changed'));
  tally.appendChild(el('div', 'n', 'reading…'));
  box.appendChild(tally);
  return box;
}

/* STAGE 19 — a revision's purchase requirements, appended to the BOM card. */
function reqCard(box, number, version) {
  api(`/api/items/${encodeURIComponent(number)}/requirements?version=${encodeURIComponent(version)}`)
    .then(r => {
      const rs = r.requirements || [];
      if (!rs.length) return;
      const t = el('table', 'led');
      t.innerHTML = '<thead><tr><th>Purchase requirement</th><th>Qty</th><th>Status</th></tr></thead>';
      const tb = el('tbody'); t.appendChild(tb);
      for (const q of rs) {
        const tr = el('tr');
        const td = el('td', null, q.requirement);
        if (q.notes) td.title = q.notes;
        tr.append(td, el('td', null, `${q.qty} ${q.unit}`),
          el('td', 'dim', 'requirement — not selected'
            + ((q.candidates || []).length ? ` · candidates ${q.candidates.join(', ')}` : '')));
        tb.appendChild(tr);
      }
      box.appendChild(el('div', 'pad note',
        `${rs.length} purchase requirement${rs.length === 1 ? '' : 's'} — what must be bought; nothing is selected yet. `
        + 'A green mesh or explode check says nothing about these.'));
      box.appendChild(t);
    }).catch(() => {});
}

/* "2 open threads · 1 blocking" in the header — kept current as threads move. */
function propThrCount(tl, tc) {
  tl.className = 'thrcount' + (tc.blocking ? ' blocks' : '');
  tl.textContent = tc.open ? `${tc.open} open thread${tc.open === 1 ? '' : 's'}`
    + (tc.blocking ? ` · ${tc.blocking} blocking` : '') : '';
  tl.style.display = tc.open ? '' : 'none';
}

/* The header's "Changed" tally when there is no diff to count: say so, rather
 * than leave "reading…" on a page that has stopped reading. */
function propTallyNone(text) {
  const tally = $('propTally');
  if (!tally) return;
  tally.innerHTML = '';
  tally.appendChild(el('div', 'lbl', 'Changed'));
  tally.appendChild(el('div', 'n dim', text));
}

/* After a decision the header must say what is now true — the stamp is the
 * revision's state (see propHeader), and the decide response carries it. */
function propStamped(r) {
  const old = $('propStamp');
  if (!old || !r || !r.state) return;
  const st = stamp(r.state); st.id = 'propStamp';
  old.replaceWith(st);
  if (r.decision && !st.parentNode.querySelector('.decided')) {
    st.parentNode.appendChild(el('span', 'decided',
      `${r.decision} by ${r.approver || '—'}`));
  }
}

/* Release gate (§5.1) — every `parts/*` pin at head, and what it means for the
 * target this proposal asks for. `catalog` is listed but never gates: it is one
 * submodule for every purchased part and has no release of its own. */
function propGate(number, p) {
  const gate = p.gate;
  if (!gate || !gate.pins.length) return null;
  const card = el('div', 'doc gatecard' + (gate.blocks ? ' blocks' : ''));
  const h = el('h3', null, 'Release gate — BOM children');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    `target ${(gate.target_state || '').toLowerCase()}`));
  card.appendChild(h);
  const verdict = el('div', 'verdict ' + (gate.blocks ? 'bad'
    : gate.warns ? 'warn' : 'ok'));
  verdict.textContent = gate.blocks
    ? `Approving this at RELEASED will be REFUSED: ${gate.unreleased.length} `
      + `BOM child${gate.unreleased.length === 1 ? ' is' : 'ren are'} pinned at a `
      + 'commit that is not a release, and you cannot release above your '
      + 'least-mature child.'
    : gate.warns
      ? `${gate.unreleased.length} pin${gate.unreleased.length === 1 ? '' : 's'} `
        + 'is not a release. A prototype may depend on prototypes, so this is a '
        + 'warning — it is recorded in the tag annotation.'
      : 'Every BOM child is pinned at a release. This proposal clears §5.1.';
  card.appendChild(verdict);
  for (const pin of gate.pins) {
    const row = el('div', 'gaterow' + (pin.released ? '' : ' loose'));
    const nm = pin.path.startsWith('parts/') ? el('a', 'who', pin.path)
                                             : el('span', 'who', pin.path);
    if (nm.tagName === 'A') nm.href = treePath(pin.child, pin.ref || pin.commit);
    row.appendChild(nm);
    row.appendChild(pinChip(pin));
    if (!pin.released && !pin.path.startsWith('parts/')) {
      const w = el('span', 'pinwarn', '· but the catalog never gates');
      w.title = 'The catalog is ONE submodule for every purchased and hardware '
        + 'child (§5.4). It has no releases of its own, so §5.1 is asked of '
        + 'parts/* only — which is exactly what gitindex.release_gate checks.';
      row.appendChild(w);
    }
    card.appendChild(row);
  }
  return card;
}

/* The timeline (PLM_REVIEW_MODEL §2.8): `events` given a shape. Two pushes of
 * the same sha are ONE thing that happened — `refs/for/main` re-indexes the
 * commit it submits — so they are folded rather than printed twice. */
const PROP_ICON = {
  pushed: '↑', submitted: '⇪', approved: '✓', rejected: '✕',
  withdrawn: '↩', commented: '💬', 'branch-deleted': '⌫',
  // stage 13 — the review model's own entries
  opened: '○', reopened: '↻', dismissed: '⊘', commit: '•', checks: '☐',
  thread: '💬', replied: '↳', resolved: '✓', claimed: '⚑', applied: '⎘',
  'thread-reopened': '↻', reviewed: '👁', requested: '🔔', blocking: '🔔',
  addressed: '🔔', merged: '⇲',
};
function propTimeline(number, p) {
  const card = el('div', 'doc');
  const h = el('h3', null, 'Timeline');
  h.appendChild(el('span', 'right')).appendChild(
    el('span', 'note', 'from the event log'));
  card.appendChild(h);
  const rows = [];
  const seenPush = new Set();
  for (const e of p.timeline || []) {
    if (e.kind === 'pushed') {
      const sha = (e.data && e.data.commit) || '';
      if (seenPush.has(sha)) continue;
      seenPush.add(sha);
    }
    rows.push(e);
  }
  if (!rows.length) {
    card.appendChild(el('div', 'empty', 'Nothing recorded yet.'));
    return card;
  }
  for (const e of rows) {
    const row = el('div', 'tlrow ' + e.kind);
    row.appendChild(el('span', 'ic', PROP_ICON[e.kind] || '·'));
    const body = el('div', 'b');
    const line = el('div', 'l');
    line.appendChild(el('b', null, e.actor || 'plm'));
    const d = e.data || {};
    if (e.kind === 'pushed') {
      line.append(document.createTextNode(' pushed '));
      const sha = el('span', 'sha', (d.commit || '').slice(0, 10));
      line.appendChild(sha);
      if (d.ref) line.append(document.createTextNode(' to '),
                             el('span', 'rf', String(d.ref).replace(/^refs\/heads\//, '')));
      body.appendChild(line);
      body.appendChild(el('div', 'd',
        `indexed as v${d.version || '?'} ${d.state || ''} · ${d.files || 0} file`
        + `${d.files === 1 ? '' : 's'}`
        + (d.bom ? ` · ${d.bom} BOM line${d.bom === 1 ? '' : 's'}` : '')));
    } else if (e.kind === 'submitted') {
      line.append(document.createTextNode(
        ` submitted it for ${(d.target || '').toLowerCase() || 'release'}`));
      body.appendChild(line);
      if (d.note) body.appendChild(el('div', 'd', d.note));
    } else if (e.kind === 'approved' || e.kind === 'rejected') {
      line.append(document.createTextNode(
        ` ${e.kind} it${d.state ? ' → ' + d.state : ''}`));
      body.appendChild(line);
      if (d.note) body.appendChild(el('div', 'd', d.note));
      const gg = d.git;
      if (gg) {
        body.appendChild(el('div', 'd',
          `${gg.merged} · main ${String(gg.main || '').slice(0, 10)}`
          + (gg.tag ? ` · tagged ${gg.tag}` : '')));
      }
    } else if (e.kind === 'withdrawn') {
      line.append(document.createTextNode(' withdrew the submission'));
      body.appendChild(line);
      if (d.note) body.appendChild(el('div', 'd', d.note));
    } else if (e.kind === 'commented') {
      line.append(document.createTextNode(' commented'));
      body.appendChild(line);
      const t = el('div', 'd'); t.style.whiteSpace = 'pre-wrap';
      t.textContent = d.body || '';
      body.appendChild(t);
    } else if (TL_REVIEW[e.kind]) {
      // STAGE 13: commits, checks, threads, dismissals, notifications — one
      // sentence each, from TL_REVIEW below.
      const [verb, detail] = TL_REVIEW[e.kind](d, e);
      line.append(document.createTextNode(' ' + verb));
      body.appendChild(line);
      if (detail) {
        const t = el('div', 'd'); t.style.whiteSpace = 'pre-wrap';
        if (detail instanceof Node) t.appendChild(detail); else t.textContent = detail;
        body.appendChild(t);
      }
    } else {
      // An event type nobody has styled yet is still part of the record, so it
      // is printed as itself with whatever its payload says — a list becomes
      // its length, which is the only readable thing to say about ten
      // attribute names in a timeline row.
      line.append(document.createTextNode(` · ${e.type}`));
      body.appendChild(line);
      const bits = [];
      if (/^bom\./.test(e.type || '') && d.child) {
        // A BOM line is "child × qty", the way a parts list reads.
        bits.push(`${d.child}${d.version ? ' v' + d.version : ''}`
          + (d.qty != null && d.qty !== '' ? ` × ${d.qty}` : '')
          + (d.find != null && d.find !== '' ? ` (find ${d.find})` : ''));
      }
      const shown = bits.length ? new Set(['child', 'version', 'qty', 'find'])
                                : new Set();
      for (const [k, v] of Object.entries(d)) {
        if (shown.has(k) || /_id$/.test(k)) continue;
        if (Array.isArray(v)) { if (v.length) bits.push(`${v.length} ${k}`); }
        // A nested object (the `git` block of a converted write) has no
        // one-line reading; its commit, when it has one, is the useful part.
        else if (v && typeof v === 'object') {
          if (v.commit) bits.push(`${k} ${String(v.commit).slice(0, 10)}`);
        }
        else if (v || v === 0) bits.push(`${k} ${v}`);
      }
      if (bits.length) body.appendChild(el('div', 'd', bits.slice(0, 5).join(' · ')));
    }
    row.appendChild(body);
    row.appendChild(when(e.at));
    card.appendChild(row);
  }
  return card;
}

/* The review model's timeline entries (stage 13): [verb, detail] per kind. */
const _sha = (x) => String(x || '').slice(0, 10);
const TL_REVIEW = {
  commit: (d) => [`committed ${_sha(d.commit)}`, d.subject],
  checks: (d) => [`checks ran on ${_sha(d.commit)}`, Object.entries(d.counts || {})
    .map(([k, v]) => `${v} ${k}`).join(' · ')],
  opened: (d) => [`opened this as a draft proposal from ${String(d.ref || '')
    .replace('refs/heads/', '')}`, ''],
  reopened: (d) => [`pushed ${_sha(d.to)} — the proposal re-opened`,
    'A rejection does not end the object: the same proposal carries on.'],
  dismissed: (d) => [`dismissed ${(d.reviews || []).length} stale review`
    + `${(d.reviews || []).length === 1 ? '' : 's'}`,
    `the head moved to ${_sha(d.head)} — a signoff speaks for the bytes it looked at`],
  reviewed: (d) => [`reviewed (${d.verdict}) at ${_sha(d.head)}`
    + (d.counts ? '' : ' — advisory'), d.body || ''],
  thread: (d) => [`opened ${d.blocking ? 'a BLOCKING ' : 'a '}thread T${d.thread}`
    + (d.anchor && d.anchor !== 'subject' ? ` on ${d.key || ((d.path || d.anchor)
      + (d.line ? ':' + d.line : ''))}` : ''),
    d.title || ''],
  replied: (d) => [`replied on T${d.thread}`, d.body || ''],
  resolved: (d) => [`resolved T${d.thread}`
    + (d.addressed_by ? ` — addressed by ${_sha(d.addressed_by)}` : '')
    + (d.shipped_over ? ' (shipped over a blocking thread)' : ''), d.reason || ''],
  claimed: (d) => [`marked T${d.thread} addressed by ${_sha(d.addressed_by)}`,
    d.reason || ''],
  applied: (d) => [`applied the suggestion on T${d.thread} as ${_sha(d.commit)}`, ''],
  'thread-reopened': (d) => [`reopened T${d.thread}`, ''],
  requested: (d) => ['— notification: review requested', d.headline || d.reason],
  blocking: (d) => ['— notification: a blocking thread', d.headline || ''],
  addressed: (d) => ['— notification: every thread addressed', d.headline || ''],
  'branch-deleted': (d) => ['deleted the branch — withdrawn', d.why || ''],
};

/* What is designed and not built, said plainly and once. A page that silently
 * lacks threads and checks reads as a page where there are none. */
function propSoon() {
  const card = el('div', 'doc soon');
  card.innerHTML = '<h3>Threads · Checks</h3>';
  const b = el('div', 'pad');
  b.appendChild(el('div', 'note',
    'Neither is built yet. Review threads anchored to a file line, a plm.toml '
    + 'key or a point on the mesh (PLM_REVIEW_MODEL §2.4), and per-push checks '
    + 'keyed by SHA — manifest lint, BOM pins released, render matches source '
    + '(§2.6) — are designed and slot in here. Until they exist this page shows '
    + 'no counts rather than zeroes, because a zero would read as a measurement.'));
  const c = el('div', 'note');
  c.style.marginTop = '6px';
  c.textContent = 'Comments on the submission appear in the timeline above.';
  b.appendChild(c);
  card.appendChild(b);
  return card;
}

/* The decision bar. Approve is the merge: it moves `main`, writes the annotated
 * tag and deletes the branch, and the response says which of those happened. */
function propDecision(number, p, id) {
  const a = p.approval, rev = p.revision, g = p.git || {};
  const card = el('div', 'doc decide');
  const h = el('h3', null, 'Decision');
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    (ME && ME.verified && ME.actor) ? `as ${ME.actor}` : `as ${ACTOR}`));
  card.appendChild(h);
  if (a.decision) {
    const done = el('div', 'pad');
    const chip = whoChip(a.approver_kind);
    if (chip) {
      const ln = el('div'); ln.style.marginBottom = '6px';
      ln.appendChild(chip);
      done.appendChild(ln);
    }
    done.appendChild(el('div', 'note',
      `Already ${a.decision}${a.approver ? ' by ' + a.approver : ''}`
      + `${a.decided_at ? ' · ' + fmtExact(a.decided_at) : ''}.`
      + (a.decision_note ? '\n' + a.decision_note : '')));
    done.style.whiteSpace = 'pre-wrap';
    if (rev.git_tag) {
      const p2 = el('div'); p2.style.marginTop = '8px';
      const lk = el('a', 'btn tiny', `the release ${rev.git_tag} →`);
      lk.href = treePath(number, rev.git_tag);
      p2.appendChild(lk);
      done.appendChild(p2);
    }
    card.appendChild(done);
    return card;
  }
  const body = el('div', 'pad');
  const why = el('div', 'note');
  why.textContent = (p.git_mode && p.git_mode !== 'write'
    ? `Approving moves v${rev.version} to ${a.target_state} in the registry `
      + `(git-first is ${p.git_mode} for this project, so nothing is merged or `
      + 'tagged). '
    : `Approving merges ${g.head || 'the draft'} into main, `
      + `writes the annotated tag v${rev.version}, and moves the revision to `
      + `${a.target_state}. `)
    + 'Rejecting sends it back to DRAFT for more work; '
    + 'withdrawing takes it out of the queue without a verdict.';
  body.appendChild(why);
  // WHICH GATE AM I STANDING IN FRONT OF? The effective policy, in one line —
  // so it is visible that this maturity needs a person here, and whether plm
  // would have let an agent through (which is a setting, not a law).
  const pol = el('div', 'policyline');
  pol.appendChild(el('span', 'dim', 'reading the policy…'));
  body.appendChild(pol);
  api('/api/policy?project=' + encodeURIComponent(p.item
      ? (p.item.project || '') : '')).then(pp => {
    const row = (pp.policy || []).find(x => x.target_state === a.target_state);
    pol.innerHTML = '';
    if (pp.policy_applies === false) {
      // `off`/`read`: master's gate — a person with the approver role.
      pol.appendChild(el('span', 'k', 'a person decides'));
      pol.appendChild(el('span', 'v', 'git-first is ' + pp.git_mode.mode
        + ' for this project: no policy applies, and approval needs the '
        + '`approver` role, as it always has.'));
      pol.appendChild(el('span', 'src', `[${pp.git_mode.source}]`));
      return;
    }
    if (!row) { pol.remove(); return; }
    pol.appendChild(el('span', 'k', row.label));
    pol.appendChild(el('span', 'v', row.rule));
    pol.appendChild(el('span', 'src', `[${row.source}]`));
  }).catch(() => pol.remove());
  const note = el('textarea', 'decnote');
  note.placeholder = 'The reason, for the record — it goes into the tag '
    + 'annotation and the event log.';
  body.appendChild(note);
  const acts = el('div', 'acts');
  const ok = el('button', 'btn primary', `Approve → ${a.target_state}`);
  const no = el('button', 'btn', 'Reject');
  const wd = el('button', 'btn', 'Withdraw');
  wd.title = 'Pull it back to DRAFT for more work (submitter or approver)';
  acts.append(ok, no, wd);
  body.appendChild(acts);
  const out = el('div', 'out');
  body.appendChild(out);
  card.appendChild(body);

  const busy = (on) => { ok.disabled = no.disabled = wd.disabled = on; };
  const run = async (fn) => {
    out.innerHTML = '';
    busy(true);
    try {
      const r = await fn();
      busy(false);
      return r;
    } catch (e) {
      busy(false);
      // ⭐ VERBATIM. The 409s from the write path are the teaching surface:
      // a merge conflict prints the rebase, the release gate names the child
      // and prints the command that bumps the pin.
      out.appendChild(refusalBox(e));
      out.scrollIntoView({ block: 'nearest' });
      return null;
    }
  };
  ok.onclick = async () => {
    const r = await run(() => post(`/api/approvals/${id}/decide`,
      decisionBody({ decision: 'approved', note: note.value.trim() })));
    if (r) {
      propStamped(r);
      out.appendChild(propApproved(number, r)); acts.remove(); note.remove();
    }
  };
  no.onclick = async () => {
    const r = await run(() => post(`/api/approvals/${id}/decide`,
      decisionBody({ decision: 'rejected', note: note.value.trim() })));
    if (r) {
      toast(`${number} v${r.version} rejected — back to ${r.state}`);
      renderProposal(number, String(id));
    }
  };
  wd.onclick = async () => {
    const r = await run(() => post(`/api/approvals/${id}/withdraw`,
      decisionBody({ note: note.value.trim() })));
    if (r) {
      toast(`${number} v${r.version} withdrawn — back to DRAFT`);
      navigate(tabPath(number, 'drafts'));
    }
  };
  return card;
}

/* THE DECISION BAR for a PROPOSAL (stage 13). A round open → Approve / Reject /
 * Withdraw, each a REVIEW bound to the head sha this page showed you (a push in
 * between and the server refuses it — you would be signing bytes you did not
 * see). No round → the Submit bar. Above both: every signoff so far, a
 * dismissed one struck through with the push that dismissed it, and the open
 * threads — "impossible to approve without seeing them".
 *
 * Built over ONE proposal today; a stage-14 change page will hold one bar over
 * N proposals and call the same review route per proposal, children first. */
function propDecideP(number, p) {
  const P = p.proposal, a = p.approval, rev = p.revision;
  const rnd = P.current_round;
  const card = el('div', 'doc decide');
  const h = el('h3', null, 'Decision');
  // STAGE 14: a proposal belongs to a CHANGE. When the change has other
  // members, it is decided THERE (the server refuses a member's own decision);
  // the bar says so and links it, rather than offering a button that 409s.
  if (P.change_id && window.CHANGES_UI) {
    const cl = el('div', 'thrwarn');
    cl.style.borderLeftColor = 'var(--blue)'; cl.style.background = 'var(--blue-bg)';
    const lk = el('a', null, `pull request ${PLM_ROUTES.ref(P.change_id)}`);
    lk.href = PLM_ROUTES.P.change(P.change_id);
    cl.append(document.createTextNode('Part of '), lk,
              document.createTextNode(' — one piece of work, one decision. '));
    api(`/api/changes/CHG-${P.change_id}`).then(cv => {
      const n = (cv.members || []).length;
      cl.appendChild(document.createTextNode(n > 1
        ? `It has ${n} proposals; decide them together on the pull request's page.`
        : 'A pull request of one: deciding here decides the pull request.'));
      if (n > 1) for (const b of card.querySelectorAll('.acts button')) {
        b.disabled = true; b.title = `Decide pull request ${PLM_ROUTES.ref(P.change_id)} instead`;
      }
    }).catch(() => {});
    card.appendChild(cl);
  }
  h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
    (ME && ME.verified && ME.actor) ? `as ${ME.actor}` : `as ${ACTOR}`));
  card.appendChild(h);
  const body = el('div', 'pad');
  card.appendChild(body);

  if ((P.reviews || []).length) {
    const so = el('div', 'signoffs');
    so.appendChild(el('div', 'lbl', 'Signoffs — each bound to the sha it looked at'));
    for (const r of P.reviews) {
      const row = el('div', 'sorow' + (r.dismissed_at ? ' dismissed' : ''));
      const mk = { approve: '✓', reject: '✕', comment: '💬' }[r.verdict] || '·';
      row.appendChild(el('span', 'mk v-' + r.verdict, mk));
      row.appendChild(el('b', null, r.reviewer || '—'));
      row.appendChild(el('span', null, ' ' + ({ approve: 'approved',
        reject: 'rejected', comment: 'commented' }[r.verdict] || r.verdict)));
      const chip = whoChip(r.kind);
      if (chip) row.appendChild(chip);
      if (!r.counts && r.verdict !== 'comment') {
        row.appendChild(el('span', 'rv-chip adv', 'advisory'));
      }
      row.appendChild(el('span', 'sha', r.head_sha ? '@ ' + r.head_sha.slice(0, 10)
                                                  : '@ (not recorded)'));
      if (r.dismissed_at) {
        const d = el('span', 'dism', `dismissed — the head moved to `
          + `${(r.dismissed_by_sha || '').slice(0, 10)}`);
        row.appendChild(d);
      }
      row.appendChild(when(r.created_at));
      if (r.body_md) row.appendChild(el('div', 'b', r.body_md));
      so.appendChild(row);
    }
    body.appendChild(so);
  }
  const tc = p.threads || {};
  if (tc.open) {
    const w = el('div', 'thrwarn' + (tc.blocking ? ' blocks' : ''));
    w.textContent = `${tc.open} open thread${tc.open === 1 ? '' : 's'}`
      + (tc.blocking ? ` — ${tc.blocking} BLOCKING: an approval at RELEASED is `
        + 'refused until it is resolved (an approver may resolve it with a reason '
        + 'to ship over it)' : ' — read them before you decide');
    body.appendChild(w);
  }

  if (['merged', 'approved', 'withdrawn'].includes(P.state)) {
    const done = el('div', 'note');
    done.style.whiteSpace = 'pre-wrap';
    done.textContent = P.state === 'withdrawn'
      ? 'Withdrawn — the draft branch was deleted. refs/plm/proposals/'
        + `${P.id}/head still holds every commit it proposed.`
      : `${P.state === 'merged' ? 'Merged' : 'Approved'}`
        + (P.merged_tag ? ` as ${P.merged_tag}` : '')
        + (a && a.approver ? ` by ${a.approver}` : '')
        + (a && a.decided_at ? ' · ' + fmtExact(a.decided_at) : '') + '.'
        + (a && a.decision_note ? '\n' + a.decision_note : '');
    body.appendChild(done);
    if (P.merged_tag) {
      const p2 = el('div'); p2.style.marginTop = '8px';
      const lk = el('a', 'btn tiny', `the release ${P.merged_tag} →`);
      lk.href = treePath(number, P.merged_tag);
      p2.appendChild(lk);
      body.appendChild(p2);
    }
    return card;
  }

  const out = el('div', 'out');
  if (!rnd) {
    // DRAFT (or rejected and not yet answered): the Submit bar.
    if (P.state === 'rejected') {
      body.appendChild(el('div', 'note',
        'Rejected. Push a fix to '
        + `${(P.head_ref || 'the draft branch').replace('refs/heads/', '')} — this `
        + 'same proposal re-opens, the rejection is dismissed by the push, and '
        + 'the threads say what is still open.'));
    }
    const bar = el('div', 'submitbar');
    bar.appendChild(el('span', 'lbl', 'Submit for review'));
    const sel = el('select', 'btn');
    for (const t of ['PROTOTYPE', 'RELEASED']) sel.appendChild(new Option(t, t));
    sel.value = P.target_maturity || 'PROTOTYPE';
    const note = el('input', 'subnote');
    note.placeholder = 'Note for the approver (optional)';
    const go = el('button', 'btn primary tiny', 'Submit');
    go.onclick = async () => {
      go.disabled = true; out.innerHTML = '';
      try {
        await post(`/api/proposals/${P.id}/submit`, decisionBody({
          target_state: sel.value, note: note.value.trim(),
          actor: actorName() }));
        toast(`${PLM_ROUTES.proposalLabel(P.id)} submitted`);
        renderProposal(number, 'p' + P.id);
      } catch (e) { go.disabled = false; out.appendChild(refusalBox(e)); }
    };
    bar.append(sel, note, go);
    body.appendChild(bar);
    body.appendChild(out);
    return card;
  }

  const why = el('div', 'note');
  why.textContent = (p.git_mode && p.git_mode !== 'write'
    ? `Approving moves v${rev.version} to ${rnd.target_state} in the registry `
      + `(git-first is ${p.git_mode} for this project). `
    : `Approving merges ${(P.head_sha || '').slice(0, 10)} — the head you are `
      + `looking at — into main, writes the tag v${rev.version}, and moves the `
      + `revision to ${rnd.target_state}. `)
    + 'Rejecting sends it back: the next push re-opens this same proposal.';
  body.appendChild(why);
  const pol = el('div', 'policyline');
  body.appendChild(pol);
  api('/api/policy?project=' + encodeURIComponent((p.item || {}).project || ''))
    .then(pp => {
      const row = (pp.policy || []).find(x => x.target_state === rnd.target_state);
      if (pp.policy_applies === false || !row) { pol.remove(); return; }
      pol.append(el('span', 'k', row.label), el('span', 'v', row.rule),
                 el('span', 'src', `[${row.source}]`));
    }).catch(() => pol.remove());
  const note = el('textarea', 'decnote');
  note.placeholder = 'The reason, for the record — it goes on the review, the '
    + 'tag annotation and the timeline.';
  body.appendChild(note);
  const acts = el('div', 'acts');
  const ok = el('button', 'btn primary', `Approve → ${rnd.target_state}`);
  const no = el('button', 'btn', 'Reject');
  const wd = el('button', 'btn', 'Withdraw');
  wd.title = 'Pull it back to DRAFT for more work (submitter or approver)';
  acts.append(ok, no, wd);
  body.appendChild(acts);
  body.appendChild(out);
  const busy = (on) => { ok.disabled = no.disabled = wd.disabled = on; };
  const run = async (fn) => {
    out.innerHTML = ''; busy(true);
    try { const r = await fn(); busy(false); return r; }
    catch (e) { busy(false); out.appendChild(refusalBox(e));
                out.scrollIntoView({ block: 'nearest' }); return null; }
  };
  const review = (verdict) => post(`/api/proposals/${P.id}/reviews`,
    decisionBody({ verdict, head_sha: P.head_sha, body: note.value.trim() }));
  ok.onclick = async () => {
    const r = await run(() => review('approve'));
    if (!r) return;
    if (r.advisory) { toast('recorded as an ADVISORY review — policy does not admit '
      + 'this signer'); renderProposal(number, 'p' + P.id); return; }
    propStamped(r);
    out.appendChild(propApproved(number, r)); acts.remove(); note.remove();
  };
  no.onclick = async () => {
    const r = await run(() => review('reject'));
    if (r) { toast(`${PLM_ROUTES.proposalLabel(P.id)} rejected — the next push re-opens it`);
             renderProposal(number, 'p' + P.id); }
  };
  wd.onclick = async () => {
    const r = await run(() => post(`/api/approvals/${rnd.id}/withdraw`,
      decisionBody({ note: note.value.trim() })));
    if (r) { toast(`${PLM_ROUTES.proposalLabel(P.id)} withdrawn to DRAFT`); renderProposal(number, 'p' + P.id); }
  };
  return card;
}

/* What actually happened, from the `git` block of the decide response. Not a
 * toast: "approved" is the least interesting half of it — which merge, which
 * tag, whether the branch is gone, is what tells you the record is now real. */
function propApproved(number, r) {
  const box = el('div', 'approved');
  const hd = el('div', 'hd');
  hd.append(el('span', 'x', '✓'),
            el('span', 'lbl', `Approved — v${r.version} is ${r.state}`));
  box.appendChild(hd);
  const g = r.git;
  if (!g) {
    box.appendChild(el('div', 'note',
      'This revision had no git origin, so nothing was merged — the registry '
      + 'moved it to ' + r.state + ' exactly as it always did.'));
    return box;
  }
  const HOW = { 'fast-forward': 'fast-forwarded `main` onto the pushed commit',
                merge: 'merged into `main` (a real three-way merge)',
                'first-release': 'created `main` at the pushed commit' };
  const tbl = el('table', 'led');
  const tb = el('tbody'); tbl.appendChild(tb);
  const row = (k, v) => {
    if (v == null || v === '') return;
    const tr = el('tr');
    tr.appendChild(el('td', 'dim', k));
    const td = el('td', 'ver');
    if (v instanceof Node) td.appendChild(v); else td.textContent = String(v);
    tr.appendChild(td);
    tb.appendChild(tr);
  };
  row('merged', HOW[g.merged] || g.merged);
  row('main', (g.main || '').slice(0, 10));
  if (g.tag) {
    const lk = el('a', null, g.tag);
    lk.href = treePath(number, g.tag);
    row('tag', lk);
  }
  row('the commit you approved', (g.pushed_commit || '').slice(0, 10)
    + ' — unmodified, and an ancestor of main');
  row('branch', g.branch_deleted
    ? `${g.branch_deleted.replace(/^refs\/heads\//, '')} deleted — it arrived`
    : '');
  if ((g.unreleased_children || []).length) {
    row('§5.1 warning', `${g.unreleased_children.length} child pin`
      + `${g.unreleased_children.length === 1 ? '' : 'ren'} not a release — `
      + 'recorded in the tag annotation');
  }
  box.appendChild(tbl);
  const acts = el('div', 'acts');
  if (g.tag) {
    const lk = el('a', 'btn tiny primary', `browse the release ${g.tag} →`);
    lk.href = treePath(number, g.tag);
    acts.appendChild(lk);
  }
  const it = el('a', 'btn tiny', `${number} →`);
  it.href = itemPath(number);
  acts.appendChild(it);
  box.appendChild(acts);
  return box;
}

function cmpHead(number, cmp, info) {
  const box = el('div'); box.id = 'cmphead';
  const all = info ? [...info.releases, ...info.drafts] : [];
  const side = (which, k) => {
    const d = el('div', 'side ' + which);
    // The page's two sides wear the §2.9 colours: old (blue) and new (amber).
    const S = PLM_DIFF.sides(cmpCtx(number, cmp));
    const sd = which === 'base' ? S.old : S.new;
    const lb = el('div', 'lbl');
    lb.append(PLM_DIFF.sideTag(sd, ''),
              document.createTextNode(which === 'base' ? ' — what it was' : ' — what it is now'));
    d.appendChild(lb);
    d.style.boxShadow = `inset 0 3px 0 var(${which === 'base' ? '--plm-old' : '--plm-new'})`;
    const pick = el('div', 'pick');
    if (all.length) {
      const sel = el('select', 'btn');
      const refs = all.map(r => r.ref);
      if (!refs.includes(k.ref)) sel.appendChild(new Option(k.ref, k.ref));
      for (const r of all) sel.appendChild(new Option(r.ref, r.ref));
      sel.value = k.ref;
      sel.onchange = () => {
        navigate(which === 'base'
          ? cmpPath(number, sel.value, cmp.head.ref)
          : cmpPath(number, cmp.base.ref, sel.value));
      };
      pick.appendChild(sel);
    } else pick.appendChild(el('span', 'num', k.ref));
    if (k.state) pick.appendChild(stamp(k.state));
    d.appendChild(pick);
    const meta = el('div', 'note');
    meta.style.cssText = 'font-family:var(--mono);font-size:11px;margin-top:5px';
    meta.textContent = `${k.commit.slice(0, 10)}`
      + (k.version ? `  ·  v${k.version}` : '');
    d.appendChild(meta);
    return d;
  };
  box.appendChild(side('base', cmp.base));
  box.appendChild(el('div', 'arrow', '→'));
  box.appendChild(side('head', cmp.head));

  const s = cmp.stats;
  const tally = el('div', 'tally');
  tally.appendChild(el('div', 'lbl', 'Changed'));
  tally.appendChild(el('div', 'n',
    `${s.files_changed} file${s.files_changed === 1 ? '' : 's'}`));
  const n = el('div', 'n');
  n.append(el('span', 'plus', `+${s.additions}`),
           document.createTextNode(' '),
           el('span', 'minus', `−${s.deletions}`));
  n.title = 'Line counts from git. They are meaningless for binary files — an '
    + 'ASCII STL counts every triangle as a line — which is why those rows '
    + 'show a size instead of a patch.';
  tally.appendChild(n);
  const swap = el('button', 'btn tiny', 'swap ⇄');
  swap.title = 'Compare the other way round';
  swap.style.marginTop = '4px';
  swap.onclick = () => { navigate(cmpPath(number, cmp.head.ref, cmp.base.ref)); };
  tally.appendChild(swap);
  box.appendChild(tally);
  // the one legend, the same line the overlay carries
  const lg = el('div', 'cmplegend');
  lg.appendChild(PLM_DIFF.legend(cmpCtx(number, cmp), {}));
  box.appendChild(lg);
  return box;
}

function cmpCommits(cmp) {
  const card = el('div', 'doc');
  const h = el('h3', null, `Commits in this range (${cmp.commits.length})`);
  card.appendChild(h);
  for (const c of cmp.commits) {
    const row = el('div', 'cmtrow');
    row.appendChild(el('span', 'sha', c.short));
    row.appendChild(el('span', 'sub', c.subject));
    const tr = el('div', 'tr');
    if (c.author) tr.appendChild(el('span', null, c.author));
    tr.appendChild(when(isoAt(c.date)));
    row.appendChild(tr);
    card.appendChild(row);
  }
  return card;
}

/* ------------------------------------------- 1. what changed in the design
 * Redlines. Old struck through in obsolete red, new set in released green —
 * the markup a checker makes on a print, not a code forge's gutter. */
function redline(from, to) {
  const v = el('span');
  if (from) v.appendChild(el('span', 'rl-del', from));
  if (from && to) v.appendChild(el('span', 'rl-to', '→'));
  if (to) v.appendChild(el('span', 'rl-add', to));
  if (!from && !to) v.appendChild(el('span', 'dim', '—'));
  return v;
}
const attrText = (a) => a
  ? `${a.value}${a.unit ? ' ' + a.unit : ''}` : '';

function rlRow(key, valueNode, source, tomlKey = '', now = '') {
  const r = el('div', 'rlrow');
  // The plm.toml key this row is a reading of (stage 13): what a review thread
  // anchors to, and — when it carries a value — what a suggestion replaces.
  if (tomlKey) { r.dataset.key = tomlKey; if (now !== '') r.dataset.now = now; }
  r.appendChild(el('span', 'k', key));
  const v = el('span', 'val');
  v.appendChild(valueNode);
  if (source) v.appendChild(el('div', 'src', source));
  r.appendChild(v);
  return r;
}
function rlGroup(title) {
  const g = el('div', 'rlgrp');
  g.appendChild(el('div', 't', title));
  return g;
}

/* The `manifestdelta` widget's body: `plm.toml` diffed as what it MEANS.
 *
 * ⭐ THE LIFECYCLE FIELDS ARE NOT THE CHANGE. Seen on proposal #24: the redlines
 * led with `state PROTOTYPE → DRAFT`, `maturity prototype → (gone)`, `author
 * Alex → alex`, `date …` — four rows that change on EVERY proposal by
 * construction, burying the one pin bump the reviewer was actually being asked
 * about. `version` and `summary` are the HEADER (they say which revision and
 * why); the design facts are the redlines; `state / maturity / date / author`
 * fold into one muted line, collapsed. A case-only change is not a change. */
const LIFECYCLE_FIELDS = new Set(['state', 'maturity', 'date', 'author']);
const HEADER_FIELDS = new Set(['version', 'summary']);

function cmpDesignBody(card, number, cmp) {
  const d = cmp.manifest_delta || {};
  let any = false;
  // `Alex` -> `alex` is the same author. A redline that says otherwise is
  // noise with a red strike through it.
  const moved = (f) => String(f.from || '').trim().toLowerCase()
                    !== String(f.to || '').trim().toLowerCase();

  // ---- the header: which revision, and why. Not a redline — a statement.
  const all = (d.fields || []).filter(moved);
  const head = el('div', 'rlhead');
  head.append(el('span', 'v', `${cmp.base.version ? 'v' + cmp.base.version : cmp.base.ref}`),
              el('span', 'rl-to', '→'),
              el('span', 'v now', `${cmp.head.version ? 'v' + cmp.head.version : cmp.head.ref}`));
  if (cmp.head.state) head.appendChild(stamp(cmp.head.state));
  card.appendChild(head);

  // ---- fields. `summary` is prose and redlines as two paragraphs.
  const fields = all.filter(f => !HEADER_FIELDS.has(f.name)
                              && !LIFECYCLE_FIELDS.has(f.name));
  const prose = fields.filter(f => f.name === 'summary' || f.name === 'verification');
  const plain = fields.filter(f => !prose.includes(f));
  if (plain.length) {
    any = true;
    const g = rlGroup('Revision');
    for (const f of plain) g.appendChild(rlRow(f.name, redline(f.from, f.to), '',
                                                f.name, f.to || ''));
    card.appendChild(g);
  }
  for (const f of all.filter(f => f.name === 'summary')) {
    const s2 = el('div', 'rlsum');
    if (f.from) s2.appendChild(el('span', 'was', f.from));
    s2.appendChild(el('span', 'now', f.to || ''));
    card.appendChild(s2);
  }
  for (const f of prose.filter(f => f.name !== 'summary')) {
    any = true;
    const g = rlGroup('Verification');
    const body = el('div', 'rlprose');
    body.style.padding = '2px 12px 10px';
    if (f.from) body.appendChild(el('span', 'was', f.from));
    if (f.to) body.appendChild(el('span', 'now', f.to));
    g.appendChild(body);
    card.appendChild(g);
  }

  // ---- attributes
  const a = d.attributes || {};
  if ((a.added || []).length || (a.removed || []).length || (a.changed || []).length) {
    any = true;
    const g = rlGroup('Attributes');
    for (const x of a.added || []) {
      const r = rlRow(x.name, redline('', attrText(x.to)), (x.to || {}).source,
                      `attributes.${x.name}`, (x.to || {}).value ?? '');
      r.querySelector('.k').prepend(el('span', 'rl-mark add', '+'));
      g.appendChild(r);
    }
    for (const x of a.removed || []) {
      const r = rlRow(x.name, redline(attrText(x.from), ''), (x.from || {}).source);
      r.querySelector('.k').prepend(el('span', 'rl-mark del', '−'));
      g.appendChild(r);
    }
    for (const x of a.changed || []) {
      const same = attrText(x.from) === attrText(x.to);
      const g2 = same
        // Only the provenance moved. Say so rather than printing "50 → 50".
        ? (() => { const s = el('span', 'dim',
            `${attrText(x.to)} — unchanged, re-sourced`); return s; })()
        : redline(attrText(x.from), attrText(x.to));
      const src = [(x.from || {}).source, (x.to || {}).source];
      g.appendChild(rlRow(x.name, g2,
        src[0] !== src[1] && src[1] ? `source: ${src[1]}` : src[1] || '',
        `attributes.${x.name}`, (x.to || {}).value ?? ''));
    }
    card.appendChild(g);
  }

  // ---- BOM
  const b = d.bom || {};
  const bomAny = (b.added || []).length || (b.removed || []).length
    || (b.qty_changed || []).length || (b.changed || []).length;
  if (bomAny) {
    any = true;
    const g = rlGroup('Bill of materials');
    const bomLine = (l) => `${l.path}   ${l.qty} ${l.unit || ''}`.trim()
      + (l.find != null ? `   find ${l.find}` : '');
    for (const l of b.added || []) {
      const r = rlRow('added', redline('', bomLine(l)), l.notes,
                      `bom[${l.path}].qty`, l.qty ?? '');
      r.querySelector('.k').prepend(el('span', 'rl-mark add', '+'));
      g.appendChild(r);
    }
    for (const l of b.removed || []) {
      const r = rlRow('removed', redline(bomLine(l), ''), l.notes);
      r.querySelector('.k').prepend(el('span', 'rl-mark del', '−'));
      g.appendChild(r);
    }
    for (const q of b.qty_changed || []) {
      g.appendChild(rlRow(q.path,
        redline(`${q.from} ${q.unit || ''}`.trim(), `${q.to} ${q.unit || ''}`.trim()),
        '', `bom[${q.path}].qty`, q.to ?? ''));
    }
    for (const c of b.changed || []) {
      const from = c.from || {}, to = c.to || {};
      const v = el('span');
      if ((from.notes || '') !== (to.notes || '')) {
        const p = el('div', 'rlprose');
        if (from.notes) p.appendChild(el('span', 'was', from.notes));
        if (to.notes) p.appendChild(el('span', 'now', to.notes));
        v.appendChild(p);
      } else v.appendChild(el('span', 'dim', 'line details changed'));
      g.appendChild(rlRow(c.path, v));
    }
    if (b.unchanged) g.appendChild(el('div', 'rlnote',
      `${b.unchanged} other line${b.unchanged === 1 ? '' : 's'} unchanged.`));
    card.appendChild(g);
  }

  // ---- submodule pins are the `pinlog` widget's, not this one's. They used
  // to be a group inside here AND a page section; now the `submodule` kind
  // registers `pinlog` and the page mounts it once, right below this card.
  if ((cmp.submodules || []).length) any = true;

  // ---- files and roles
  const f = d.files || {};
  const fileAny = (f.added || []).length || (f.removed || []).length
    || (f.role_changed || []).length || f.primary_changed
    || (f.external_added || []).length || (f.external_removed || []).length;
  if (fileAny) {
    any = true;
    const g = rlGroup('Files and roles');
    if (f.primary_changed) {
      const r = rlRow('primary',
        redline(f.primary_changed.from, f.primary_changed.to),
        'The geometry of record — this is what the 3D overlay below is showing.',
        'files.primary', f.primary_changed.to || '');
      g.appendChild(r);
    }
    for (const x of f.added || []) {
      const r = rlRow(x.role || 'file', redline('', x.path));
      r.querySelector('.k').prepend(el('span', 'rl-mark add', '+'));
      g.appendChild(r);
    }
    for (const x of f.removed || []) {
      const r = rlRow(x.role || 'file', redline(x.path, ''));
      r.querySelector('.k').prepend(el('span', 'rl-mark del', '−'));
      g.appendChild(r);
    }
    for (const x of f.role_changed || []) {
      g.appendChild(rlRow(x.path, redline(x.from, x.to), 'role'));
    }
    for (const x of f.external_added || []) {
      const r = rlRow(x.role || 'file', redline('', x.name),
        `external — ${x.backend || 'vault'}, ${fmtSize(x.size || 0)}`);
      r.querySelector('.k').prepend(el('span', 'rl-mark add', '+'));
      g.appendChild(r);
    }
    for (const x of f.external_removed || []) {
      const r = rlRow(x.role || 'file', redline(x.name, ''), 'external');
      r.querySelector('.k').prepend(el('span', 'rl-mark del', '−'));
      g.appendChild(r);
    }
    card.appendChild(g);
  }

  // ---- relations, routing, tags
  for (const [key, title, fmt] of [
    ['relations', 'Relations', (r) => `${r.type}  ${r.item}`
      + (r.version ? ` v${r.version}` : '')],
    ['routing', 'Routing', (r) => `${r.seq}  ${r.op}`
      + (r.work_center ? `  ·  ${r.work_center}` : '')],
    ['tags', 'Tags', (r) => String(r)],
  ]) {
    const x = d[key] || {};
    if (!(x.added || []).length && !(x.removed || []).length
        && !(x.changed || []).length) continue;
    any = true;
    const g = rlGroup(title);
    for (const r of x.added || []) {
      const row = rlRow('added', redline('', fmt(r)));
      row.querySelector('.k').prepend(el('span', 'rl-mark add', '+'));
      g.appendChild(row);
    }
    for (const r of x.removed || []) {
      const row = rlRow('removed', redline(fmt(r), ''));
      row.querySelector('.k').prepend(el('span', 'rl-mark del', '−'));
      g.appendChild(row);
    }
    for (const r of x.changed || []) {
      g.appendChild(rlRow(fmt(r.to || r.from || r), redline(
        r.from ? fmt(r.from) : '', r.to ? fmt(r.to) : '')));
    }
    card.appendChild(g);
  }

  if (!any) card.appendChild(el('div', 'rlnote',
    'Nothing in the DESIGN changed between these two refs — no attribute, BOM '
    + 'line, file role, relation, routing operation or tag moved. Whatever '
    + 'changed is in the files below.'));

  // ---- lifecycle: the fields that move on every proposal by construction.
  // Present, checkable, and out of the way.
  const life = all.filter(f => LIFECYCLE_FIELDS.has(f.name));
  if (life.length) {
    const det = el('details', 'lifecycle');
    const sum = el('summary', null,
      `lifecycle — ${life.map(f => f.name).join(', ')}`);
    sum.title = 'State, maturity, author and date change on every proposal. '
      + 'They are bookkeeping, not the design change being reviewed.';
    det.appendChild(sum);
    for (const f of life) det.appendChild(rlRow(f.name, redline(f.from, f.to)));
    card.appendChild(det);
  }
  // STAGE 13 HOOK (web/review.js): each keyed row takes a thread (💬), and an
  // attribute / BOM quantity row takes a SUGGESTED value.
  if (window.REVIEW_UI && REVIEW_UI.ctx) REVIEW_UI.mountRedline(card);
  return card;
}

/* `KML-ASM-4000  v0.6.4 → v0.8.6`, with the CHILD'S OWN commit subjects
 * underneath. This is `git diff --submodule=log` without a clone: the reason
 * a parent's BOM moved, in the child's own words. */
function pinChange(s) {
  const row = el('div', 'pinrow');
  const hd = el('div', 'hd');
  const who = el('span', 'who');
  if (s.number && s.number !== 'catalog'
      && ((s.to && s.to.known) || (s.from && s.from.known))) {
    const a = el('a', null, s.number);
    const at = s.to || s.from;
    a.href = treePath(s.number, at.ref || at.commit);
    who.appendChild(a);
  } else who.textContent = s.number || s.path;
  hd.appendChild(who);
  hd.appendChild(pinChip(s.from));
  hd.appendChild(el('span', 'rl-to', '→'));
  hd.appendChild(pinChip(s.to));
  if (s.direction === 'backward') {
    const b = el('span', 'pinwarn', '↩ rolled back');
    b.title = 'The pin moved to an OLDER commit. The subjects below are what '
      + 'this parent gave up.';
    hd.appendChild(b);
  }
  if (!s.from) hd.appendChild(el('span', 'rl-mark add', '+'));
  if (!s.to) hd.appendChild(el('span', 'rl-mark del', '−'));
  row.appendChild(hd);

  if ((s.commits || []).length) {
    const kids = el('div', 'kids');
    for (const c of s.commits) {
      const k = el('div', 'k');
      k.append(el('span', 'sha', c.short), el('span', 's', c.subject));
      k.title = fmtExact(isoAt(c.date));
      kids.appendChild(k);
    }
    row.appendChild(kids);
  } else {
    row.appendChild(el('div', 'why', s.note
      || 'No commits between the two pins.'));
  }
  // The gitlink's path is `parts/<number>` in every ordinary case — a fact the
  // row already carries. It is only worth a line of its own when it is not.
  if (s.path) {
    who.title = s.path;
    if (s.path !== `parts/${s.number}` && s.path !== s.number) {
      row.appendChild(el('div', 'why', s.path));
    }
  }
  return row;
}

/* ══ 2. GEOMETRY, and every other file, shown rather than counted ══════════
 *
 * Old (blue, ghosted) and new (amber) in ONE scene; where they differ, the
 * volume that went away reads red and the volume that arrived reads green
 * (vCompareOn — a screen-space depth comparison of the two bodies). The
 * dark room is DOCKED into a card rather than covering the window, so whatever
 * you are reading stays on screen — same renderer, camera, controls and fit as
 * the part viewer.
 *
 * Geoff: "which part is chosen to be the visual diff here … ideally I might
 * want to look at the diff between any file, but for a part maybe you look at
 * the text difference, or the graphical difference, whether it's the main part
 * or not." So the overlay is no longer a fixed answer — it is a chooser, and
 * every mesh row in the file list can open its own.
 */
// The colours are NOT here: they are the four §2.9 tokens on index.html's
// :root (--plm-old / --plm-new for the two revisions, --plm-removed /
// --plm-added for what changed), read through PLM_DIFF.tok.

/* ⭐ THE FOUR-COLOUR READING (PLM_REVIEW_MODEL §2.9 "Sides and colours").
 *
 * The two bodies keep their SIDE colours — new (amber) solid, old (blue)
 * ghosted over it — and wherever one surface stands proud of the other the
 * CHANGE colours take over: old surface in front of the new one is volume that
 * was REMOVED (red); new surface in front of the old one is volume that was
 * ADDED (green). Coincident surfaces did not move and keep their side colours.
 *
 * How: each frame, each body's depth alone goes into its own depth texture
 * (layers 1 and 2, one cheap override-material pass each); each body's
 * fragment shader compares its own view depth with the OTHER body's at the
 * same pixel. A body with nothing of the other behind it at all is wholly
 * removed/added. The tolerance (0.12 mm + 0.15 % of the distance) swallows
 * re-tessellation of an unchanged curved face.
 *
 * FOUR TOGGLES, NOT THREE BUTTONS (Geoff, 2026-10-27): old, new, removed and
 * added each switch on their own. `uMark` per body says whether its proud
 * volume takes the change colour (removed on the old body, added on the new);
 * the depth passes always see BOTH bodies, even one that is hidden, so "old
 * with removed" still reads red where the new body no longer is. Hiding a
 * body without a change colour on it is the plain side-colour view. */
function vCompareOn() {
  const base = V.parts.find(p => p.side === 'base'), head = V.parts.find(p => p.side === 'head');
  if (!base || !head) { V.cmp = null; return; }
  const U = {
    uRes: { value: new THREE.Vector2(1, 1) }, uNear: { value: 1 }, uFar: { value: 8000 },
    uOn: { value: 1 }, uDepthBase: { value: null }, uDepthHead: { value: null },
    uMarkBase: { value: 1 }, uMarkHead: { value: 1 }, uGhostBase: { value: 0.35 },
  };
  V.cmp = { u: U, rt: {}, size: [0, 0], base, head,
            depthMat: new THREE.MeshBasicMaterial({ side: THREE.DoubleSide, colorWrite: false }) };
  base.mesh.layers.enable(1); head.mesh.layers.enable(2);
  const patch = (part, other, chgTok, mark, ghost) => {
    const mt = part.mesh.material;
    const chg = { value: new THREE.Color(PLM_DIFF.tok(chgTok)) };
    mt.onBeforeCompile = (sh) => {
      Object.assign(sh.uniforms, { uRes: U.uRes, uNear: U.uNear, uFar: U.uFar, uOn: U.uOn,
        uOther: other === 'head' ? U.uDepthHead : U.uDepthBase, uChg: chg,
        uMark: mark, uGhost: ghost });
      sh.fragmentShader = sh.fragmentShader
        .replace('#include <common>', '#include <common>\n'
          + 'uniform sampler2D uOther; uniform vec2 uRes; uniform float uNear;\n'
          + 'uniform float uFar; uniform float uOn; uniform vec3 uChg;\n'
          + 'uniform float uMark; uniform float uGhost;')
        .replace('#include <color_fragment>', `#include <color_fragment>
  if (uOn > 0.5) {
    float od = texture2D(uOther, gl_FragCoord.xy / uRes).x;
    float mz = perspectiveDepthToViewZ(gl_FragCoord.z, uNear, uFar);
    float oz = perspectiveDepthToViewZ(od, uNear, uFar);
    float eps = 0.12 + 0.0015 * abs(mz);
    if (uMark > 0.5 && (od >= 0.999999 || mz - oz > eps)) {
      diffuseColor.rgb = uChg; diffuseColor.a = max(diffuseColor.a, 0.88);
    } else diffuseColor.a *= uGhost;
  }`);
    };
    mt.customProgramCacheKey = () => 'plm-cmp-' + chgTok;
    mt.needsUpdate = true;
  };
  patch(base, 'head', '--plm-removed', U.uMarkBase, U.uGhostBase);
  patch(head, 'base', '--plm-added', U.uMarkHead, { value: 1.0 });
}
function vCompareDepth() {
  const C = V.cmp, r = V.renderer;
  if (!C.u.uOn.value) return;
  const sz = r.getDrawingBufferSize(C.u.uRes.value);
  if (C.size[0] !== sz.x || C.size[1] !== sz.y) {
    for (const k of ['base', 'head']) {
      if (C.rt[k]) C.rt[k].dispose();
      const rt = new THREE.WebGLRenderTarget(sz.x, sz.y);
      rt.depthTexture = new THREE.DepthTexture(sz.x, sz.y);
      rt.depthTexture.type = THREE.UnsignedIntType;
      C.rt[k] = rt;
    }
    C.u.uDepthBase.value = C.rt.base.depthTexture;
    C.u.uDepthHead.value = C.rt.head.depthTexture;
    C.size = [sz.x, sz.y];
  }
  C.u.uNear.value = V.camera.near; C.u.uFar.value = V.camera.far;
  // A hidden body still has to leave its depth, or "old with removed" could
  // not say where the new body no longer is.
  const wasVis = [C.base.mesh.visible, C.head.mesh.visible];
  C.base.mesh.visible = true; C.head.mesh.visible = true;
  const bg = V.scene.background;
  V.scene.background = null; V.scene.overrideMaterial = C.depthMat;
  for (const [k, layer] of [['base', 1], ['head', 2]]) {
    V.camera.layers.set(layer);
    r.setRenderTarget(C.rt[k]); r.clear(); r.render(V.scene, V.camera);
  }
  V.camera.layers.set(0);
  C.base.mesh.visible = wasVis[0]; C.head.mesh.visible = wasVis[1];
  r.setRenderTarget(null); V.scene.overrideMaterial = null; V.scene.background = bg;
}

/* THERE IS ONE WEBGL CONTEXT AND IT MOVES. A dozen docked viewers would be a
 * dozen contexts, which browsers cap and then start dropping. Whoever opens a
 * 3-D diff takes the room; whoever had it gets a button to take it back. */
let OVL = null;
function ovlTake(owner) {
  if (OVL && OVL !== owner) OVL.release();
  OVL = owner;
}

function meshWhy(side, m) {
  if (!m || m.source === 'none') return `${side}: this revision names no primary mesh.`;
  if (m.source === 'missing') return `${side}: the manifest names ${m.path}, but `
    + 'it is in neither the tree nor [[files.external]].';
  return '';
}

/* One overlay host: the control bar, the mount the dark room docks into, and
 * the bookkeeping that keeps exactly one of them live.
 *   sides — [{side:'base'|'head', color, path, url}], one or two
 *   title — what the HUD says
 */
function overlayHost(card, title, sides, opts = {}) {
  const host = { open, release, mount: el('div') };
  host.mount.className = 'ovmount';
  const bar = el('div', 'ovbar');
  // FOUR TOGGLES, each on its own: the two bodies and the two change colours.
  // (Old / Both / New used to be one choice, and ticking a body off in the
  // list below left the other body reading wholly added — Geoff, 2026-10-27.)
  const seg = el('div', 'seg toggles');
  const tog = { base: true, head: true, removed: true, added: true };
  const togBtn = {};
  const apply = () => {
    for (const k of Object.keys(togBtn)) togBtn[k].classList.toggle('on', tog[k]);
    for (const p of V.parts) {
      p.mesh.visible = !!tog[p.side];
      if (p.box) p.box.checked = p.mesh.visible;
    }
    if (V.cmp) {
      V.cmp.u.uMarkBase.value = tog.removed ? 1 : 0;
      V.cmp.u.uMarkHead.value = tog.added ? 1 : 0;
      // the old body is a ghost only when it is drawn over the new one
      V.cmp.u.uGhostBase.value = tog.base && tog.head ? 0.35 : 1.0;
    }
    setOp();
  };
  for (const [k, label, tok, tip] of [
      ['base', 'old', '--plm-old', 'the old revision\'s body'],
      ['head', 'new', '--plm-new', 'the new revision\'s body'],
      ['removed', 'removed', '--plm-removed', 'volume the old body has where the new one does not'],
      ['added', 'added', '--plm-added', 'volume the new body has where the old one does not']]) {
    const b = el('button', 'on');
    const sw = el('span', 'sw'); sw.style.background = PLM_DIFF.tok(tok);
    b.append(sw, document.createTextNode(label));
    b.title = tip + ' — click to show or hide';
    b.setAttribute('aria-pressed', 'true');
    b.onclick = () => { tog[k] = !tog[k]; b.setAttribute('aria-pressed', String(tog[k])); apply(); };
    togBtn[k] = b;
    seg.appendChild(b);
  }
  if (sides.length === 2) bar.appendChild(seg);
  // THE LEGEND (PLM_DIFF.legend): ■ old v… ■ new v… ■ removed ■ added ⓘ
  if (opts.legend) bar.appendChild(opts.legend());
  const op = el('span', 'op');
  const slider = el('input');
  slider.type = 'range'; slider.min = 20; slider.max = 100; slider.value = 60;
  /* ⭐ THE THREE-WAY READING DEPENDS ON depthWrite STAYING ON.
   *
   * Two translucent shells with depthWrite off just render whichever mesh has
   * more surface area: a part that grew read solid green, and forcing base on
   * top read solid red. With depthWrite ON, head draws first and claims its
   * depth; base draws at renderOrder 2 and the depth test lets it through only
   * where it is in front of, or level with, the head. So base-only reads red,
   * head-only green, and the overlap blends — the sentence above the viewer. */
  const setOp = () => {
    const v = Number(slider.value) / 100;
    // with both bodies up, the old one is a GHOST drawn after the new one:
    // translucent, and it writes no depth of its own (vCompareOn)
    // the reading is live whenever there are two bodies; which of them, and
    // which change colours, are the toggles' business
    const both = V.parts.length === 2 && tog.base && tog.head;
    if (V.cmp) V.cmp.u.uOn.value = V.parts.length === 2 ? 1 : 0;
    for (const p of V.parts) {
      const mt = p.mesh.material;
      const ghost = both && p.side === 'base';
      mt.transparent = v < 1 || ghost; mt.opacity = v; mt.depthWrite = !ghost;
      mt.needsUpdate = true;
      p.mesh.renderOrder = p.side === 'base' ? 2 : 1;
    }
  };
  slider.oninput = setOp;
  op.append(el('span', null, 'opacity'), slider);
  bar.appendChild(op);
  card.append(bar, host.mount);
  // STAGE 13 HOOK (web/review.js): on a proposal page the geometry card's
  // overlay takes notes ON THE PART — a point in model space + the camera.
  if (opts.review && window.REVIEW_UI && REVIEW_UI.ctx) {
    REVIEW_UI.meshBar(bar, host, sides, host.mount);
  }

  async function open() {
    ovlTake(host);
    host.mount.innerHTML = '';
    closeViewer();                 // undock from wherever the room is now
    vDock(host.mount);
    vBegin(title);
    const top = el('div');
    top.appendChild(el('h3', null, 'Overlay'));
    if (opts.legend) {
      const r = el('div', 'row');
      r.appendChild(opts.legend());
      top.appendChild(r);
    }
    bar.style.display = '';
    // `noXray`: the x-ray checkbox sets depthWrite=false, which is the one
    // thing the removed/added reading cannot survive. In overlay mode the
    // opacity slider above IS the x-ray, so the checkbox steps out.
    await vBuild(title, sides.map(s => ({ ...s, bodyLabel: s.label, bodyTitle: s.title })),
                 top, { noXray: true });
    if (V.parts.length) {
      vCompareOn();
      // the body list's own boxes are the same two switches
      for (const p of V.parts) if (p.box) p.box.onchange = () => { tog[p.side] = p.box.checked; apply(); };
      apply();
    }
    if (opts.review && window.REVIEW_UI && REVIEW_UI.ctx) REVIEW_UI.drawPins();
  }

  function release() {
    if (OVL === host) OVL = null;
    closeViewer();
    bar.style.display = 'none';
    host.mount.innerHTML = '';
    const again = el('button', 'btn', 'Show the 3-D diff');
    again.title = 'There is one 3-D room and another row is using it';
    again.onclick = open;
    const p = el('div', 'pad');
    p.appendChild(again);
    host.mount.appendChild(p);
  }
  if (opts.autoOpen) queueMicrotask(() => {
    if (document.body.contains(host.mount)) open();
  });
  else release();
  return host;
}

/* The GEOMETRY section: the chooser, then the overlay. */
function cmpGeometry(number, cmp, want, autoOpen = true) {
  const card = el('div', 'doc');
  const h = el('h3', null, 'Geometry — old over new');
  card.appendChild(h);
  const list = (cmp.meshes || []).filter(m => (m.base && m.base.url)
                                           || (m.head && m.head.url));
  if (!list.length) {
    const m = cmp.mesh || {};
    const why = [meshWhy('Base', m.base), meshWhy('Head', m.head)].filter(Boolean);
    card.appendChild(el('div', 'empty', why.join('  ')
      || 'Neither revision has a mesh, so there is nothing to overlay.'));
    return card;
  }
  let idx = Math.max(0, list.findIndex(m =>
    want && ((m.head && m.head.path === want) || (m.base && m.base.path === want))));

  const chooser = el('div', 'ovpick');
  const sel = el('select', 'btn');
  for (let i = 0; i < list.length; i++) {
    const m = list[i];
    const nm = Path_basename((m.head || m.base).path);
    const tag = [m.is_primary ? 'primary' : '', m.role].filter(Boolean).join(' · ');
    sel.appendChild(new Option(`${tag ? tag + ' — ' : ''}${nm}`, String(i)));
  }
  sel.value = String(idx);
  chooser.append(el('span', 'lbl', 'Showing'), sel);
  const why = el('span', 'why');
  chooser.appendChild(why);
  card.appendChild(chooser);

  const read = el('div', 'ovread');
  card.appendChild(read);
  const holder = el('div');
  card.appendChild(holder);

  const draw = () => {
    const m = list[idx];
    why.textContent = m.reason;
    // the same sides, labels and colours as a mesh row's Overlay form
    const f = { basePath: (m.base || {}).path || '', headPath: (m.head || {}).path || '',
                baseUrl: (m.base || {}).url || null, headUrl: (m.head || {}).url || null };
    const ctx = cmpCtx(number, cmp);
    const sides = PLM_DIFF.overlaySides(ctx, f, number);
    read.innerHTML = '';
    if (sides.length === 2) {
      read.append(document.createTextNode(PLM_DIFF.overlayReading(sides) + ' Showing '),
                  el('b', null, Path_basename((m.head || m.base).path)),
                  document.createTextNode(m.is_primary ? ' (the primary).' : '.'));
    } else {
      read.textContent = `Only the ${sides[0].which} side has this mesh, so there `
        + 'is nothing to overlay it against — it is shown on its own.';
    }
    holder.innerHTML = '';
    overlayHost(holder, `${number}  ${cmp.base.ref} → ${cmp.head.ref}  ·  `
      + Path_basename((m.head || m.base).path), sides,
      { autoOpen, review: true, legend: () => PLM_DIFF.legend(ctx, f) });
    autoOpen = true;      // only the FIRST draw defers to a `file=` link
  };
  sel.onchange = () => {
    idx = Number(sel.value);
    const m = list[idx];
    const p = (m.head || m.base).path;
    history.replaceState(null, '', cmpPathQ(number, cmp.base.ref, cmp.head.ref,
      { mesh: p, file: cmpQ().file }));
    draw();
  };
  draw();
  return card;
}

const Path_basename = (p) => String(p || '').split('/').pop();

/* The dark room, docked into a card. The canvas element keeps its WebGL
 * context across the move, and #vcanvas is watched by a ResizeObserver, so the
 * renderer re-sizes itself into whatever box it lands in. */
function vDock(mount) {
  const v = $('viewer');
  v.classList.add('docked');
  mount.appendChild(v);
}
function vUndock() {
  const v = $('viewer');
  if (!v || !v.classList.contains('docked')) return;
  v.classList.remove('docked');
  document.body.appendChild(v);
}

/* ══ 3. the file list — one row per file, shown in the FORMS its type registers ══
 * The rows, their forms (Overlay / Stats / Pointer for a mesh; Side by side /
 * Swipe / Onion skin / Difference for an image; Unified / Split for text;
 * Fields / Design / Text for plm.toml; Pin for a BOM child) and the default
 * per row all live in web/diffwidgets.js — the registry the change page's
 * Files changed tab draws through too (PLM_REVIEW_MODEL §2.9). This is only
 * the card around them. */
function cmpFiles(cmp, number, q, allRows) {
  const card = el('div', 'doc');
  const rows = (allRows || PLM_DIFF.rows(cmp))
    // The kinds the page FEATURES at the top are not repeated here.
    .filter(r => !FEATURED_KINDS[r.kind]);
  card.appendChild(el('h3', null, `File diffs (${rows.length})`));
  if (cmp.stats.truncated) card.appendChild(el('div', 'dfnote',
    `${cmp.stats.files_changed} files changed; the first `
    + `${cmp.stats.files_reported} carry a patch.`));
  const featured = (allRows || PLM_DIFF.rows(cmp)).filter(r => FEATURED_KINDS[r.kind]).length;
  if (featured) card.appendChild(el('div', 'dfnote',
    `${featured} row${featured === 1 ? '' : 's'} — the manifest and the BOM `
    + 'pins — are shown above, where what they MEAN is readable.'));
  for (const a of cmp.pairs_ambiguous || []) {
    const n = el('div', 'dfnote');
    n.innerHTML = `<b>${escapeHtml(a.key)}</b> could not be paired: `
      + `${a.base_paths.length} candidate(s) at base, ${a.head_paths.length} at `
      + 'head. Left as a separate add and delete rather than guessed.';
    n.title = a.base_paths.concat(a.head_paths).join('\n');
    card.appendChild(n);
  }
  if (!rows.length) {
    card.appendChild(el('div', 'empty', 'No file changed between these refs.'));
    return card;
  }
  const ctx = cmpCtx(number, cmp);
  for (const r of rows) card.appendChild(PLM_DIFF.row(r, ctx, { q }));
  return card;
}

/* What every form is handed on a compare surface. */
function cmpCtx(number, cmp, extra = {}) {
  return { number, cmp,
           review: true, ...extra };
}

/* ------------------------------------------------------------- router
 * STAGE 22 — REAL PATHS. Geoff: "please move to real paths". The address bar
 * reads like GitHub's (`/CID-ASM-0001/compare/v0.1.0...v0.2.0`, `/changes/
 * CHG-380`); web/routes.js holds the whole table and this is the History-API
 * router over it:
 *
 *   navigate(path)   the ONE way the app moves: pushState + route(). Every
 *                    `<a href="/…">` to a UI path is a real link (middle-click,
 *                    copy-link) and a plain left-click on it comes here instead
 *                    of reloading (inAppClick, one document listener).
 *   popstate         back / forward re-route.
 *   the hash era     an old `/#/item/N/…` link is mapped FIRST (routes.js
 *                    fromHash) and the history entry REPLACED with its path, so
 *                    every link in old chats, tickets and email still lands.
 *
 * The server serves the shell for every UI path (plm/api.py _ShellStatic) and
 * a small 404 for anything else, so reload and deep links are just route(). */
let INSTANCE_LABEL = '';
/* The tab title once a page knows its subject: `CHG-380 · Inky Frame: USB-C… ·
 * plm` — the subject first, then the site, GitHub's order. */
function pageTitle(...parts) {
  document.title = [...parts.filter(Boolean),
    INSTANCE_LABEL ? `plm [${INSTANCE_LABEL}]` : 'plm'].join(' · ');
}

function navigate(path, { replace = false } = {}) {
  if (!path) return;
  const here = location.pathname + location.search + location.hash;
  // Already there — unless the path carries an in-page anchor (`/pulls/380#merge`):
  // then the page renders again and scrolls to it, however many times it is asked.
  if (path === here && !replace && !path.includes('#')) return;
  history[replace ? 'replaceState' : 'pushState'](null, '', path);
  if (!replace && !path.includes('#')) window.scrollTo(0, 0);
  route();
}

/* A left-click on a link to a UI path → navigate(); everything else (a
 * modifier, a middle-click, target=_blank, a download, another origin, /api,
 * an in-page `#anchor`) is left to the browser. Exported for the few links
 * whose own handler has to stopPropagation (a link inside a clickable row). */
function inAppClick(e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey
      || e.shiftKey || e.altKey) return false;
  const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
  if (!a || (a.target && a.target !== '_self') || a.hasAttribute('download')) return false;
  const raw = a.getAttribute('href') || '';
  if (raw.startsWith('#') && !raw.startsWith('#/')) return false;
  let u;
  try { u = new URL(a.href, location.href); } catch (_) { return false; }
  if (u.origin !== location.origin || !PLM_ROUTES.isAppPath(u.pathname)) return false;
  e.preventDefault();
  // an old `#/…` link inside rendered markdown (a context entry, a ticket
  // pasted into a note) is still a link to its new path
  const legacy = u.hash.startsWith('#/') ? PLM_ROUTES.fromHash(u.hash) : null;
  // an in-page anchor on an app path (`/pulls/380#merge`, the header's
  // "Approve…") rides along: the page that renders scrolls to it
  navigate(legacy || (u.pathname + u.search + u.hash));
  return true;
}
document.addEventListener('click', inAppClick);

/* A ref with a slash in it (`draft/v0.4.1`) is split against the repo's ref
 * names, longest first — GitHub's rule. The list is fetched only when the URL
 * is actually ambiguous, and kept for a few seconds (a new draft appears with
 * a push, so it is never cached for the visit). */
const REF_NAMES = new Map();
async function refNamesFor(number) {
  const hit = REF_NAMES.get(number);
  if (hit && Date.now() - hit.at < 15000) return hit.names;
  let names = [];
  try {
    const i = await repoGet(number, 'refs');
    names = ['main', i.default_ref, ...(i.releases || []).map(r => r.ref),
             ...(i.drafts || []).map(r => r.ref)].filter(Boolean);
  } catch (_) { /* no repo: the first segment is the ref, the page will say why */ }
  REF_NAMES.set(number, { at: Date.now(), names });
  return names;
}

async function routeTree(r) {
  const num = r.number;
  const parts = String(r.rest || '').split('/').filter(Boolean);
  const plain = parts.length > 1 && !decodeURIComponent(parts[0]).includes('/');
  const names = plain ? await refNamesFor(num) : [];
  if (location.pathname.indexOf('/' + r.kind + '/') < 0) return;   // moved on meanwhile
  let { ref, path } = PLM_ROUTES.splitRef(r.rest, names);
  let open = '';
  if (r.kind === 'blob' && path) {
    const cut = path.lastIndexOf('/');
    open = path.slice(cut + 1);
    path = cut < 0 ? '' : path.slice(0, cut);
  }
  // Same item, same ref, different DIRECTORY: walk the tree without
  // rebuilding the page around it.
  if (REPO && REPO.number === num && REPO.info && REPO.ref === ref
      && document.body.contains(REPO.mount)) { REPO.open = open; repoGoto(ref, path); }
  else renderItem(num, { ref, path, tab: 'part', open });
}

/* `/<N>/pull/<n>`: a proposal of <N>, or the change CHG-<n> that <N> is a
 * member of (scoped to <N>). The server decides — /api/items/<N>/pull/<n> —
 * because only it knows both sequences; a collision prefers the change and the
 * change page offers the proposal (`/<N>/pull/p<n>` always means the proposal). */
async function renderPull(number, n, tab = 'conversation') {
  const page = $('page');
  page.innerHTML = '';
  CURRENT_ITEM = `${number}#${n}`;
  page.appendChild(el('div', 'empty', `reading ${number} #${n}…`));
  let res;
  try { res = await api(`/api/items/${encodeURIComponent(number)}/pull/${n}`); }
  catch (e) {
    if (CURRENT_ITEM !== `${number}#${n}`) return;
    page.innerHTML = '';
    const crumbs = el('div', 'crumbs');
    const back = el('a', null, `← ${number}`); back.href = tabPath(number, 'drafts');
    crumbs.append(back, document.createTextNode(`  /  #${n}`));
    page.append(crumbs, el('div', 'empty', `Nothing at ${number} #${n}: ${e.message}`));
    return;
  }
  if (CURRENT_ITEM !== `${number}#${n}`) return;
  if (res.kind === 'change' && window.CHANGES_UI) {
    let flash = null;
    if (res.also_proposal) {
      flash = el('div', 'note');
      flash.style.margin = '8px 2px';
      flash.append(document.createTextNode(`${PLM_ROUTES.ref(n)} on ${number} is also proposal ${PLM_ROUTES.proposalLabel(n)}, `
        + `which is not part of ${PLM_ROUTES.ref(res.change)} — `));
      const a = el('a', null, `open proposal ${PLM_ROUTES.proposalLabel(n)}`);
      a.href = PLM_ROUTES.P.pullP(number, n);
      flash.appendChild(a);
    }
    CHANGES_UI.renderChange(res.change, flash, { member: number, n, tab });
    refreshApprovalCount();
  } else {
    renderProposal(number, 'p' + res.proposal);
  }
}

async function renderReleaseByTag(number, tag) {
  const page = $('page');
  page.innerHTML = '';
  CURRENT_ITEM = number;
  page.appendChild(el('div', 'empty', `reading ${number} ${tag}…`));
  let res;
  try { res = await api(`/api/items/${encodeURIComponent(number)}/releases/by-tag/`
                        + tag.split('/').map(encodeURIComponent).join('/')); }
  catch (e) {
    if (CURRENT_ITEM !== number) return;
    page.innerHTML = '';
    const crumbs = el('div', 'crumbs');
    const back = el('a', null, `← ${number}`); back.href = tabPath(number, 'releases');
    crumbs.append(back, document.createTextNode(`  /  releases  /  ${tag}`));
    // A tag nobody asked to promote has no release RECORD — its tree still is
    // the revision, so offer that rather than a dead end.
    const t = el('a', null, `browse ${tag}`); t.href = treePath(number, tag);
    const box = el('div', 'empty', `No release record for ${tag}: ${e.message}  `);
    box.appendChild(t);
    page.append(crumbs, box);
    return;
  }
  if (CURRENT_ITEM !== number) return;
  renderRelease(number, String(res.id));
}

function renderNotFound() {
  const page = $('page');
  page.innerHTML = '';
  const box = el('div', 'doc');
  box.appendChild(el('div', 'empty', `plm has no page at ${location.pathname}.`));
  const a = el('a', null, '← dashboard'); a.href = '/';
  page.append(box, a);
}

function route() {
  // 1. The hash era: `/#/change/CHG-380` → `/pulls/380`, replacing the
  //    history entry so Back does not bounce through the old form.
  if (location.hash.startsWith('#/')) {
    const to = PLM_ROUTES.fromHash(location.hash);
    if (to) history.replaceState(null, '', to);
  }
  const r = PLM_ROUTES.parse(location.pathname, location.search);
  document.title = PLM_ROUTES.title(r, INSTANCE_LABEL);
  const here = r.view === 'registry' ? 'registry'
    : r.view === 'approvals' || r.view === 'change' ? 'approvals'
    : r.view === 'dash' ? 'dash' : '';
  document.querySelectorAll('#mastNav a').forEach(a =>
    a.classList.toggle('on', a.dataset.nav === here));
  // A lightbox is not a routed state, so any navigation dismisses it — the back
  // button never leaves a preview floating over a different page.
  closeLightbox();
  // A review context belongs to the proposal / release / change page that
  // began it; any other page must not grow comment buttons for somebody
  // else's subject.
  if (window.REVIEW_UI && !['proposal', 'round', 'pull', 'release', 'change'].includes(r.view))
    REVIEW_UI.end();
  if (r.view === 'snip') {
    if (r.info) closeViewer();      // /info is the card alone
    renderSnip(r.token, { view: !r.info });
    whoami();
    return;
  }
  closeViewer();
  popClose();
  const num = r.number;
  switch (r.view) {
    case 'item': renderItem(num, { tab: r.tab, ref: r.ref || '' }); break;
    case 'tree': routeTree(r); break;
    case 'compare':
      renderCompare(num, `${encodeURIComponent(r.base)}...${encodeURIComponent(r.head)}`
        + location.search);
      break;
    case 'pull': renderPull(num, r.n, r.tab); break;
    case 'proposal': renderProposal(num, 'p' + r.pid); break;
    case 'round': renderProposal(num, String(r.aid || '')); break;
    case 'release':
      if (r.tag) renderReleaseByTag(num, r.tag); else renderRelease(num, String(r.id));
      break;
    case 'search': renderSearch(r.text || ''); break;
    case 'project': renderProject(r.project); break;
    case 'change':
      if (window.CHANGES_UI) { CHANGES_UI.renderChange(r.id, null, { tab: r.tab }); refreshApprovalCount(); }
      break;
    case 'release-set':
      if (window.CHANGES_UI) { CHANGES_UI.renderReleaseSet(r.id); refreshApprovalCount(); }
      break;
    case 'registry':
      if (r.q.project !== undefined) R.project = r.q.project;
      renderRegistry(); refreshApprovalCount();
      break;
    case 'approvals': renderApprovals(); break;
    case 'dash': renderDashboard(); break;
    default: renderNotFound(); refreshApprovalCount();
  }
  whoami();
}

(async function badge() {
  try {
    const st = await api('/api/stats');
    if (st.label) {
      const b = $('instanceBadge');
      b.textContent = st.label;
      b.classList.remove('hide');
      // the page title follows the page; if it is still the route's plain
      // one (no richer title landed yet), re-derive it with the label in
      const r = PLM_ROUTES.parse(location.pathname, location.search);
      const plain = PLM_ROUTES.title(r, '');
      INSTANCE_LABEL = st.label;
      if (document.title === plain) document.title = PLM_ROUTES.title(r, INSTANCE_LABEL);
    }
  } catch {}
})();
addEventListener('popstate', route);
// A pasted `#/…` on an open page changes only the hash: map it like a load.
addEventListener('hashchange', () => { if (location.hash.startsWith('#/')) route(); });
route();
// Keep the masthead approval count live without a reload: submissions arrive
// from agents, not only from this browser.
refreshApprovalCount();
setInterval(refreshApprovalCount, 60000);
