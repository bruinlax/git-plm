/* routes.js — plm's URL shapes, as pure functions (stage 22: real paths).
 *
 * Geoff: "please move to real paths". The UI's addresses are GitHub's, verb for
 * verb, and this file is the ONE place that knows them:
 *
 *   /                                       dashboard
 *   /<N>                                    the repo home (Part tab)
 *   /<N>/<tab>[?ref=<ref>]                  bom releases drafts history record context
 *   /<N>/tree/<ref>[/<dir>…]                the repo band at a ref (+ a directory)
 *   /<N>/blob/<ref>/<path>                  …at a file
 *   /<N>/compare/<base>...<head>[?file=|mesh=]
 *   /<N>/pull/<n>                           a proposal of <N>, or — when <N> is a
 *                                           member of CHG-<n> — that change, scoped
 *                                           to <N> (the server decides: /api/items/
 *                                           <N>/pull/<n>; a collision prefers the
 *                                           change)
 *   /<N>/pull/p<n>                          ALWAYS proposal #n (the unambiguous alias)
 *   /<N>/proposal/<approval id>             one review ROUND → redirects to its pull
 *   /<N>/releases/tag/<vX.Y.Z>              the newest release record of that tag
 *   /<N>/releases/<id>                      one release record
 *   /pulls/<n>[/<tab>]                      a pull request (CHG-<n> is its legacy label)
 *                                           across its member repos; <tab> = files |
 *                                           checks | commits, none = Conversation
 *                                           (GitHub's four PR tabs)
 *   /pulls                                  the list (Open / Closed) — the Approvals page
 *   /<N>/pull/<n>/<tab>                     the same tabs, scoped to member <N>
 *   /<N>/pulls                              the item's Pull requests tab
 *   /changes/CHG-<n>…, /item/<N>/…          old page paths: NOT ours — the server 301s
 *                                           them (plm/webroutes.py _LEGACY, the one table),
 *                                           so an in-page link to one is a full load
 *   /release-sets/<n>                       a release set
 *   /registry  /approvals  /search?q=  /projects/<p>  /snip/<token>[/info]
 *
 * A ref may contain "/" (`draft/v0.4.1`). It is written with its slashes
 * LITERAL, like GitHub, and read back by trying the repo's ref names
 * longest-first (`splitRef`). A `%2F`-encoded ref (the hash era's spelling) is
 * still understood. git forbids `..` in a ref name, so `...` in a compare spec
 * is never ambiguous.
 *
 * The old hash forms (`/#/item/N/…`) keep working forever: `fromHash` maps one
 * to its path and the router `location.replace`s to it before anything renders.
 *
 * No DOM, no fetch: node can load this (`tests/test_web_routes.py` runs
 * web/routes.test.js), and the server's own copy of the first-segment rule is
 * plm/webroutes.py. Keep the two in step.
 */
(function (root) {
  'use strict';

  // An item number: PREFIX-CLASS-SEQ (plm/ops.py NUMBER_RE) — loosened to allow
  // the one legacy shape with two classes (LFD-PRT-PRT-0001). Fixed words are
  // lower-case, so they can never collide with one.
  const ITEM_RE = /^[A-Z][A-Z0-9]*(?:-[A-Z]+)+-\d+$/;
  const WORDS = ['registry', 'approvals', 'search', 'projects', 'pulls',
                 'release-sets', 'snip'];
  const TABS = ['part', 'bom', 'releases', 'drafts', 'pulls', 'history', 'record', 'context'];
  // A change's tabs — GitHub's pull request: Conversation (no segment),
  // Commits, Checks, Files changed. plm/webroutes.py CHANGE_TABS is the same.
  const CHANGE_TABS = ['commits', 'checks', 'files'];
  const TAB_LABEL = { conversation: 'Conversation', commits: 'Commits',
                      checks: 'Checks', files: 'Files changed' };
  const ctab = (t) => (CHANGE_TABS.includes(t) ? t : '');

  const enc = encodeURIComponent;
  const dec = (s) => { try { return decodeURIComponent(s); } catch (e) { return s; } };
  // A ref or a repo path: every segment encoded, the slashes kept.
  const segs = (p) => String(p || '').split('/').map(enc).join('/');
  const qs = (q) => {
    const p = Object.entries(q || {}).filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${enc(k)}=${enc(v)}`).join('&');
    return p ? '?' + p : '';
  };
  const parseQuery = (search) => {
    const out = {};
    const s = String(search || '').replace(/^\?/, '');
    if (!s) return out;
    for (const kv of s.split('&')) {
      if (!kv) continue;
      const i = kv.indexOf('=');
      const k = dec((i < 0 ? kv : kv.slice(0, i)).replace(/\+/g, ' '));
      out[k] = i < 0 ? '' : dec(kv.slice(i + 1).replace(/\+/g, ' '));
    }
    return out;
  };
  // A pull request's number from any spelling it has had — `CHG-380`, `CHG380`,
  // `#380`, `380` (plm/webroutes.py LABEL_RE is the same rule); anything else
  // comes back as it was given.
  const LABEL_RE = /^\s*(?:CHG-?|#)?(\d+)\s*$/i;
  const chgId = (id) => { const m = LABEL_RE.exec(String(id)); return m ? m[1] : String(id); };
  // How people say them (plm/changes.py `ref` / `title_ref`, proposals.label)
  const ref = (id) => '#' + chgId(id);
  const titleRef = (id) => 'Pull request ' + ref(id);
  const proposalLabel = (pid) => 'p' + pid;

  /* ------------------------------------------------------------ builders */
  const PULLS = '/pulls';     // the list of pull requests (plm/webroutes.py PULLS)
  const P = {
    home: () => '/',
    item: (n) => '/' + enc(n),
    tab: (n, tab, ref = '') => (tab === 'part' ? '/' + enc(n) : `/${enc(n)}/${tab}`)
      + (ref ? '?ref=' + enc(ref) : ''),
    tree: (n, ref, path = '') => `/${enc(n)}/tree/${segs(ref)}`
      + (path ? '/' + segs(path) : ''),
    blob: (n, ref, path) => path ? `/${enc(n)}/blob/${segs(ref)}/${segs(path)}`
      : P.tree(n, ref),
    compare: (n, base, head, q = {}) =>
      `/${enc(n)}/compare/${segs(base)}...${segs(head)}` + qs(q),
    pull: (n, pid, tab = '') => `/${enc(n)}/pull/${enc(pid)}` + (ctab(tab) ? '/' + tab : ''),
    pullP: (n, pid) => `/${enc(n)}/pull/p${enc(pid)}`,
    round: (n, aid) => `/${enc(n)}/proposal/${enc(aid)}`,
    release: (n, id, tag = '') => tag ? `/${enc(n)}/releases/tag/${segs(tag)}`
      : `/${enc(n)}/releases/${enc(id)}`,
    releases: (n) => `/${enc(n)}/releases`,
    change: (id, tab = '') => `${PULLS}/${enc(chgId(id))}` + (ctab(tab) ? '/' + tab : ''),
    releaseSet: (id) => `/release-sets/${enc(String(id).replace(/^RS-?/i, ''))}`,
    registry: (q = {}) => '/registry' + qs(q),
    search: (q) => '/search' + qs({ q }),
    project: (p) => '/projects/' + enc(p),
    snip: (t, info = false) => '/snip/' + enc(t) + (info ? '/info' : ''),
  };

  /* A UI path's first segment is an item number or a fixed word; anything else
   * is not ours (the server answers 404 for it, see plm/webroutes.py). */
  function isAppPath(pathname) {
    const p = String(pathname || '');
    if (p === '/' || p === '') return true;
    const first = dec(p.replace(/^\//, '').split('/')[0]);
    return ITEM_RE.test(first) || WORDS.includes(first);
  }

  /* `rest` = the encoded segments after /tree/ or /blob/. Returns {ref, path}.
   * Longest-first against `refNames` (GitHub's rule); a whole-segment `%2F`
   * ref is taken as-is; with no ref list, `draft/<x>` is the one known
   * two-segment shape and anything else is one segment (a tag, `main`, a sha). */
  function splitRef(rest, refNames) {
    const raw = String(rest || '').replace(/^\/+|\/+$/g, '');
    if (!raw) return { ref: '', path: '' };
    const parts = raw.split('/');
    const first = dec(parts[0]);
    if (first.includes('/')) return { ref: first, path: parts.slice(1).map(dec).join('/') };
    const d = parts.map(dec);
    const names = new Set(refNames || []);
    if (names.size) {
      for (let k = d.length; k >= 1; k--) {
        const cand = d.slice(0, k).join('/');
        if (names.has(cand)) return { ref: cand, path: d.slice(k).join('/') };
      }
    } else if (d[0] === 'draft' && d.length > 1) {
      return { ref: d.slice(0, 2).join('/'), path: d.slice(2).join('/') };
    }
    return { ref: d[0], path: d.slice(1).join('/') };
  }

  /* pathname + search → what to render. Pure; ref splitting that needs the ref
   * list is left to the caller (`rest` is handed back for /tree and /blob). */
  function parse(pathname, search = '') {
    const q = parseQuery(search);
    const p = String(pathname || '/');
    const raw = p.replace(/^\/+/, '').replace(/\/+$/, '');
    if (!raw) return { view: 'dash', q };
    const parts = raw.split('/');
    const first = dec(parts[0]);
    const tail = parts.slice(1);
    switch (first) {
      case 'registry': return { view: 'registry', q };
      case 'approvals': return { view: 'approvals', q };
      case 'search': return { view: 'search', text: q.q || dec(tail.join('/')), q };
      case 'projects':
        return tail[0] ? { view: 'project', project: dec(tail[0]), q } : { view: 'registry', q };
      case 'pulls':
        return tail[0] ? { view: 'change', id: chgId(dec(tail[0])), tab: ctab(tail[1]) || 'conversation', q }
          : { view: 'approvals', q };
      case 'release-sets':
        return tail[0] ? { view: 'release-set', id: dec(tail[0]), q } : { view: 'approvals', q };
      case 'snip':
        if (!tail[0]) break;
        return { view: 'snip', token: dec(tail[0]), info: tail[1] === 'info', q };
      default: break;
    }
    if (!ITEM_RE.test(first)) return { view: 'notfound', q };
    const number = first;
    const verb = tail[0] || '';
    const rest = tail.slice(1).join('/');
    if (!verb) return { view: 'item', number, tab: 'part', ref: q.ref || '', q };
    if (verb === 'tree' || verb === 'blob') {
      return { view: 'tree', number, kind: verb, rest, q };
    }
    if (verb === 'compare') {
      const at = rest.indexOf('...');
      if (at < 0) return { view: 'compare', number, base: '', head: '', q };
      return { view: 'compare', number, base: dec(rest.slice(0, at)),
               head: dec(rest.slice(at + 3)), q };
    }
    if (verb === 'pull' || verb === 'pulls') {
      const n = tail[1] || '';
      if (/^p\d+$/.test(n)) return { view: 'proposal', number, pid: +n.slice(1), q };
      if (/^\d+$/.test(n)) return { view: 'pull', number, n: +n, tab: ctab(tail[2]) || 'conversation', q };
      return { view: 'item', number, tab: 'pulls', ref: '', q };   // GitHub's /pulls list
    }
    if (verb === 'proposal') {               // the hash era's verb, kept as an alias
      const n = tail[1] || '';
      if (/^p\d+$/.test(n)) return { view: 'proposal', number, pid: +n.slice(1), q };
      return { view: 'round', number, aid: parseInt(n, 10) || 0, q };
    }
    if (verb === 'releases' || verb === 'release') {
      if (tail[1] === 'tag' && tail[2])
        return { view: 'release', number, tag: dec(tail.slice(2).join('/')), q };
      if (/^\d+$/.test(tail[1] || '')) return { view: 'release', number, id: +tail[1], q };
      return { view: 'item', number, tab: 'releases', ref: q.ref || '', q };
    }
    if (TABS.includes(verb)) return { view: 'item', number, tab: verb, ref: q.ref || '', q };
    return { view: 'notfound', number, q };
  }

  /* An old `#/…` location → its path (or null when `hash` is not a route). */
  function fromHash(hash) {
    const h = String(hash || '');
    if (!h.startsWith('#/') && h !== '#') return null;
    let body = h.slice(1);                       // '/item/N/…?x=y'
    let query = '';
    const qi = body.indexOf('?');
    if (qi >= 0) { query = body.slice(qi); body = body.slice(0, qi); }
    const parts = body.replace(/^\/+/, '').split('/');
    const w = parts[0] || '';
    const rest = parts.slice(1);
    switch (w) {
      case '': return '/';
      case 'registry': return '/registry' + query;
      case 'approvals': return '/approvals';
      case 'search': return P.search(dec(rest.join('/')));
      case 'project': return rest[0] ? P.project(dec(rest[0])) : '/registry';
      case 'change': return rest[0] ? P.change(dec(rest[0])) : PULLS;
      case 'release-set': return rest[0] ? '/release-sets/' + rest[0] : '/approvals';
      case 'snip': return rest[0] ? '/snip/' + rest[0] + (rest[1] === 'info' ? '/info' : '') : '/';
      case 'item': break;
      default: return '/';
    }
    if (!rest[0]) return '/';
    const n = dec(rest[0]);
    const verb = rest[1] || '';
    const more = rest.slice(2);
    if (!verb) return P.item(n) + query;
    if (verb === 'view' || verb === 'file') return P.item(n);
    if (verb === 'ref') {
      if (!more.length) return P.item(n);
      const ref = dec(more[0]);
      const path = more.slice(1).map(dec).join('/');
      return P.tree(n, ref, path);
    }
    if (verb === 'compare') {
      const spec = more.join('/');
      const at = spec.indexOf('...');
      if (at < 0) return `/${enc(n)}/compare/${spec}${query}`;
      return P.compare(n, dec(spec.slice(0, at)), dec(spec.slice(at + 3))) + query;
    }
    if (verb === 'proposal') {
      const x = more[0] || '';
      if (/^p\d+$/.test(x)) return P.pull(n, x.slice(1));
      return x ? P.round(n, x) : P.tab(n, 'drafts');
    }
    if (verb === 'release') return more[0] ? `/${enc(n)}/releases/${more[0]}` : P.releases(n);
    if (TABS.includes(verb)) return `/${enc(n)}/${verb}` + query;
    return P.item(n);
  }

  /* The tab title, GitHub's way: the subject first, then the site. */
  function title(r, label = '') {
    const site = label ? `plm [${label}]` : 'plm';
    const t = (s) => `${s} · ${site}`;
    switch (r.view) {
      case 'dash': return label ? `plm [${label}] — part registry` : 'plm — part registry';
      case 'registry': return t('Registry');
      case 'approvals': return t('Pull requests');
      case 'search': return t(`Search · ${r.text || ''}`);
      case 'project': return t(r.project);
      case 'change': {
        const id = titleRef(r.id);
        return t(r.tab && r.tab !== 'conversation' ? `${TAB_LABEL[r.tab]} · ${id}` : id);
      }
      case 'release-set': return t(`Release set ${r.id}`);
      case 'snip': return t('Snippet');
      case 'compare': return t(`Comparing ${r.base}...${r.head} · ${r.number}`);
      case 'pull': return t((r.tab && r.tab !== 'conversation' ? TAB_LABEL[r.tab] + ' · ' : '')
        + `#${r.n} · ${r.number}`);
      case 'proposal': return t(`Proposal ${proposalLabel(r.pid)} · ${r.number}`);
      case 'release': return t(`Release ${r.tag || '#' + r.id} · ${r.number}`);
      case 'notfound': return t('Not found');
      default: return r.number ? t(r.number) : site;
    }
  }

  const api = { ITEM_RE, WORDS, TABS, CHANGE_TABS, TAB_LABEL, P, isAppPath, splitRef, parse, parseQuery,
                fromHash, title, chgId, ref, titleRef, proposalLabel, PULLS };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PLM_ROUTES = api;
})(typeof window !== 'undefined' ? window : this);
