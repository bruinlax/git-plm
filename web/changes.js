/* changes.js — the PULL REQUEST page, the RELEASE-SET page, "Waiting on you",
 * the list of pull requests (/pulls, an item's tab, a project page) (stage 14).
 *
 * Geoff, 2026-10-16: "if you call it 'pull' then let's just call it pull
 * request". The object is still a CHANGE in the code and the API (`/api/
 * changes`, CHG-n — the legacy label, accepted everywhere); everything a person
 * reads says pull request and `#380`.
 *
 * Geoff: "I'd like my workflow to be like the agent is asking me to approve a
 * pull request to main for the project — and then also approve a release."
 *
 *   /pulls/n[/tab]       one page for one piece of work across N repos, shaped
 *                        like a GitHub pull request (§2.9): header, the four
 *                        tabs (Conversation / Commits / Checks / Files changed)
 *                        and a sidebar. It MOUNTS the shared components — the
 *                        checks board `CHECKS.card`, the thread hooks of
 *                        web/review.js through its LENS, the timeline
 *                        `propTimeline`, and every changed file through the
 *                        diff-form registry (web/diffwidgets.js) — and has ONE
 *                        merge box. `/<N>/pull/<n>[/tab]` is it, scoped to <N>.
 *   /release-sets/n      the closure tree with each member's readiness, bulk
 *                        attestation, blockers linked to the change that must
 *                        merge first, and ONE decision.
 *
 * Self-contained like checks.js and review.js: its own CSS, one
 * `window.CHANGES_UI` global. It borrows app.js's helpers (`el`, `api`, `post`,
 * `when`, `stamp`, `whoChip`, `refusalBox`, `decisionBody`, `toast`, `repoGet`,
 * `propTimeline`, `treePath`, `cmpPath`, `mdToHtml`) — globals in the same
 * document — and PLM_DIFF. Nothing here is a second implementation of any of
 * them. Also here: the repo/project BANNERS and the masthead BELL.
 */
(function () {
  'use strict';

  const CSS = `
.chg-pill { font:600 9.5px/1 var(--ui); letter-spacing:.1em; text-transform:uppercase;
  border:1px solid var(--rule-2); border-radius:2px; padding:3px 6px; color:var(--ink-2); }
.chg-pill.open { color:var(--blue); border-color:var(--blue); }
.chg-pill.merged, .chg-pill.approved { color:var(--st-released); border-color:var(--st-released); }
.chg-pill.rejected { color:var(--st-obsolete); border-color:var(--st-obsolete); }
.chg-pill.draft, .chg-pill.requested, .chg-pill.in_review { color:var(--st-prototype); border-color:var(--st-prototype); }
.chg-num { font:600 13px var(--mono); color:var(--ink); }
.chg-facts { display:flex; flex-wrap:wrap; gap:6px 16px; margin-top:10px; padding-top:9px;
  border-top:1px solid var(--rule); font:12px var(--mono); color:var(--ink-2); }
.chg-facts b { color:var(--ink); font-weight:600; }
.chg-facts .plus { color:var(--plm-added); } .chg-facts .minus { color:var(--plm-removed); }
/* impact */
.imp-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:6px 12px;
  padding:9px 12px; border-top:1px solid var(--rule); align-items:start; }
.imp-row:first-of-type { border-top:0; }
.imp-row .s { font-size:12.5px; line-height:1.45; min-width:0; }
.imp-row .s a { font-family:var(--mono); }
.imp-row .n { font-size:12px; color:var(--ink-2); margin-top:3px; white-space:pre-wrap; }
.imp-row .who { font:11px var(--mono); color:var(--ink-3); margin-top:2px; }
.disp { font:600 10px/1 var(--ui); letter-spacing:.08em; text-transform:uppercase;
  padding:3px 6px; border-radius:2px; border:1px solid var(--rule-2); white-space:nowrap; }
.disp.bump { color:var(--blue); border-color:var(--blue); }
.disp.use_as_is { color:var(--st-released); border-color:var(--st-released); }
.disp.no_impact { color:var(--ink-2); }
.disp.none { color:var(--st-prototype); border-color:var(--st-prototype); border-style:dashed; }
.disp.drafted { border-style:dashed; }
.imp-edit { grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.imp-edit input { flex:1 1 220px; min-width:0; font:12.5px var(--ui); padding:3px 7px;
  border:1px solid var(--rule-2); }
/* member sections */
.repin { font:11.5px var(--mono); color:var(--blue); }
.repin.done { color:var(--st-released); }
.mprob { font-size:12px; color:#8A1C13; margin-top:4px; white-space:pre-wrap; }
/* decision */
.chg-dec .pf { border:1px solid var(--rule); margin:0 0 10px; }
.chg-dec .pf.ok { border-left:3px solid var(--st-released); padding:7px 10px; font-size:12.5px;
  color:#0F5124; background:var(--add-wash); }
.chg-dec .pf.bad { border-left:3px solid var(--st-obsolete); background:#FFF6F4; }
.chg-dec .pf.bad .hd { padding:7px 10px; font-size:12.5px; font-weight:600; color:#8A1C13; }
.chg-dec .pf .pr { padding:5px 10px 7px; border-top:1px solid var(--rule); font-size:12.5px; }
.chg-dec .pf .pr .m { font:600 12px var(--mono); }
.chg-dec .pf .pr .f { font:11.5px var(--mono); color:var(--ink-2); white-space:pre-wrap; margin-top:2px; }
.chg-dec .heads { font:11.5px var(--mono); color:var(--ink-3); margin:0 0 8px; overflow-wrap:anywhere; }
.chg-dec .policyline { margin-bottom:8px; }
.chg-dec .acts { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
.chg-dec textarea { width:100%; min-height:56px; font:13px/1.45 var(--ui); padding:6px 8px;
  border:1px solid var(--rule-2); box-sizing:border-box; }
.chg-done { border:2px solid var(--st-released); background:var(--add-wash); padding:10px 12px; margin:0 0 10px; }
.chg-done .hd { font:600 14px var(--ui); color:#0F5124; margin-bottom:6px; }
.chg-done .row { font:12px var(--mono); padding:3px 0; border-top:1px solid #cfe5d5;
  display:flex; flex-wrap:wrap; gap:4px 10px; }
.chg-done .row:first-of-type { border-top:0; }
.chg-done .row .ok { color:var(--st-released); font-weight:600; }
/* release set */
.rs-tree { padding:6px 0; }
.rs-node { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px 10px;
  padding:8px 12px; border-top:1px solid var(--rule); align-items:center; }
.rs-node:first-child { border-top:0; }
.rs-node .nm { min-width:0; overflow-wrap:anywhere; }
.rs-node .nm .ind { color:var(--ink-3); font:12px var(--mono); }
.rs-node .nm a.num { font:600 12.5px var(--mono); }
.rs-node .nm .v { font:12px var(--mono); color:var(--ink-2); }
.rs-node .ready { display:flex; flex-wrap:wrap; gap:4px 10px; font:11.5px var(--mono);
  color:var(--ink-2); justify-content:flex-end; }
.rs-node .ready .good { color:var(--st-released); } .rs-node .ready .bad { color:#8A1C13; }
.rs-node.blocker { background:#FFF6F4; box-shadow:inset 3px 0 0 var(--st-obsolete); }
.rs-node.released .nm { color:var(--ink-3); }
.rs-node .tag { font:600 9.5px var(--ui); letter-spacing:.08em; text-transform:uppercase;
  border:1px solid var(--rule-2); padding:1px 5px; border-radius:2px; color:var(--ink-3); }
.rs-node .tag.prom { color:var(--blue); border-color:var(--blue); }
.rs-node .tag.blk { color:#8A1C13; border-color:#8A1C13; }
.rs-grid { width:100%; border-collapse:collapse; font-size:12.5px; }
.rs-grid th, .rs-grid td { border-top:1px solid var(--rule); padding:6px 8px; text-align:left;
  vertical-align:top; }
.rs-grid th { font:600 10px/1.2 var(--ui); letter-spacing:.08em; text-transform:uppercase;
  color:var(--ink-3); background:#FBFCFA; }
.rs-grid td.c { text-align:center; font:12px var(--mono); white-space:nowrap; }
.rs-grid td.c.done { color:var(--st-released); }
.rs-grid td.c.todo { color:var(--ink-3); }
.rs-grid td.c .ev { display:block; font-size:10.5px; color:var(--blue); }
.rs-grid .bulk { display:flex; flex-wrap:wrap; gap:5px; margin-top:5px; }
.rs-grid .bulk input { flex:1 1 160px; min-width:0; font:12px var(--ui); padding:2px 6px;
  border:1px solid var(--rule-2); }
.rs-scroll { overflow-x:auto; }
/* the list of pull requests (one renderer, three scopes) */
.pl-sw { display:flex; gap:4px; }
.pl-sw .btn.on { font-weight:700; border-color:var(--ink-2); }
.pl-row { display:grid; grid-template-columns:auto minmax(0,1fr); gap:4px 10px; padding:9px 12px;
  border-top:1px solid var(--rule); cursor:pointer; align-items:start; }
.pl-row:first-of-type { border-top:0; }
.pl-row:hover { background:var(--blue-bg); }
.pl-row .pl-ic { font:600 14px var(--mono); color:var(--st-released); padding-top:1px; }
.pl-row .pl-ic.open { color:var(--blue); } .pl-row .pl-ic.withdrawn { color:var(--ink-3); }
.pl-row .l1 { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.pl-row .l1 .tt { font-weight:600; color:var(--ink); }
.pl-row .l2 { font:11.5px var(--mono); color:var(--ink-3); margin-top:3px; overflow-wrap:anywhere; }
.pl-row .l2 .num { color:var(--ink-2); }
.tl-img img { display:block; max-width:min(480px,100%); margin-top:6px; border:1px solid var(--rule); }
/* waiting on you */
.wait-row { display:grid; grid-template-columns:auto minmax(0,1fr) auto; gap:4px 12px;
  padding:10px 12px; border-top:1px solid var(--rule); align-items:center; cursor:pointer; }
.wait-row:first-of-type { border-top:0; }
.wait-row:hover { background:var(--blue-bg); }
.wait-row .k { font:600 9.5px/1 var(--ui); letter-spacing:.1em; text-transform:uppercase;
  padding:4px 6px; border-radius:2px; color:#fff; background:var(--blue); white-space:nowrap; }
.wait-row .k.rel { background:var(--st-released); }
.wait-row .t { min-width:0; }
.wait-row .t .l1 { font-weight:600; overflow-wrap:anywhere; }
.wait-row .t .l1 .num { font:600 12.5px var(--mono); margin-right:6px; }
.wait-row .t .l2 { display:flex; flex-wrap:wrap; gap:3px 10px; font:11.5px var(--mono);
  color:var(--ink-2); margin-top:3px; }
.wait-row .t .l2 .warn { color:#8A1C13; }
.wait-row .go { white-space:nowrap; text-decoration:none; }
#waiting { border:2px solid var(--ink); }
#waiting > h3 { background:var(--ink); color:var(--sheet); }
#waiting > h3 .note { color:#c9ced4; }
.copyline { display:flex; gap:6px; align-items:center; margin-top:6px; }
.copyline code { flex:1 1 auto; min-width:0; overflow-x:auto; white-space:nowrap;
  font:11.5px var(--mono); background:var(--paper); padding:3px 6px; border:1px solid var(--rule); }
/* ---- the change page as a pull request (§2.9) ---- */
.pr-head { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:6px 18px; align-items:start;
  padding:4px 0 14px; border-bottom:1px solid var(--rule-2); margin-bottom:0; }
.pr-title { grid-column:1 / -1; font:600 22px/1.25 var(--ui); margin:0; overflow-wrap:anywhere; }
.pr-num { font:400 20px var(--mono); color:var(--ink-3); }
.pr-meta { display:flex; flex-wrap:wrap; gap:6px 12px; align-items:center; font-size:13px; min-width:0; }
.pr-meta code, .pr-gh code { font:12px var(--mono); background:var(--blue-bg); color:var(--blue);
  padding:1px 5px; border-radius:3px; }
.pr-state { font:600 12px/1 var(--ui); padding:6px 11px; border-radius:14px; color:#fff;
  background:var(--st-released); white-space:nowrap; }
.pr-state.open { background:#1F7A3A; } .pr-state.draft { background:var(--ink-3); }
.pr-state.merged, .pr-state.approved { background:#6D28A8; }
.pr-state.rejected, .pr-state.withdrawn { background:var(--st-obsolete); }
.pr-who { min-width:0; overflow-wrap:anywhere; }
.pr-dim { color:var(--ink-3); font-size:12px; }
.pr-small { font-size:11.5px; margin-top:3px; }
.pr-policy { font:600 10px/1 var(--ui); letter-spacing:.08em; text-transform:uppercase;
  border:1px solid var(--rule-2); padding:4px 7px; border-radius:2px; color:var(--ink-2); }
.pr-act { display:flex; flex-direction:column; align-items:flex-end; gap:6px; }
.pr-act > a.btn { text-decoration:none; }
.pr-act .btn + .btn { margin-left:6px; }
.pr-act { flex-direction:row; flex-wrap:wrap; justify-content:flex-end; max-width:440px; }
.pr-verdict { flex-basis:100%; text-align:right; font:12px var(--mono); }
.pr-verdict.ok { color:var(--st-released); } .pr-verdict.bad { color:#8A1C13; }
.pr-verdict.idle { color:var(--ink-3); }
.pr-wait { font-size:12.5px; color:var(--st-prototype); font-weight:600; }
.pr-tabs { display:flex; gap:2px; border-bottom:1px solid var(--rule-2); margin:10px 0 16px;
  overflow-x:auto; align-items:flex-end; }
.pr-tab { padding:9px 14px 8px; font-size:13.5px; color:var(--ink-2); border:1px solid transparent;
  border-bottom:0; border-radius:4px 4px 0 0; white-space:nowrap; margin-bottom:-1px; }
.pr-tab:hover { text-decoration:none; background:var(--sheet); }
.pr-tab.on { background:var(--paper); border-color:var(--rule-2); color:var(--ink); font-weight:600;
  box-shadow:inset 0 2px 0 var(--st-prototype); background:var(--sheet); }
.pr-count { display:inline-block; margin-left:6px; min-width:18px; padding:0 6px; border-radius:9px;
  background:var(--rule); color:var(--ink-2); font:600 11px/18px var(--mono); text-align:center; }
.pr-count.red { background:#F6D6D2; color:#8A1C13; } .pr-count.amber, .pr-count.pending { background:var(--st-prototype-bg); color:var(--st-prototype); }
.pr-count.green { background:var(--st-released-bg); color:var(--st-released); }
.pr-scope { margin-left:auto; font-size:12px; padding:0 4px 8px; white-space:nowrap; }
.pr-grid { display:grid; grid-template-columns:minmax(0,1fr) 250px; grid-template-areas:"main side"; gap:20px; }
.pr-main { grid-area:main; min-width:0; }
.pr-side { grid-area:side; min-width:0; }
.pr-sec { border-bottom:1px solid var(--rule); padding:0 0 10px; margin:0 0 10px; font-size:12.5px; }
.pr-sech { font:600 12px var(--ui); color:var(--ink-2); margin-bottom:6px; }
.pr-sr { display:flex; gap:7px; align-items:center; margin:3px 0; }
.pr-av { width:20px; height:20px; border-radius:50%; background:var(--ink); color:var(--sheet);
  font:600 10.5px/20px var(--ui); text-align:center; flex:none; }
.pr-sr .pr-dim { margin-left:auto; }
.pr-you { color:var(--st-released); font-weight:600; }
.pr-mem { padding:4px 0; border-top:1px solid var(--rule); }
.pr-mem:first-of-type { border-top:0; }
.pr-mem.on { box-shadow:inset 3px 0 0 var(--blue); padding-left:6px; }
.pr-mem .ord { font:600 10.5px var(--mono); color:var(--ink-3); margin-right:6px; }
.pr-mem .num { font:600 12px var(--mono); margin-right:6px; }
.pr-mem .nm { font-size:11.5px; color:var(--ink-2); margin-top:2px; overflow-wrap:anywhere; }
.pr-card { background:var(--sheet); border:1px solid var(--rule-2); border-radius:4px; margin:0 0 14px; }
.pr-design-row { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px 16px; padding:10px 0; border-top:1px solid var(--rule); }
.pr-design-row:first-of-type { margin-top:10px; }
.pr-design summary { cursor:pointer; margin:8px 0 0; }
.pr-design .btn { text-decoration:none; }
.pr-cardh { display:flex; gap:6px; align-items:baseline; flex-wrap:wrap; padding:8px 12px;
  background:#FBFCFA; border-bottom:1px solid var(--rule); font-size:12.5px; border-radius:4px 4px 0 0; }
.pr-right { margin-left:auto; font-size:12px; }
.pr-card .pad { padding:8px 12px; }
.pr-card .pad + .pad { padding-top:0; }
.pr-body .mddoc { font-size:13.5px; }
.pr-heroimg { display:block; width:100%; max-height:340px; object-fit:contain; background:#fff; cursor:zoom-in; }
.pr-heroimg.big { max-height:none; cursor:zoom-out; }
.pr-herofoot { display:flex; flex-wrap:wrap; gap:6px 14px; align-items:center; font-size:12px; border-top:1px solid var(--rule); }
.pr-ckrow { display:flex; flex-wrap:wrap; gap:6px 12px; align-items:center; padding:7px 12px;
  border-top:1px solid var(--rule); }
.pr-ckrow:first-of-type { border-top:0; }
.pr-ckrow .num { font:600 12px var(--mono); min-width:110px; }
details.pr-impact > summary { cursor:pointer; list-style:none; }
details.pr-impact > summary::-webkit-details-marker { display:none; }
details.pr-impact > summary::before { content:'▸'; color:var(--ink-3); margin-right:4px; }
details.pr-impact[open] > summary::before { content:'▾'; }
.pr-needs { margin-left:auto; font:600 10px var(--ui); letter-spacing:.08em; text-transform:uppercase;
  color:var(--st-prototype); border:1px dashed var(--st-prototype); padding:2px 6px; border-radius:2px; }
.pr-merge { border:2px solid var(--ink); }
.pr-will { margin:4px 0 10px; padding-left:22px; font-size:12.5px; }
.pr-will li { margin:3px 0; }
.pr-will code { font:12px var(--mono); }
.pr-waitbox { border-left:3px solid var(--st-prototype); background:var(--st-prototype-bg); padding:8px 10px;
  font-size:12.5px; margin-top:8px; }
.pr-gh { display:flex; flex-wrap:wrap; gap:4px 10px; align-items:center; padding:8px 12px; background:#FBFCFA;
  border-bottom:1px solid var(--rule); font-size:12.5px; }
.pr-gh .num { font:600 13px var(--mono); }
.pr-gh .nm { color:var(--ink-2); }
.pr-gh .rng { font-size:12px; color:var(--ink-2); }
.pr-tally { font:11.5px var(--mono); display:inline-flex; gap:6px; align-items:baseline; }
.pr-tally .plus, .pr-filesum .plus { color:var(--plm-added); }
.pr-tally .minus, .pr-filesum .minus { color:var(--plm-removed); }
.pr-filesum { font:12.5px var(--mono); margin:0 0 10px; display:flex; flex-wrap:wrap; gap:4px 8px; align-items:baseline; }
.pr-filesum .pr-dim { font-family:var(--ui); }
details.pr-group { background:var(--sheet); border:1px solid var(--rule-2); border-radius:4px; margin:0 0 14px; }
details.pr-group > summary { list-style:none; cursor:pointer; }
details.pr-group > summary::-webkit-details-marker { display:none; }
details.pr-group > summary .pr-gh::before { content:'▸'; color:var(--ink-3); }
details.pr-group[open] > summary .pr-gh::before { content:'▾'; }
details.pr-group:not([open]) > summary .pr-gh { border-bottom:0; }
.pr-warns { padding:8px 12px; font-size:12px; color:var(--st-prototype); border-bottom:1px solid var(--rule);
  background:var(--st-prototype-bg); }
.pr-warns div + div { margin-top:4px; }
.pr-scopenote { display:block; margin:4px 2px 14px; font-size:12.5px; }
/* banners (repo + project) and the bell */
.bnr-wrap { margin:-8px 0 16px; }
.bnr { display:flex; flex-wrap:wrap; gap:6px 12px; align-items:center; padding:10px 14px; margin:0 0 8px;
  border:1px solid; border-radius:4px; font-size:13px; }
.bnr.chg { background:var(--blue-bg); border-color:#B9C7EC; }
.bnr.mine { background:#E9F5EC; border-color:#9CCDA9; }
.bnr.push { background:#FFF8C5; border-color:#E3D27A; }
.bnr .t { min-width:0; flex:1 1 300px; overflow-wrap:anywhere; }
.bnr .t b { font-family:var(--mono); }
.bnr .t .d { font-size:11.5px; color:var(--ink-2); margin-top:2px; }
.bnr .go { white-space:nowrap; text-decoration:none; }
.bnr .x { border:0; background:none; color:var(--ink-3); cursor:pointer; font-size:15px; padding:0 2px; }
.bnr .copyline { flex-basis:100%; }
.bnr-dlg { flex-basis:100%; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.bnr-dlg input { flex:1 1 280px; min-width:0; font:13px var(--ui); padding:4px 7px; border:1px solid var(--rule-2); }
#bell { position:relative; border:1px solid var(--rule-2); background:var(--sheet); border-radius:3px;
  padding:2px 8px; cursor:pointer; display:inline-flex; align-items:center; gap:4px; color:var(--ink-2); }
#bell svg { width:15px; height:15px; display:block; }
#bell .badge { display:inline-block; padding:0 5px; border-radius:8px; font:600 10.5px/15px var(--mono);
  background:#8a5a12; color:#fff; }
#bell .badge.hide { display:none; }
#bellPop { position:absolute; z-index:60; width:min(420px, 92vw); background:var(--sheet);
  border:1px solid var(--rule-2); box-shadow:0 8px 28px rgb(0 0 0 / .18); border-radius:4px; }
#bellPop .h { padding:8px 12px; font:600 12px var(--ui); border-bottom:1px solid var(--rule); background:#FBFCFA; }
#bellPop a.r { display:grid; grid-template-columns:auto minmax(0,1fr); gap:2px 10px; padding:8px 12px;
  border-top:1px solid var(--rule); color:var(--ink); }
#bellPop a.r:hover { background:var(--blue-bg); text-decoration:none; }
#bellPop a.r .k { font:600 9.5px/1 var(--ui); letter-spacing:.1em; text-transform:uppercase; padding:4px 6px;
  border-radius:2px; color:#fff; background:var(--blue); align-self:start; }
#bellPop a.r .k.rel { background:var(--st-released); }
#bellPop a.r .tt { font-size:12.5px; font-weight:600; overflow-wrap:anywhere; }
#bellPop a.r .tt .num { font-family:var(--mono); margin-right:5px; }
#bellPop a.r .m { grid-column:2; font:11px var(--mono); color:var(--ink-3); }
#bellPop a.r.other { opacity:.7; }
#bellPop .e { padding:12px; font-size:12.5px; color:var(--ink-3); }
#bellPop .f { display:block; padding:7px 12px; border-top:1px solid var(--rule); font-size:12px; }
@media (max-width: 900px) {
  .pr-grid { grid-template-columns:minmax(0,1fr); grid-template-areas:"side" "main"; gap:8px; }
  .pr-side { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:0 16px; }
  .pr-head { grid-template-columns:minmax(0,1fr); }
  .pr-act { justify-content:flex-start; max-width:none; }
  .pr-verdict { text-align:left; }
}
@media (max-width: 560px) {
  .imp-row { grid-template-columns:1fr; }
  .imp-row .disp { justify-self:start; }
  .rs-node { grid-template-columns:1fr; }
  .rs-node .ready { justify-content:flex-start; }
  .wait-row { grid-template-columns:auto minmax(0,1fr); }
  .wait-row .go { grid-column:2; justify-self:start; }
}
`;
  if (!document.getElementById('chg-css')) {
    const s = document.createElement('style');
    s.id = 'chg-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  const U = {};
  const short = (s) => String(s || '').slice(0, 10);
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const pill = (state) => el('span', 'chg-pill ' + (state || ''), String(state || '').replace('_', ' '));
  const itemHref = (n) => itemPath(n);
  const chgHref = (id) => PLM_ROUTES.P.change(id);
  /* `#380` — what a person calls a pull request (CHG-380 is the legacy label) */
  const prRef = PLM_ROUTES.ref;
  const rsHref = (id) => PLM_ROUTES.P.releaseSet(id);

  /* A link/command the human copies onto another device: on its own line,
   * with a Copy button (the drawing-office rule for anything typed elsewhere). */
  function copyLine(text) {
    const row = el('div', 'copyline');
    const c = el('code', null, text);
    const b = el('button', 'btn tiny', 'Copy');
    b.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
        .then(() => toast('copied')).catch(() => {
          const r = document.createRange(); r.selectNodeContents(c);
          const s = getSelection(); s.removeAllRanges(); s.addRange(r);
        });
    };
    row.append(c, b);
    return row;
  }

  function installTimeline() {
    if (typeof TL_REVIEW === 'undefined' || TL_REVIEW._chg) return;
    TL_REVIEW._chg = true;
    Object.assign(TL_REVIEW, {
      'member-added': (d) => [`put p${d.proposal} ${d.item || ''} v${d.version || ''} into the pull request`,
        d.origin === 'push' ? 'by `git push -o pr=…`' : ''],
      'change-submitted': (d) => [`${d.resubmit ? 're-submitted' : 'submitted'} the pull request `
        + `(${plural((d.proposals || []).length, 'proposal')}) — in front of a human`, ''],
      'left-review': (d) => ['pushed during review — the pull request LEFT review', d.why || ''],
      merged: (d) => [`merged the pull request${d.by_policy ? ' (by policy — AUTO)' : ''}`,
        d.order ? d.order.map(n => `${n} ${(d.tags || {})[n] || ''}`).join(' → ') : ''],
      repinned: (d) => [`re-pinned ${d.path}: ${short(d.from)} (draft) → ${short(d.to)} = ${d.child} ${d.tag}`,
        'the child merged first in the same decision; the parent\'s release commit says so'],
      disposition: (d) => [`${d.confirmed ? 'recorded' : 'drafted'} a disposition for ${d.parent}: `
        + `${String(d.disposition || '').replace(/_/g, ' ')}`, d.note || ''],
      partial: (d) => ['merge STOPPED part-way (resumable)', d.error || ''],
    });
    const prevO = TL_REVIEW.opened;
    TL_REVIEW.opened = (d, e) => (d.change && !d.ref)
      ? [d.auto ? `opened a pull request of one around p${d.proposal}` : 'opened the pull request', d.title || '']
      : prevO(d, e);
    const prev = TL_REVIEW.reviewed;
    TL_REVIEW.reviewed = (d, e) => d.heads
      ? [`${d.verdict === 'approve' ? 'approved' : d.verdict === 'reject' ? 'rejected'
          : 'commented on'} the pull request${d.counts ? '' : ' — advisory'}`
         + ` at ${Object.keys(d.heads).length} heads`, d.body || '']
      : prev(d, e);
    const prevD = TL_REVIEW.dismissed;
    TL_REVIEW.dismissed = (d, e) => d.reviews && d.why && d.proposal
      ? [`dismissed ${plural(d.reviews.length, 'pull request signoff')}`, d.why] : prevD(d, e);
    // a check on the PULL REQUEST finished (§2.10): the bot's line, GitHub's
    // way — "physical-diff passed · 4 meshes changed", the first image inline
    const prevC = TL_REVIEW.checks;
    TL_REVIEW.checks = (d, e) => {
      if (!d.check) return prevC(d, e);
      const word = { pass: 'passed', warn: 'warned', fail: 'failed', skipped: 'skipped', error: 'errored' }[d.status] || d.status;
      const img = d.image && d.image.image_url;
      let det = '';
      if (img) {
        det = el('a', 'tl-img');
        det.href = PLM_ROUTES.P.change(d.change, 'checks');
        const im = el('img'); im.src = img; im.alt = d.image.alt || d.check; im.loading = 'lazy';
        det.appendChild(im);
      }
      return [`${d.check} ${word} · ${d.summary || ''}`, det];
    };
  }

  /* ======================================================== THE CHANGE PAGE
   * Geoff, 2026-10-16: "this page is so cluttered it should look like a github
   * pull request". So it IS one: a header (title, state, who wants to merge
   * what into main, the policy; Approve/Refuse only for somebody the decision
   * would admit, with the pre-flight verdict beside them), GitHub's four tabs
   * as real paths (/changes/CHG-n[/commits|/checks|/files]), and a sidebar.
   * One thing per place:
   *   Conversation  the body, a COMPACT checks line per member, the affected
   *                 items (collapsed, with dispositions), threads, the timeline
   *                 and the ONE merge box at the bottom.
   *   Commits       per member, base..head, each linking to its compare.
   *   Checks        the full board per member (web/checks.js), red/amber/green.
   *   Files changed per member repo, one row per changed file in the FORMS its
   *                 type registers (web/diffwidgets.js) — a mesh opens on the
   *                 3-D overlay of base and head.
   * `/<N>/pull/<n>[/<tab>]` is the same page scoped to member <N>.          */
  const TAB_ORDER = ['conversation', 'commits', 'checks', 'files'];
  const STATE_WORD = { draft: 'Draft', open: 'Open', approved: 'Merged', merged: 'Merged',
                       rejected: 'Refused', withdrawn: 'Closed' };
  const branch = (r) => String(r || '').replace('refs/heads/', '');

  U.renderChange = async function (spec, flash, opts = {}) {
    if (window.REVIEW_UI) REVIEW_UI.end();
    if (typeof closeViewer === 'function') closeViewer();
    installTimeline();
    const raw = String(spec || '').split(/[/?]/)[0];
    const cid = +PLM_ROUTES.chgId(raw) || 0;
    const tab = TAB_ORDER.includes(opts.tab) ? opts.tab : 'conversation';
    const page = $('page');
    page.innerHTML = '';
    const key = 'CHG-' + cid + '/' + tab + '/' + (opts.member || '');
    CURRENT_ITEM = key;
    const crumbs = el('div', 'crumbs');
    const back = el('a', null, '← Pull requests'); back.href = PLM_ROUTES.PULLS;
    crumbs.append(back, document.createTextNode(`  /  pull request  ${prRef(cid)}`));
    if (opts.member) crumbs.append(document.createTextNode(`  /  ${opts.member}`));
    page.appendChild(crumbs);
    if (!cid) { page.appendChild(el('div', 'empty', 'A pull request is /pulls/<n>.')); return; }
    await whoami();
    let v;
    try { v = await api(`/api/changes/CHG-${cid}?actor=${encodeURIComponent(actorName())}`); }
    catch (e) { page.appendChild(el('div', 'empty', `Could not read pull request ${prRef(cid)}: ${e.message}`)); return; }
    if (CURRENT_ITEM !== key) return;
    const C = v.change, M = v.members;
    pageTitle(tab === 'conversation' ? '' : PLM_ROUTES.TAB_LABEL[tab], PLM_ROUTES.titleRef(cid), C.title);
    if (typeof refreshApprovalCount === 'function') refreshApprovalCount();
    const P = { cid, tab, member: opts.member || '', n: opts.n || '' };
    P.href = (t) => P.member ? PLM_ROUTES.P.pull(P.member, P.n || cid, t) : PLM_ROUTES.P.change(cid, t);

    // THE REVIEW CONTEXT over N members (stage 14's LENS) — unchanged: the
    // threads, a diff line, a redline row and a mesh point all find their member
    if (window.REVIEW_UI) {
      const members = {};
      for (const m of M) members[m.item] = { pid: m.proposal, head: m.head_sha, base: m.review_base_sha || m.base_sha };
      await REVIEW_UI.begin({ scope: [['change', cid], ...M.map(m => ['proposal', m.proposal])],
        members, number: '', head: '', base: '',
        canApply: ['draft', 'rejected'].includes(C.state),
        subjectFor: () => ({ type: 'change', id: cid }) });
    }

    page.appendChild(prHeader(v, P));
    if (flash) page.appendChild(flash);
    const counts = {};
    page.appendChild(prTabs(v, P, counts));
    page.appendChild(prDesign(v, P));
    const grid = el('div', 'pr-grid');
    const side = el('aside', 'pr-side');
    const main = el('div', 'pr-main');
    grid.append(side, main);
    page.appendChild(grid);
    prSidebar(side, v, P);
    if (tab === 'conversation') prConversation(main, v, P);
    else if (tab === 'commits') prCommits(main, v, P);
    else if (tab === 'checks') prChecks(main, v, P);
    else prFiles(main, v, P);
    fillCounts(M, counts);
    if (location.hash === '#merge') {
      const mb = $('mergebox');
      if (mb) setTimeout(() => {
        mb.scrollIntoView({ block: 'center' });
        const ta = mb.querySelector('textarea'); if (ta) ta.focus();
      }, 50);
    }
  };

  /* ---- header ---------------------------------------------------------------- */
  function verdictNode(v) {
    const C = v.change, pf = v.preflight || { problems: [] };
    if (!['open', 'draft', 'rejected'].includes(C.state)) return null;
    // a draft's pre-flight always says "not in review" — that is its state, not a finding
    const probs = C.state === 'open' ? (pf.problems || []) : [];
    const n = el('span', 'pr-verdict ' + (probs.length ? 'bad' : C.state === 'open' ? 'ok' : 'idle'));
    if (probs.length) {
      n.textContent = `✕ ${plural(probs.length, 'problem')} ${probs.length === 1 ? 'blocks' : 'block'} the merge — ${String(probs[0].why).split('\n')[0]}`;
      n.title = probs.map(p => `${p.member}: ${p.why}`).join('\n\n') + '\n\nDetails: the merge box, Conversation tab.';
    } else if (C.state === 'open') {
      n.textContent = '✓ Mergeable — pre-flight passes';
      n.title = pf.means || 'nothing stops the approval';
    } else n.textContent = C.state === 'draft' ? 'Not submitted yet' : 'Refused — waiting for a push';
    return n;
  }

  function prHeader(v, P) {
    const C = v.change, M = v.members, V = v.viewer || {};
    const box = el('div', 'pr-head');
    const t = el('h1', 'pr-title');
    t.append(document.createTextNode(C.title + ' '), el('span', 'pr-num', prRef(C.id)));
    box.appendChild(t);
    const row = el('div', 'pr-meta');
    const st = C.state === 'open' && (v.reviews || []).some(r => !r.dismissed_at) ? 'in review' : (STATE_WORD[C.state] || C.state);
    row.appendChild(el('span', 'pr-state ' + C.state, st));
    const who = el('span', 'pr-who');
    who.append(el('b', null, C.author || 'somebody'),
      document.createTextNode(` ${['merged', 'approved'].includes(C.state) ? 'merged' : 'wants to merge'} ${plural(M.length, 'member')} (`));
    M.forEach((m, i) => {
      if (i) who.appendChild(document.createTextNode(', '));
      const a = el('a', null, `${m.item} v${m.version}`); a.href = PLM_ROUTES.P.pull(m.item, P.cid, P.tab);
      a.title = `this pull request, scoped to ${m.item}`; who.appendChild(a);
    });
    who.appendChild(document.createTextNode(') into '));
    who.appendChild(el('code', null, 'main'));
    row.appendChild(who);
    const meta = el('span', 'pr-dim');
    meta.append(document.createTextNode('opened '), document.createTextNode(fmtAgo(C.submitted_at || C.created_at)));
    meta.title = fmtExact(C.submitted_at || C.created_at);
    row.appendChild(meta);
    if (C.project) { const a = el('a', 'pr-dim', C.project); a.href = PLM_ROUTES.P.project(C.project); row.appendChild(a); }
    const pol = [...new Set(M.map(m => (m.policy || {}).label).filter(Boolean))].join(' · ');
    if (pol) { const p = el('span', 'pr-policy', pol); p.title = (v.policy || []).join('\n'); row.appendChild(p); }
    if (C.auto) { const a = el('span', 'pr-dim', 'pull request of one'); a.title = 'A proposal submitted on its own is wrapped in a pull request of one.'; row.appendChild(a); }
    box.appendChild(row);
    // right: the decision, ONLY for somebody the decision would admit
    const act = el('div', 'pr-act');
    const vd = verdictNode(v);
    if (C.state === 'open' && V.can_decide) {
      const conv = P.href('conversation') + '#merge';
      const ok = el('a', 'btn primary', M.length > 1 ? `Approve all ${M.length}…` : 'Approve…');
      ok.href = conv; ok.title = 'The merge box: read what will happen, leave the reason, approve';
      const no = el('a', 'btn', 'Refuse…'); no.href = conv;
      act.append(ok, no);
    } else if (C.state === 'open') {
      act.appendChild(el('span', 'pr-wait', `Waiting on ${(V.waiting_on || []).join(', ') || 'an approver'}`));
      if (V.why_not) act.lastChild.title = V.why_not;
    } else if (['merged', 'approved'].includes(C.state) && C.decided_by) {
      act.appendChild(el('span', 'pr-wait', `merged by ${C.decided_by} · ${fmtAgo(C.decided_at)}`));
    }
    if (vd) act.appendChild(vd);
    box.appendChild(act);
    return box;
  }

  /* ---- the four tabs ---------------------------------------------------------- */
  function prTabs(v, P, counts) {
    const nav = el('nav', 'pr-tabs');
    const ck = v.members.reduce((n, m) => n + Object.values((m.checks || {}).counts || {}).reduce((a, b) => a + b, 0), 0);
    const worst = ['red', 'pending', 'amber', 'green'].find(s => v.members.some(m => (m.checks || {}).state === s)) || '';
    const thr = (v.threads || {}).open || 0;
    const defs = {
      conversation: { n: thr ? String(thr) : '', title: 'threads open' },
      commits: { n: '…' }, checks: { n: String(ck), cls: worst }, files: { n: '…' },
    };
    for (const t of TAB_ORDER) {
      const a = el('a', 'pr-tab' + (t === P.tab ? ' on' : ''));
      a.href = P.href(t);
      a.appendChild(document.createTextNode(PLM_ROUTES.TAB_LABEL[t]));
      const c = el('span', 'pr-count ' + (defs[t].cls || ''), defs[t].n);
      if (!defs[t].n) c.classList.add('hide');
      counts[t] = c;
      a.appendChild(c);
      if (t === P.tab) a.setAttribute('aria-current', 'page');
      nav.appendChild(a);
    }
    if (P.member) {
      const all = el('a', 'pr-scope', `scoped to ${P.member} · show the whole pull request`);
      all.href = PLM_ROUTES.P.change(P.cid, P.tab);
      nav.appendChild(all);
    }
    return nav;
  }

  async function fillCounts(M, counts) {
    let files = 0, commits = 0, ok = 0;
    const res = await Promise.all(M.map(m => memberCompare(m).catch(() => null)));
    for (const cmp of res) {
      if (!cmp) continue;
      ok++;
      files += (cmp.files || []).length;
      commits += (cmp.commits || []).length;
    }
    for (const [k, n] of [['files', files], ['commits', commits]]) {
      if (!counts[k]) continue;
      counts[k].textContent = ok ? String(n) : '—';
      counts[k].classList.remove('hide');
    }
  }

  const CMP = new Map();
  function memberRefs(m) {
    const merged = ['merged', 'approved'].includes(m.state);
    const head = merged ? (m.merged_tag || m.merged_sha || m.head_sha) : m.head_sha;
    // stage 19: the REVIEW BASELINE (the draft/tag this head descends from),
    // not the merge target — a never-released item has no main to diff with
    const base = m.review_base_sha || m.base_sha || '';
    return { base, head };
  }
  function memberCompare(m) {
    const { base, head } = memberRefs(m);
    if (!head) return Promise.reject(new Error('no head'));
    const key = `${m.item}|${base}|${head}`;
    if (!CMP.has(key)) {
      CMP.set(key, base ? repoGet(m.item, 'compare',
        `base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}`)
        : Promise.reject(new Error('first revision — nothing to compare against')));
    }
    return CMP.get(key);
  }
  /* What the page KNOWS about a member's two sides (§2.9 "Sides and colours"):
   * the review baseline (old) and the member head (new). PLM_DIFF.sides fills
   * whatever is missing — the version a baseline sha carries — from the
   * compare, and formats both through the one sideLabel. */
  function memberSides(m) {
    const rb = m.review_base || {};
    const merged = ['merged', 'approved'].includes(m.state);
    return {
      old: { ref: rb.ref || '', sha: rb.sha || m.review_base_sha || m.base_sha || '',
             version: rb.version || '' },
      new: { ref: (merged && m.merged_tag) || m.head_ref || '',
             sha: (merged && m.merged_sha) || m.head_sha || '', version: m.version || '' },
    };
  }

  // Reuse the immutable repo tree: it includes unchanged files and BOM pins.
  // PR membership alone does not establish a parent/child hierarchy.
  function prDesign(v, P) {
    const box = el('section', 'pr-card pr-design');
    box.setAttribute('aria-label', 'Complete design');
    const h = el('div', 'pr-cardh');
    h.appendChild(el('b', null, 'Complete design'));
    box.appendChild(h);
    const pad = el('div', 'pad');
    const assemblies = v.members.filter(m => m.kind === 'assembly');
    pad.appendChild(el('div', 'pr-dim', assemblies.length
      ? 'Open an assembly to inspect its complete file tree and pinned parts, including unchanged files. Open a file in the tree to preview that snapshot.'
      : 'Browse each member’s complete file tree, including unchanged files. This pull request has no assembly member.'));
    if (P.member) {
      const all = el('a', 'pr-small', 'Show the whole pull request');
      all.href = PLM_ROUTES.P.change(P.cid, P.tab);
      pad.appendChild(all);
    }
    const row = (m, assembly) => {
      const r = el('div', 'pr-design-row');
      const name = el('div');
      name.appendChild(el('b', null, `${m.item} · v${m.version}`));
      if (m.name) name.appendChild(el('div', 'pr-dim', m.name));
      const { head } = memberRefs(m);
      if (head) {
        const a = el('a', 'btn' + (assembly ? ' primary' : ''), assembly ? 'Browse complete assembly →' : 'Browse complete tree →');
        a.href = treePath(m.item, head);
        a.title = `Complete snapshot at ${head}`;
        r.append(name, a);
      } else r.append(name, el('span', 'pr-dim', 'No snapshot available'));
      return r;
    };
    for (const m of assemblies) pad.appendChild(row(m, true));
    const others = v.members.filter(m => m.kind !== 'assembly');
    if (others.length) {
      const details = el('details');
      details.open = !assemblies.length;
      details.appendChild(el('summary', 'pr-sech', `${assemblies.length ? 'Other pull request members' : 'Pull request members'} · ${others.length}`));
      for (const m of others) details.appendChild(row(m, false));
      pad.appendChild(details);
    }
    box.appendChild(pad);
    return box;
  }

  /* ---- the sidebar --------------------------------------------------------------- */
  function prSidebar(side, v, P) {
    const C = v.change, M = v.members, V = v.viewer || {};
    const sec = (title) => { const s = el('div', 'pr-sec'); s.appendChild(el('div', 'pr-sech', title)); side.appendChild(s); return s; };
    const ap = sec('Approvers');
    for (const u of V.waiting_on || []) {
      const r = el('div', 'pr-sr');
      const signed = (v.reviews || []).find(x => x.reviewer === u && !x.dismissed_at);
      r.append(el('span', 'pr-av', u.slice(0, 1).toUpperCase()), el('span', null, u),
               el('span', 'pr-dim', signed ? ({ approve: '✓ approved', reject: '✕ refused', comment: 'commented' }[signed.verdict] || '') : 'awaiting'));
      ap.appendChild(r);
    }
    for (const rule of v.policy || []) ap.appendChild(el('div', 'pr-dim pr-small', rule));
    if (C.state === 'open') ap.appendChild(el('div', 'pr-small ' + (V.can_decide ? 'pr-you' : 'pr-dim'),
      V.can_decide ? `you (${V.actor}) may decide` : (V.actor ? `${V.actor}: ${V.why_not || 'may not decide'}` : V.why_not || '')));
    const ms = sec(`Members · ${M.length}`);
    for (const m of M) {
      const r = el('div', 'pr-mem' + (P.member === m.item ? ' on' : ''));
      const a = el('a', 'num', m.item); a.href = PLM_ROUTES.P.pull(m.item, P.cid, P.tab);
      a.title = 'The pull request, scoped to this member';
      const pp = el('a', 'pr-dim', `p${m.proposal}`); pp.href = PLM_ROUTES.P.pullP(m.item, m.proposal);
      pp.title = 'This member\'s own proposal (its rounds and reviews)';
      r.append(el('span', 'ord', String((m.order ?? 0) + 1)), a, pill(m.state), document.createTextNode(' '), pp);
      r.appendChild(el('div', 'nm', `${m.name || ''} · v${m.version}`));
      if ((m.repin || []).length) r.appendChild(el('div', 'pr-dim pr-small', `re-pins ${m.repin.map(x => x.child).join(', ')}`));
      ms.appendChild(r);
    }
    if (M.length > 1) ms.appendChild(el('div', 'pr-dim pr-small', 'merged in this order — children first'));
    const rs = sec('Release set');
    rs.appendChild(el('div', 'pr-dim pr-small', '…'));
    api('/api/release-sets?pending=0&limit=200').then(d => {
      const mine = new Set(M.map(m => `${m.item}@${m.version}`));
      const hit = (d.release_sets || []).find(s => (s.items || []).some(i => mine.has(`${i.item}@${i.version}`)));
      rs.lastChild.remove();
      if (hit) { const a = el('a', null, `${hit.number} — ${hit.root_number} v${hit.root_version} (${hit.state})`); a.href = rsHref(hit.id); rs.appendChild(a); }
      else rs.appendChild(el('div', 'pr-dim pr-small', 'none — releasing is a separate decision, after this merges'));
    }).catch(() => { rs.lastChild.textContent = '—'; });
    if (C.project) { const p = sec('Project'); const a = el('a', null, C.project); a.href = PLM_ROUTES.P.project(C.project); p.appendChild(a); }
    if (C.state === 'open') {
      const w = sec('Waiting on');
      w.appendChild(el('div', null, (v.preflight || {}).problems && v.preflight.problems.length
        ? `the author — ${plural(v.preflight.problems.length, 'pre-flight problem')} to fix`
        : (V.waiting_on || []).join(', ') || 'an approver'));
    }
  }

  /* ---- The completed assembly, first ----------------------------------------------
   * Geoff: "can a pull request by default show the visual diff of the completed
   * assembly as one of the first cards". The server picks the picture
   * (`checks.hero_tiles`: each root member's primary-mesh tile); this only draws
   * it. Queued/running → a slim placeholder; skipped or nothing → no card. */
  function heroCards(v, P) {
    const own = (v.checks || {}).checks || [];
    const pd = own.find(c => c.name === 'physical-diff');
    const heroes = (v.checks || {}).hero || [];
    if (!heroes.length) {
      if (pd && (pd.status === 'queued' || pd.status === 'running')) {
        const w = el('div', 'pr-card pr-hero');
        w.appendChild(el('div', 'pad pr-dim pr-small', 'rendering the visual diff…'));
        return [w];
      }
      return [];
    }
    return heroes.map(h => {
      const m = v.members.find(x => x.item === h.item) || {};
      const S = PLM_DIFF.sides({ sides: memberSides(m) });
      const card = el('div', 'pr-card pr-hero');
      const hd = el('div', 'pr-cardh');
      const asm = m.kind === 'assembly';
      const vs = (S.old.version && S.new.version) ? ` · v${S.old.version} → v${S.new.version}` : '';
      hd.append(el('b', null, (asm ? 'Completed assembly' : 'Visual diff') + ' · '),
                Object.assign(el('a', 'num', h.item), { href: itemHref(h.item) }),
                document.createTextNode(vs));
      const all = el('a', 'pr-right', 'all tiles →'); all.href = P.href('checks'); hd.appendChild(all);
      card.appendChild(hd);
      const img = el('img', 'pr-heroimg');
      img.src = h.image_url; img.alt = h.label; img.loading = 'lazy';
      img.title = 'click to enlarge';
      img.onclick = () => img.classList.toggle('big');
      card.appendChild(img);
      const ft = el('div', 'pad pr-herofoot');
      ft.appendChild(PLM_DIFF.legend({ sides: memberSides(m) }, {}, { changes: h.mode === 'diff' }));
      if (h.delta) ft.appendChild(el('span', 'pr-dim', h.delta));   // the check's words (checks._what)
      const { base, head } = memberRefs(m);
      if (h.compare_mesh && base && head) {
        const a = el('a', 'pr-right', 'open in 3D ⇄');
        a.href = cmpPathQ(h.item, base, head, { mesh: h.compare_mesh });
        ft.appendChild(a);
      }
      card.appendChild(ft);
      return card;
    });
  }

  /* ---- Conversation --------------------------------------------------------------- */
  function prConversation(main, v, P) {
    const C = v.change, M = v.members;
    const body = el('div', 'pr-card pr-body');
    const bh = el('div', 'pr-cardh');
    bh.append(el('b', null, C.author || 'somebody'), document.createTextNode(' opened this pull request '),
              when(C.created_at));
    body.appendChild(bh);
    const md = el('div', 'mddoc pad');
    if (C.body_md) md.innerHTML = mdToHtml(C.body_md); else md.appendChild(el('span', 'dim', 'No description.'));
    body.appendChild(md);
    main.appendChild(body);

    for (const card of heroCards(v, P)) main.appendChild(card);

    // checks: ONE line per member — the board is the Checks tab
    const ck = el('div', 'pr-card');
    const ch = el('div', 'pr-cardh');
    ch.append(el('b', null, 'Checks'));
    const all = el('a', 'pr-right', 'details →'); all.href = P.href('checks'); ch.appendChild(all);
    ck.appendChild(ch);
    for (const m of M) {
      const r = el('div', 'pr-ckrow');
      const a = el('a', 'num', m.item); a.href = P.href('checks'); r.appendChild(a);
      const roll = { ...(m.checks || {}) };
      roll.counts = roll.counts || {};
      roll.required_failed = roll.required_failed || []; roll.required_missing = roll.required_missing || [];
      roll.total = Object.values(roll.counts).reduce((x, y) => x + y, 0);
      roll.target_maturity = (m.target || '').toLowerCase();
      if (window.CHECKS && roll.state) r.appendChild(CHECKS.chipNode(roll));
      if ((m.warnings || []).length) {
        const w = el('span', 'pr-dim', plural(m.warnings.length, 'warning'));
        w.title = m.warnings.join('\n\n');
        r.appendChild(w);
      }
      for (const p of m.problems || []) r.appendChild(el('span', 'mprob', '⚠ ' + p.why));
      ck.appendChild(r);
    }
    // the pull request's OWN checks (§2.10): "physical-diff — 4 meshes changed",
    // Details → the Checks tab, where its images are
    for (const c of (v.checks || {}).checks || []) {
      const r = el('div', 'pr-ckrow');
      const a = el('a', 'num', c.name); a.href = P.href('checks'); r.appendChild(a);
      if (window.CHECKS) r.appendChild(CHECKS.markNode(c.status));
      r.appendChild(el('span', 'pr-dim', c.status === 'queued' || c.status === 'running' ? `${c.status}…` : c.summary));
      const det = el('a', 'pr-right', 'Details'); det.href = P.href('checks'); r.appendChild(det);
      ck.appendChild(r);
    }
    main.appendChild(ck);

    main.appendChild(impactSection(v));
    if (window.REVIEW_UI) {
      const pan = REVIEW_UI.panel();
      const h = pan.querySelector('h3');
      if (h && h.firstChild) h.firstChild.textContent = 'Threads — the pull request and every member';
      main.appendChild(pan);
    }
    const tlBox = el('div');
    main.appendChild(tlBox);
    let showAll = false;
    const drawTl = (tl) => {
      tlBox.innerHTML = '';
      const LIM = 24;
      const cut = !showAll && tl.length > LIM;
      const card = propTimeline('', { timeline: cut ? tl.slice(-LIM) : tl });
      if (cut) {
        const more = el('button', 'btn tiny', `Show all ${tl.length} entries (${tl.length - LIM} earlier)`);
        more.style.margin = '8px 12px';
        more.onclick = () => { showAll = true; drawTl(tl); };
        card.insertBefore(more, card.children[1]);
      }
      const h = card.querySelector('h3');
      if (h && h.firstChild) h.firstChild.textContent = 'Timeline — the pull request and every member';
      tlBox.appendChild(card);
    };
    drawTl(v.timeline);
    if (window.REVIEW_UI) REVIEW_UI.on(async () => {
      if (!document.body.contains(tlBox)) return;
      try { drawTl((await api(`/api/timeline?change=CHG-${P.cid}`)).timeline); } catch (_) {}
    });
    main.appendChild(mergeBox(v, P));
  }

  /* ---- Affected items: the impact panel, collapsed ----------------------------------- */
  function impactSection(v) {
    const C = v.change;
    const rows = v.impact || [];
    const disposed = rows.filter(x => x.in_change || x.disposition).length;
    const det = el('details', 'pr-card pr-impact');
    const sm = el('summary', 'pr-cardh');
    const need = rows.length - disposed;
    sm.append(el('b', null, `Affected items (${rows.length})`),
      document.createTextNode(rows.length ? ` — ${disposed} disposed${need ? `, ${need} need${need === 1 ? 's' : ''} your call` : ''}` : ' — nothing outside the pull request pins or shares what it touches'));
    if (need) sm.appendChild(el('span', 'pr-needs', `${need} undecided`));
    det.appendChild(sm);
    if (need && rows.some(x => !x.advisory && !x.in_change && !x.disposition)) det.open = true;
    if (v.impact_coverage && v.impact_coverage.say) {
      const cv = el('div', 'pad note', v.impact_coverage.say);
      cv.title = v.impact_coverage.rule || '';
      det.appendChild(cv);
    }
    if (!rows.length) return det;
    const anyReleased = (v.members || []).some(m => m.target === 'RELEASED');
    det.appendChild(el('div', 'pad note', anyReleased
      ? 'A member targets RELEASED: every parent below needs a disposition CONFIRMED BY A HUMAN before the pull request can merge.'
      : 'Not a gate at prototype — what else the pull request reaches. A `bump` disposition is a promise: '
        + 'the pin bump must be in this pull request. Source drift is advisory.'));
    const open = ['open', 'draft', 'rejected'].includes(C.state);
    const human = ME && ((ME.kind || ME.user_kind) !== 'bot');
    for (const x of rows) {
      const r = el('div', 'imp-row');
      const s = el('div', 's');
      const a = el('a', null, x.parent); a.href = itemHref(x.parent);
      s.append(a, document.createTextNode(x.sentence.slice(x.parent.length)));
      if (x.note) s.appendChild(el('div', 'n', x.note));
      if (x.drafted_by || x.confirmed_by) {
        s.appendChild(el('div', 'who', (x.confirmed_by ? `confirmed by ${x.confirmed_by}` : '')
          + (x.drafted_by && x.drafted_by !== x.confirmed_by
            ? `${x.confirmed_by ? ' · ' : ''}drafted by ${x.drafted_by}${x.drafted_kind === 'bot' ? ' (agent)' : ''}` : '')));
      }
      r.appendChild(s);
      const d = el('span', 'disp ' + (x.in_change ? 'bump' : (x.disposition || (x.advisory ? 'no_impact' : 'none')))
        + (x.disposition && !x.confirmed ? ' drafted' : ''),
        x.in_change ? 'bumped in this pull request' : x.disposition
          ? (x.disposition_label + (x.confirmed ? '' : ' · draft')) : (x.advisory ? 'drift · no reason recorded' : 'needs a disposition'));
      r.appendChild(d);
      if (!x.in_change && open && human) {
        const ed = el('div', 'imp-edit');
        const sel = el('select', 'btn tiny');
        const opts = [['use_as_is', 'use as is'], ['no_impact', 'no impact'], ['bump', 'bump in this pull request']];
        if (x.drift || x.shares) opts.push(['unchanged_geometry', 'selected geometry unchanged'], ['historical', 'historical copy']);
        for (const [k, lbl] of opts) sel.appendChild(new Option(lbl, k));
        if (x.disposition) sel.value = x.disposition;
        const inp = el('input'); inp.placeholder = 'why (recorded)';
        if (x.note) inp.value = x.note;
        const go = el('button', 'btn tiny', x.disposition && !x.confirmed ? 'Confirm' : 'Record');
        const out = el('div'); out.style.flexBasis = '100%';
        go.onclick = async () => {
          go.disabled = true; out.innerHTML = '';
          try {
            await post(`/api/changes/${C.number}/dispositions`, decisionBody({
              parent: x.parent, disposition: sel.value, note: inp.value.trim() }));
            toast(`${x.parent}: ${sel.options[sel.selectedIndex].text}`);
            route();
          } catch (e) { go.disabled = false; out.appendChild(refusalBox(e)); }
        };
        ed.append(sel, inp, go, out);
        r.appendChild(ed);
      }
      det.appendChild(r);
    }
    return det;
  }

  /* ---- the ONE merge box ---------------------------------------------------------------- */
  function willHappen(v) {
    const M = v.members;
    const ol = el('ol', 'pr-will');
    for (const m of M) {
      const li = el('li');
      li.append(el('b', null, m.item), document.createTextNode(` merges ${branch(m.head_ref) || short(m.head_sha)} @ ${short(m.head_sha).slice(0, 7)} into main → tag `),
                el('code', null, `v${m.version}`), document.createTextNode(` (${(m.target || '').toLowerCase()})`));
      for (const r of m.repin || []) {
        const sub = el('div', 'pr-dim pr-small');
        sub.textContent = `then re-pins ${r.path}: ${r.child} draft ${short(r.from).slice(0, 7)} → ${r.child} v${(M.find(x => x.item === r.child) || {}).version || '?'} (Plm-Repin trailer)`;
        li.appendChild(sub);
      }
      ol.appendChild(li);
    }
    return ol;
  }

  function mergeBox(v, P) {
    const C = v.change, M = v.members, V = v.viewer || {};
    const card = el('div', 'pr-card pr-merge chg-dec'); card.id = 'mergebox';
    const h = el('div', 'pr-cardh');
    h.append(el('b', null, 'Merge'), el('span', 'pr-right pr-dim',
      (ME && ME.verified && ME.actor) ? `as ${ME.actor}` : `as ${actorName()}`));
    card.appendChild(h);
    const body = el('div', 'pad');
    card.appendChild(body);
    if ((v.reviews || []).length) {
      const so = el('div', 'signoffs');
      so.appendChild(el('div', 'lbl', 'Signoffs — each bound to every member\'s head'));
      for (const r of v.reviews) {
        const row = el('div', 'sorow' + (r.dismissed_at ? ' dismissed' : ''));
        row.appendChild(el('span', 'mk v-' + r.verdict, { approve: '✓', reject: '✕', comment: '💬' }[r.verdict] || '·'));
        row.appendChild(el('b', null, r.reviewer || '—'));
        row.appendChild(el('span', null, ' ' + ({ approve: 'approved', reject: 'refused', comment: 'commented' }[r.verdict] || r.verdict)));
        const chip = whoChip(r.kind); if (chip) row.appendChild(chip);
        if (!r.counts && r.verdict !== 'comment') row.appendChild(el('span', 'rv-chip adv', 'advisory'));
        row.appendChild(el('span', 'sha', '@ ' + Object.entries(r.heads || {}).map(([p, s]) => `${PLM_ROUTES.proposalLabel(p)}:${short(s).slice(0, 7)}`).join(' ')));
        if (r.dismissed_at) {
          const [pp, ss] = String(r.dismissed_by || '').split('@');   // `p12@<sha>` (`#12@` before stage 23)
          row.appendChild(el('span', 'dism', `dismissed — ${PLM_ROUTES.proposalLabel(pp.replace(/^[#p]/, ''))} moved to ${short(ss)}`));
        }
        row.appendChild(when(r.created_at));
        if (r.body_md) row.appendChild(el('div', 'b', r.body_md));
        so.appendChild(row);
      }
      body.appendChild(so);
    }
    if (['merged', 'approved', 'withdrawn'].includes(C.state)) {
      body.appendChild(mergedBox(C, C.merge_log || []));
      return card;
    }
    if (C.merge_state === 'partial') {
      const w = el('div', 'thrwarn blocks');
      w.textContent = 'PARTIALLY MERGED — pre-flight passed, then a member failed. Merged members stay merged; '
        + 'deciding again resumes with the rest, in order.';
      body.appendChild(w);
      body.appendChild(mergedBox(C, C.merge_log || [], true));
    }
    if (C.state !== 'open' && C.merge_state !== 'partial') {
      body.appendChild(el('div', 'note', C.state === 'rejected'
        ? 'Refused. The agent fixes what the threads say and pushes to the same branches — the push dismisses the '
          + 'refusal, the pull request comes back as a draft, and it is submitted again.'
        : `In draft — not in front of a human yet. The agent submits it when the work is whole `
          + `(MCP submit_change('${C.number}')).`));
      const go = el('button', 'btn tiny', 'Submit it for review now');
      const out = el('div');
      go.style.marginTop = '8px';
      go.onclick = async () => {
        go.disabled = true; out.innerHTML = '';
        try { await post(`/api/changes/${C.number}/submit`, decisionBody({})); toast(`${PLM_ROUTES.titleRef(C.id)} submitted`); route(); }
        catch (e) { go.disabled = false; out.appendChild(refusalBox(e)); }
      };
      body.append(go, out);
      return card;
    }
    const pf = v.preflight || { ok: true, problems: [] };
    if (pf.problems.length) {
      const box = el('div', 'pf bad');
      box.appendChild(el('div', 'hd', `Pre-flight: ${plural(pf.problems.length, 'problem')} — approving now merges NOTHING and says why`));
      for (const p of pf.problems) {
        const r = el('div', 'pr');
        r.append(el('span', 'm', p.member + ': '), document.createTextNode(p.why));
        if (p.fix) r.appendChild(el('div', 'f', p.fix));
        box.appendChild(r);
      }
      body.appendChild(box);
    } else {
      body.appendChild(el('div', 'pf ok', `Pre-flight: ${M.length > 1 ? `all ${M.length} members merge` : 'the member merges'} cleanly, checks and `
        + 'gates pass.'));
    }
    body.appendChild(el('div', 'lbl', 'What will happen — one decision, children first'));
    body.appendChild(willHappen(v));
    body.appendChild(el('div', 'heads', 'You are approving these heads (a push after this page loaded is refused): '
      + M.map(m => `${m.item} ${short(m.head_sha)}`).join(' · ')));
    for (const rule of v.policy || []) {
      const pl = el('div', 'policyline'); pl.append(el('span', 'k', 'policy'), el('span', 'v', rule)); body.appendChild(pl);
    }
    if (!V.can_decide) {
      body.appendChild(el('div', 'pr-waitbox', `Waiting on ${(V.waiting_on || []).join(', ') || 'an approver'}.`
        + (V.why_not ? ` ${V.actor ? V.actor + ': ' : ''}${V.why_not}` : '')));
      return card;
    }
    const note = el('textarea');
    note.placeholder = 'The reason, for the record — it goes on every member\'s review, tag annotation and the timeline.';
    body.appendChild(note);
    const acts = el('div', 'acts');
    const ok = el('button', 'btn primary', M.length > 1 ? `Approve all ${M.length} → merge` : 'Approve → merge');
    const no = el('button', 'btn', 'Refuse');
    acts.append(ok, no);
    body.appendChild(acts);
    const out = el('div', 'out');
    body.appendChild(out);
    const run = async (decision) => {
      out.innerHTML = ''; ok.disabled = no.disabled = true;
      try {
        const r = await post(`/api/changes/${C.number}/decide`, decisionBody({
          decision, note: note.value.trim(), heads: v.heads }));
        ok.disabled = no.disabled = false;
        return r;
      } catch (e) {
        ok.disabled = no.disabled = false;
        out.appendChild(refusalBox(e));
        out.scrollIntoView({ block: 'nearest' });
        return null;
      }
    };
    ok.onclick = async () => {
      const r = await run('approved');
      if (!r) return;
      U.renderChange(C.number, mergedBox(r.change, r.merged || r.change.merge_log || [], false, true),
                     { tab: 'conversation', member: P.member, n: P.n });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    };
    no.onclick = async () => {
      if (!note.value.trim()) { note.focus(); note.placeholder = 'Say why — the agent reads this and the threads.'; return; }
      const r = await run('rejected');
      if (r) { toast(`${PLM_ROUTES.titleRef(C.id)} refused — the next push re-opens it`); route(); }
    };
    return card;
  }

  /* ---- a member's group header, shared by Commits / Checks / Files ------------------- */
  function groupHead(m, extra) {
    const h = el('div', 'pr-gh');
    const a = el('a', 'num', m.item); a.href = itemHref(m.item);
    a.onclick = (e) => e.stopPropagation();
    h.append(a, el('span', 'nm', m.name || ''), pill(m.state));
    const { base, head } = memberRefs(m);
    const rng = el('span', 'rng');
    // ■ old v0.4.0 → ■ new v0.4.1 — the ref @ commit is the tooltip; once the
    // compare is in, the old side learns its version from the tree
    const draw = (cmp) => {
      const S = PLM_DIFF.sides({ sides: memberSides(m), cmp });
      rng.innerHTML = '';
      rng.append(PLM_DIFF.sideTag(S.old), document.createTextNode(' → '), PLM_DIFF.sideTag(S.new));
      rng.title = [PLM_DIFF.sideLabel(S.old, 'full'), PLM_DIFF.sideLabel(S.new, 'full'),
                   (m.review_base || {}).say || ''].filter(Boolean).join('\n');
    };
    draw(null);
    if (head && (m.review_base_sha || m.base_sha)) memberCompare(m).then(draw).catch(() => {});
    h.appendChild(rng);
    if (extra) h.appendChild(extra);
    if (base && head) {
      const c = el('a', 'pr-right', 'compare ⇄'); c.href = cmpPath(m.item, base, head);
      c.onclick = (e) => e.stopPropagation();
      h.appendChild(c);
    }
    return h;
  }
  const scoped = (P, m) => !P.member || P.member === m.item;

  /* ---- Commits ----------------------------------------------------------------------- */
  function prCommits(main, v, P) {
    for (const m of v.members) {
      if (!scoped(P, m)) continue;
      const card = el('div', 'pr-card');
      card.appendChild(groupHead(m));
      const list = el('div');
      list.appendChild(el('div', 'empty', 'reading the range…'));
      card.appendChild(list);
      main.appendChild(card);
      memberCompare(m).then(cmp => {
        list.innerHTML = '';
        if (!(cmp.commits || []).length) { list.appendChild(el('div', 'empty', 'No commits between the baseline and the head.')); return; }
        const { base } = memberRefs(m);
        for (const c of cmp.commits) {
          const row = el('div', 'cmtrow');
          const sh = el('a', 'sha', c.short); sh.href = treePath(m.item, c.sha); sh.title = 'the tree at this commit';
          row.appendChild(sh);
          const sub = el('a', 'sub', c.subject); sub.href = cmpPath(m.item, base, c.sha);
          sub.title = 'compare the baseline with this commit';
          row.appendChild(sub);
          const tr = el('div', 'tr');
          if (c.author) tr.appendChild(el('span', null, c.author));
          tr.appendChild(when(isoAt(c.date)));
          row.appendChild(tr);
          list.appendChild(row);
        }
      }).catch(e => { list.innerHTML = ''; list.appendChild(el('div', 'empty', firstRevision(m, e))); });
    }
    scopeNote(main, v, P);
  }
  const firstRevision = (m, e) => (m.review_base_sha || m.base_sha)
    ? `Could not read the range: ${e.message}`
    : `${m.item} v${m.version} is its first revision — every file is new. Open the item to read them.`;
  function scopeNote(main, v, P) {
    if (!P.member || v.members.length < 2) return;
    const a = el('a', 'pr-scopenote', `Showing ${P.member} only — show all ${v.members.length} members`);
    a.href = PLM_ROUTES.P.change(P.cid, P.tab);
    main.appendChild(a);
  }

  /* ---- Checks --------------------------------------------------------------------------- */
  function prChecks(main, v, P) {
    // the pull request's own checks first — physical-diff and its images —
    // through the same board as a commit's (subject CHG-n, keyed by the head set)
    const own = v.checks || {};
    if (!P.member && window.CHECKS && own.sha) {
      const card = el('div', 'pr-card');
      const gh = el('div', 'pr-gh');
      gh.append(el('span', 'num', prRef(P.cid)), el('span', 'nm', 'this pull request — every member at its head'));
      card.appendChild(gh);
      card.appendChild(CHECKS.card({ number: v.change.number, sha: own.sha, expand: true }));
      main.appendChild(card);
    }
    for (const m of v.members) {
      if (!scoped(P, m)) continue;
      const card = el('div', 'pr-card');
      card.appendChild(groupHead(m));
      for (const p of m.problems || []) card.appendChild(el('div', 'mprob pad', '⚠ ' + p.why));
      if (window.CHECKS && m.head_sha) {
        const { base, head } = memberRefs(m);
        card.appendChild(CHECKS.card({ number: m.item, sha: m.head_sha,
          target: (m.target || '').toLowerCase(), expand: false, base, head }));
      }
      main.appendChild(card);
    }
    scopeNote(main, v, P);
  }

  /* ---- Files changed -------------------------------------------------------------------- */
  function prFiles(main, v, P) {
    const M = v.members;
    const q = PLM_ROUTES.parseQuery(location.search);
    const sum = el('div', 'pr-filesum', 'reading every member\'s diff…');
    main.appendChild(sum);
    // the members map the Pin form links a gitlink to (another member's draft)
    const members = {};
    for (const m of M) {
      const { base, head } = memberRefs(m);
      members[m.item] = { pid: m.proposal, page: m.page, compare: base && head ? cmpPath(m.item, base, head) : '' };
    }
    const tallies = [];
    for (const m of M) {
      const det = el('details', 'pr-group');
      det.dataset.rvNumber = m.item;          // review.js's LENS: which member a line / point is
      det.dataset.rvPid = m.proposal;
      det.open = scoped(P, m);
      const sm = el('summary');
      const tally = el('span', 'pr-tally', '…');
      sm.appendChild(groupHead(m, tally));
      det.appendChild(sm);
      const body = el('div', 'pr-gbody');
      det.appendChild(body);
      main.appendChild(det);
      let built = false;
      const build = () => {
        if (built) return;
        built = true;
        body.appendChild(el('div', 'empty', 'diffing…'));
        memberCompare(m).then(cmp => {
          body.innerHTML = '';
          const rows = PLM_DIFF.rows(cmp);
          if (!rows.length) { body.appendChild(el('div', 'empty', 'No file changed between the baseline and the head.')); return; }
          if (cmp.stats.truncated) body.appendChild(el('div', 'dfnote',
            `${cmp.stats.files_changed} files changed; the first ${cmp.stats.files_reported} carry a patch.`));
          const ctx = { number: m.item, cmp, members, sides: memberSides(m), review: true };
          for (const r of rows) body.appendChild(PLM_DIFF.row(r, ctx, { q: q.member && q.member !== m.item ? {} : q }));
        }).catch(e => { body.innerHTML = ''; body.appendChild(el('div', 'empty', firstRevision(m, e))); });
      };
      if (det.open) build();
      det.addEventListener('toggle', () => { if (det.open) build(); });
      tallies.push([m, tally]);
    }
    scopeNote(main, v, P);
    // the tallies: files ± per member, and the whole change at the top
    Promise.all(tallies.map(([m, node]) => memberCompare(m).then(cmp => {
      let a = 0, d = 0, pins = 0;
      for (const f of cmp.files || []) { if (f.kind === 'submodule') pins++; else { a += f.additions || 0; d += f.deletions || 0; } }
      node.innerHTML = '';
      node.append(document.createTextNode(`${plural((cmp.files || []).length, 'file')} `),
                  el('span', 'plus', `+${a}`), document.createTextNode(' '), el('span', 'minus', `−${d}`));
      if (pins) node.appendChild(el('span', 'pinbump', plural(pins, 'pin bump')));
      return { n: (cmp.files || []).length, a, d, pins };
    }).catch(() => { node.textContent = 'no diff'; return null; }))).then(res => {
      const ok = res.filter(Boolean);
      const t = ok.reduce((x, y) => ({ n: x.n + y.n, a: x.a + y.a, d: x.d + y.d, pins: x.pins + y.pins }), { n: 0, a: 0, d: 0, pins: 0 });
      sum.innerHTML = '';
      sum.append(document.createTextNode(`${plural(t.n, 'file')} changed across ${plural(M.length, 'member')} `),
                 el('span', 'plus', `+${t.a}`), document.createTextNode(' '), el('span', 'minus', `−${t.d}`));
      if (t.pins) sum.appendChild(el('span', 'pinbump', plural(t.pins, 'pin bump')));
      sum.appendChild(el('span', 'pr-dim', ' · each member against its review baseline · a mesh opens on the 3-D overlay; '
        + 'the toggles on a row are that file\'s other forms'));
    });
  }

  function mergedBox(C, log, partial = false, fresh = false) {
    const box = el('div', 'chg-done');
    const done = log.filter(e => e.ok);
    box.appendChild(el('div', 'hd', partial ? `${done.length} merged before the stop`
      : `✓ ${fresh ? 'Approved — ' : ''}${C.number || ''} merged: ${plural(done.length, 'repo')}, children first`));
    for (const e of log) {
      const r = el('div', 'row');
      r.appendChild(el('span', e.ok ? 'ok' : '', e.ok ? '✓' : '✕'));
      const a = el('a', null, e.item); a.href = itemHref(e.item); r.appendChild(a);
      if (e.tag) { const t = el('a', null, e.tag); t.href = treePath(e.item, e.tag); r.appendChild(t); }
      if (e.merged) r.appendChild(el('span', 'dim', e.merged));
      for (const rp of e.repinned || []) {
        r.appendChild(el('span', 'repin done', `${rp.path}: ${short(rp.from)} → ${short(rp.to)} `
          + `= ${rp.child} ${(rp.on_tag || []).join(',') || rp.tag}`));
      }
      if (!e.ok) r.appendChild(el('span', 'mprob', e.error || 'failed'));
      box.appendChild(r);
    }
    return box;
  }

  /* ===================================================== THE RELEASE-SET PAGE */
  U.renderReleaseSet = async function (spec) {
    if (window.REVIEW_UI) REVIEW_UI.end();
    if (typeof closeViewer === 'function') closeViewer();
    const sid = parseInt(String(spec || '').replace(/^RS-?/i, ''), 10);
    const page = $('page');
    page.innerHTML = '';
    CURRENT_ITEM = 'RS-' + sid;
    const crumbs = el('div', 'crumbs');
    const back = el('a', null, '← Approvals'); back.href = '/approvals';
    crumbs.append(back, document.createTextNode(`  /  release set  ${sid}`));
    page.appendChild(crumbs);
    let v;
    try { v = await api(`/api/release-sets/${sid}`); }
    catch (e) { page.appendChild(el('div', 'empty', `Could not read release set ${sid}: ${e.message}`)); return; }
    if (CURRENT_ITEM !== 'RS-' + sid) return;
    await whoami();
    const S = v.release_set, M = v.members;
    pageTitle(`Release set ${sid}`, S.root_number);
    if (typeof refreshApprovalCount === 'function') refreshApprovalCount();
    // header
    const box = el('div'); box.id = 'prophead';
    const main = el('div', 'main');
    main.appendChild(el('div', 'ttl', `Release ${S.root_number} v${S.root_version}`
      + (M.length > 1 ? ` and ${plural(M.length - 1, 'unreleased child', )}`.replace('childs', 'children') : '')));
    const sub = el('div', 'sub');
    sub.appendChild(el('span', 'chg-num', `RS-${sid}`));
    sub.appendChild(pill(S.state));
    sub.appendChild(el('span', null, `${S.requested_by || 'somebody'} asks to promote ${plural(M.length, 'revision')} to RELEASED`));
    sub.appendChild(when(S.requested_at));
    if (S.decided_by) sub.appendChild(el('span', 'decided', `${S.state} by ${S.decided_by}`));
    main.appendChild(sub);
    main.appendChild(el('div', 'body', 'A release changes no files and moves no tag: each member\'s decision is a '
      + '[status] note on its release commit, its evidence frozen and hashed. Members are promoted bottom-up, '
      + 'so a parent is never released above its least-mature child.'));
    const facts = el('div', 'chg-facts');
    const ready = M.filter(m => m.progress && m.progress.checklist_ok).length;
    const f = (k, val) => { const s = el('span'); s.append(document.createTextNode(k + ' '), el('b', null, val)); facts.appendChild(s); };
    f('members', String(M.length));
    f('checklists complete', `${ready}/${M.length}`);
    f('blockers', String((S.blockers || []).length));
    if (S.manifest_sha256) f('set manifest', S.manifest_sha256.slice(0, 16) + '…');
    main.appendChild(facts);
    box.appendChild(main);
    page.appendChild(box);
    if ((S.blockers || []).length) page.appendChild(blockersCard(S));
    page.appendChild(treeCard(v));
    page.appendChild(gridCard(v));
    if (S.notes_md) {
      const nc = el('div', 'doc');
      nc.appendChild(el('h3', null, 'Release notes — generated, editable per member'));
      const pre = el('div', 'pad'); pre.style.whiteSpace = 'pre-wrap'; pre.style.fontSize = '12.5px';
      pre.textContent = S.notes_md;
      nc.appendChild(pre);
      page.appendChild(nc);
    }
    page.appendChild(setDecision(v));
  };

  function blockersCard(S) {
    const card = el('div', 'doc gatecard blocks');
    card.appendChild(el('h3', null, 'Blocked — a member is pinned at a commit, not a tag'));
    const vd = el('div', 'verdict bad');
    vd.textContent = 'There is no immutable revision to promote at an untagged commit. The pull request that holds it must '
      + 'merge first; then bump the parent\'s pin to the child\'s tag, merge that, and request the release again.';
    card.appendChild(vd);
    for (const b of S.blockers) {
      const r = el('div', 'gaterow');
      const who = el('span', 'who');
      const a = el('a', null, b.parent); a.href = itemHref(b.parent);
      who.append(a, document.createTextNode(` → ${b.number} @ ${short(b.commit)}`));
      r.appendChild(who);
      if (b.change) {
        const lk = el('a', 'btn tiny', `${PLM_ROUTES.titleRef(b.change)} must merge first →`);
        lk.href = b.change_page || chgHref(b.change);
        r.appendChild(lk);
        if (b.change_state) r.appendChild(pill(b.change_state));
      } else r.appendChild(el('span', 'dim', b.fix || 'in no pull request'));
      card.appendChild(r);
    }
    return card;
  }

  function treeCard(v) {
    const S = v.release_set, M = v.members;
    const card = el('div', 'doc');
    const h = el('h3', null, 'The closure — what will be promoted, bottom-up');
    card.appendChild(h);
    const tree = el('div', 'rs-tree');
    const byNum = {};
    for (const m of M) byNum[m.number + '@' + m.tag_sha] = m;
    const kids = {};
    for (const p of S.closure || []) (kids[p.parent] = kids[p.parent] || []).push(p);
    const blk = new Set((S.blockers || []).map(b => b.number + '@' + b.commit));
    const seen = new Set();
    const node = (number, commit, depth, pin) => {
      const m = byNum[number + '@' + commit];
      const isBlk = blk.has(number + '@' + commit);
      const released = pin && pin.released;
      const row = el('div', 'rs-node' + (isBlk ? ' blocker' : released ? ' released' : ''));
      const nm = el('div', 'nm');
      nm.appendChild(el('span', 'ind', depth ? '  '.repeat(depth - 1) + '└ ' : ''));
      const a = el('a', 'num', number); a.href = m ? m.page : itemHref(number);
      nm.append(a, document.createTextNode(' '));
      nm.appendChild(el('span', 'v', m ? `v${m.version} · ${m.from || '—'} → released`
        : isBlk ? `draft commit ${short(commit)} — on no tag`
        : pin ? (pin.version ? `v${pin.version} · ${pin.maturity || 'released'}` : short(commit)) : ''));
      nm.appendChild(document.createTextNode(' '));
      nm.appendChild(el('span', 'tag' + (m ? ' prom' : isBlk ? ' blk' : ''),
        m ? (m.state === 'approved' ? `promoted #${(m.order ?? 0) + 1}` : `promote #${(m.order ?? 0) + 1}`)
          : isBlk ? 'blocker' : released ? 'released' : '—'));
      row.appendChild(nm);
      const rd = el('div', 'ready');
      if (m) {
        const p = m.progress || {};
        const cl = p.checklist || [];
        const done = cl.filter(l => l.done).length;
        rd.appendChild(el('span', done === cl.length ? 'good' : '', `checklist ${done}/${cl.length}`));
        const cs = (p.rollup || {}).state || '—';
        rd.appendChild(el('span', cs === 'green' || cs === 'amber' ? 'good' : cs === 'red' ? 'bad' : '', `checks ${cs}`));
        if ((m.threads || {}).blocking) rd.appendChild(el('span', 'bad', `${m.threads.blocking} blocking`));
        const ev = cl.filter(l => l.evidence).map(l => l.evidence);
        if (ev.length) rd.appendChild(el('span', 'good', `coupon ${ev.join(', ')}`));
        if (m.evidence_sha256) rd.appendChild(el('span', 'good', `evidence ${m.evidence_sha256.slice(0, 10)}`));
        if (m.state === 'approved') rd.appendChild(el('span', 'good', 'RELEASED'));
      } else if (isBlk) {
        const b = (S.blockers || []).find(x => x.number === number && x.commit === commit);
        if (b && b.change) { const lk = el('a', null, `${prRef(b.change)} →`); lk.href = b.change_page; rd.appendChild(lk); }
      }
      row.appendChild(rd);
      tree.appendChild(row);
      const k = number + '@' + commit;
      if (seen.has(k)) return;
      seen.add(k);
      for (const c of kids[number] || []) {
        if (c.child === 'catalog') continue;
        node(c.child, c.commit, depth + 1, c);
      }
    };
    node(S.root_number, S.root_tag_sha, 0, null);
    card.appendChild(tree);
    return card;
  }

  function gridCard(v) {
    const S = v.release_set, M = v.members;
    const card = el('div', 'doc');
    const h = el('h3', null, 'Checklists — attest once for all that apply');
    h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
      'one statement, one row per member, your name on each'));
    card.appendChild(h);
    const wrap = el('div', 'rs-scroll');
    const tbl = el('table', 'rs-grid');
    const thead = el('tr');
    thead.appendChild(el('th', null, 'Line'));
    for (const m of M) thead.appendChild(el('th', null, m.number));
    tbl.appendChild(thead);
    const open = ['requested', 'in_review'].includes(S.state);
    for (const [line, cells] of Object.entries(v.lines || {})) {
      const tr = el('tr');
      const td = el('td');
      td.appendChild(el('div', null, line));
      const todo = cells.filter(c => !c.done);
      if (open && todo.length) {
        const bulk = el('div', 'bulk');
        const inp = el('input'); inp.placeholder = 'what you did (recorded)';
        const go = el('button', 'btn tiny', todo.length > 1 ? `Attest for all ${todo.length}` : 'Attest');
        const out = el('div'); out.style.flexBasis = '100%';
        go.onclick = async () => {
          if (!inp.value.trim()) { inp.focus(); return; }
          go.disabled = true; out.innerHTML = '';
          try {
            const r = await post(`/api/release-sets/${S.id}/attest`, decisionBody({ line, note: inp.value.trim() }));
            toast(`attested on ${(r.attested || {}).signed ? r.attested.signed.length : 0}`);
            U.renderReleaseSet(S.id);
          } catch (e) { go.disabled = false; out.appendChild(refusalBox(e)); }
        };
        bulk.append(inp, go, out);
        td.appendChild(bulk);
      }
      tr.appendChild(td);
      for (const m of M) {
        const c = cells.find(x => x.release === m.release);
        const cell = el('td', 'c ' + (c ? (c.done ? 'done' : 'todo') : ''));
        if (!c) cell.textContent = '—';
        else if (c.done) {
          cell.textContent = '✓ ' + (c.signed_by || '');
          if (c.evidence) cell.appendChild(el('span', 'ev', 'by ' + c.evidence));
        } else cell.textContent = c.by_reference && c.by_reference.length ? '○ coupon avail.' : '○';
        tr.appendChild(cell);
      }
      tbl.appendChild(tr);
    }
    wrap.appendChild(tbl);
    card.appendChild(wrap);
    return card;
  }

  function setDecision(v) {
    const S = v.release_set, M = v.members;
    const card = el('div', 'doc decide chg-dec');
    const h = el('h3', null, 'Decision — the whole set, once');
    h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
      (ME && ME.verified && ME.actor) ? `as ${ME.actor}` : `as ${ACTOR}`));
    card.appendChild(h);
    const body = el('div', 'pad');
    card.appendChild(body);
    if (S.state === 'approved') {
      const box = el('div', 'chg-done');
      box.appendChild(el('div', 'hd', `✓ Released — ${plural(M.length, 'revision')}, bottom-up`));
      for (const m of M) {
        const r = el('div', 'row');
        r.appendChild(el('span', 'ok', '✓'));
        const a = el('a', null, `${m.number} v${m.version}`); a.href = m.page; r.appendChild(a);
        r.appendChild(el('span', 'dim', `evidence ${String(m.evidence_sha256 || '').slice(0, 16)}…`));
        if (m.evidence_url) { const e = el('a', null, 'evidence'); e.href = m.evidence_url; r.appendChild(e); }
        box.appendChild(r);
      }
      body.appendChild(box);
      if (v.manifest_url) {
        body.appendChild(el('div', 'note', `Set manifest sha256 ${S.manifest_sha256}`));
        body.appendChild(copyLine(location.origin + v.manifest_url));
      }
      return card;
    }
    if (!['requested', 'in_review'].includes(S.state)) {
      body.appendChild(el('div', 'note', `This set is ${S.state}.`));
      return card;
    }
    const pf = v.preflight || { ok: true, problems: [] };
    if (pf.problems.length) {
      const box = el('div', 'pf bad');
      box.appendChild(el('div', 'hd', `Pre-flight: ${plural(pf.problems.length, 'problem')} — approving now promotes NOTHING`));
      for (const p of pf.problems) {
        const r = el('div', 'pr');
        r.append(el('span', 'm', p.member + ': '), document.createTextNode(p.why));
        if (p.fix) r.appendChild(el('div', 'f', p.fix));
        box.appendChild(r);
      }
      body.appendChild(box);
    } else {
      body.appendChild(el('div', 'pf ok', `Pre-flight: ${M.length > 1 ? `all ${M.length} members pass` : 'the member passes'} their gates. `
        + 'Approving promotes them bottom-up in one decision.'));
    }
    const rest = el('label', 'note');
    const cb = el('input'); cb.type = 'checkbox'; cb.checked = true;
    rest.append(cb, document.createTextNode(' …and I attest every checklist line still open (one row per line per member, my name on each)'));
    body.appendChild(rest);
    const note = el('textarea');
    note.placeholder = 'The reason, for the record — it goes into every member\'s evidence and note.';
    body.appendChild(note);
    const acts = el('div', 'acts');
    const ok = el('button', 'btn primary', M.length > 1 ? `Approve — release all ${M.length}` : 'Approve — release it');
    const no = el('button', 'btn', 'Reject');
    acts.append(ok, no);
    body.appendChild(acts);
    const out = el('div', 'out');
    body.appendChild(out);
    const run = async (decision) => {
      out.innerHTML = ''; ok.disabled = no.disabled = true;
      try {
        const r = await post(`/api/release-sets/${S.id}/decide`, decisionBody({
          decision, note: note.value.trim(), attest_rest: cb.checked }));
        ok.disabled = no.disabled = false; return r;
      } catch (e) {
        ok.disabled = no.disabled = false; out.appendChild(refusalBox(e));
        out.scrollIntoView({ block: 'nearest' }); return null;
      }
    };
    ok.onclick = async () => { const r = await run('approved'); if (r) { toast(`release set ${S.id} approved`); U.renderReleaseSet(S.id); } };
    no.onclick = async () => {
      if (!note.value.trim()) { note.focus(); return; }
      const r = await run('rejected'); if (r) U.renderReleaseSet(S.id);
    };
    return card;
  }

  /* ========================================== "WAITING ON YOU" + the queue */
  /* A release set waiting (the pull requests are `pullRow`s). */
  function setRow(r) {
    const row = el('div', 'wait-row');
    const href = rsHref(r.id);
    row.onclick = (e) => { if (!e.target.closest('a,button')) navigate(href); };
    row.appendChild(el('span', 'k rel', 'release'));
    const t = el('div', 't');
    const l1 = el('div', 'l1');
    l1.appendChild(el('span', 'num', r.number));
    l1.appendChild(document.createTextNode(`${r.root_number} v${r.root_version}`
      + ((r.items || []).length > 1 ? ` + ${(r.items || []).length - 1} below it` : '')));
    t.appendChild(l1);
    const l2 = el('div', 'l2');
    l2.appendChild(el('span', null, (r.items || []).map(i => i.item).join(' · ')));
    if ((r.blockers || []).length) l2.appendChild(el('span', 'warn', `blocked by ${plural(r.blockers.length, 'commit pin')}`));
    l2.appendChild(el('span', null, `by ${r.requested_by || '—'}`));
    l2.appendChild(el('span', null, fmtAgo(r.requested_at)));
    t.appendChild(l2);
    row.appendChild(t);
    const go = el('a', 'btn primary tiny go', 'Release →');
    go.href = href;
    row.appendChild(go);
    return row;
  }
  const waitRow = (r) => (r.kind === 'change' ? pullRow(r) : setRow(r));

  /* The dashboard's TOP card. Returns null on an instance with no pull
   * requests (git-first off everywhere) — the old queue then stands. */
  U.waitingCard = async function () {
    let w;
    try { w = await api(`/api/waiting?actor=${encodeURIComponent(actorName())}`); } catch (_) { return null; }
    const card = el('div', 'doc'); card.id = 'waiting';
    const n = (w.waiting || []).length;
    const h = el('h3', null, `Waiting on you${n ? ' · ' + n : ''}`);
    h.appendChild(el('span', 'right')).appendChild(el('span', 'note',
      `${plural(w.changes, 'pull request')} · ${plural(w.release_sets, 'release set')}`));
    card.appendChild(h);
    if (!n) {
      card.appendChild(el('div', 'empty', 'Nothing is waiting on you. When an agent asks you to merge a '
        + 'piece of work, or to release what was built, it lands here — one click from its page.'));
      return card;
    }
    for (const r of w.waiting.slice(0, 12)) card.appendChild(waitRow(r));
    if (n > 12) {
      const more = el('a', 'pad note', `…and ${n - 12} more on the Pull requests page →`);
      more.href = PLM_ROUTES.PULLS; more.style.display = 'block';
      card.appendChild(more);
    }
    return card;
  };

  /* ===================================================== THE LIST OF PULL REQUESTS
   * Geoff: "shouldn't we be able to see pull requests from the part homepage".
   * GitHub's list, ONE renderer, three scopes: the global list (/pulls — the
   * Approvals page), an item's Pull requests tab (/<N>/pulls) and a project
   * page. Every row is the server's `pending.change_entry` and says whether it
   * is `open` (plm/changes.py OPEN — the one definition; a refused one waits
   * for a push). The item and project scopes read the banners' endpoint:
   * `state=open` for the banners and the tab's count, `state=all` only when
   * the list itself is drawn; the global one reads /api/changes once.  */
  const LIST_WORD = { draft: 'draft', open: 'open', rejected: 'refused', merged: 'merged',
                      approved: 'merged', withdrawn: 'closed' };
  const PULLS = new Map();
  /* `{number}` | `{project}` → the pending payload at `state` (open | all),
   * cached for the life of one page (the banners and the tab count share it). */
  U.pulls = function (where, state = 'open') {
    const k = (where.number ? 'n:' + where.number : 'p:' + where.project) + ':' + state;
    const hit = PULLS.get(k);
    if (hit && Date.now() - hit.at < 5000) return hit.p;
    const url = where.number
      ? `/api/items/${encodeURIComponent(where.number)}/pending`
      : `/api/projects/${encodeURIComponent(where.project)}/pending`;
    const p = api(`${url}?state=${state}&actor=${encodeURIComponent(actorName())}`);
    PULLS.set(k, { at: Date.now(), p });
    return p;
  };
  /* The open count for a tab badge. */
  U.openPulls = async function (where) {
    try { return ((await U.pulls(where)).changes || []).length; }
    catch (_) { return 0; }
  };

  /* ONE pull-request row — the lists, the dashboard's "Waiting on you". `c` is
   * a `pending.change_entry` row; in an item's scope it links to that member. */
  function pullRow(c, where = {}) {
    const mem = c.members || [];
    const mine = where.number ? mem.find(m => m.item === where.number) : null;
    const row = el('div', 'pl-row');
    const href = mine ? PLM_ROUTES.P.pull(where.number, c.id) : chgHref(c.id);
    row.onclick = (e) => { if (!e.target.closest('a,button')) navigate(href); };
    row.appendChild(el('span', 'pl-ic ' + (c.open ? 'open' : c.state),
      c.open ? '⇄' : c.state === 'withdrawn' ? '✕' : '✓'));
    const t = el('div', 't');
    const l1 = el('div', 'l1');
    const a = el('a', 'tt', c.title); a.href = href;
    l1.appendChild(a);
    l1.appendChild(el('span', 'chg-pill ' + c.state,
      c.state === 'open' && c.can_decide ? 'waiting on you' : LIST_WORD[c.state] || c.state));
    // the checks mark: this member's head in an item's list, every member's elsewhere
    for (const m of mine ? [mine] : mem) {
      if (!m.checks || !window.CHECKS) continue;
      const d = CHECKS.stateMark(m.checks);
      d.title = `${m.item}: checks — ${d.title}`;
      l1.appendChild(d);
    }
    t.appendChild(l1);
    const l2 = el('div', 'l2');
    l2.appendChild(el('span', 'num', c.ref));
    l2.appendChild(document.createTextNode(` opened ${fmtAgo(c.created_at || c.at)} by ${c.author || '—'}`
      + ' · ' + mem.map(m => `${m.item} v${m.version}` + (m.merged_tag ? ` → ${m.merged_tag}` : '')).join(', ')));
    if (c.pin_bumps) l2.appendChild(document.createTextNode(' · ' + plural(c.pin_bumps, 'pin bump')));
    if ((c.threads || {}).open) l2.appendChild(document.createTextNode(' · ' + plural(c.threads.open, 'open thread')
      + (c.threads.blocking ? ` (${c.threads.blocking} blocking)` : '')));
    if (['merged', 'approved'].includes(c.state) && c.decided_at) l2.appendChild(document.createTextNode(` · merged ${fmtAgo(c.decided_at)}`));
    t.appendChild(l2);
    row.appendChild(t);
    return row;
  }

  /* `rows` — or, for an item/project, the promise of them at state=all, asked
   * for here (only when the list is drawn). */
  U.pullList = function (where = {}, rows = null) {
    const card = el('div', 'doc pl');
    const h = el('h3', null, where.project ? `Pull requests · ${where.project}` : 'Pull requests');
    const sw = el('span', 'right pl-sw');
    const bOpen = el('button', 'btn tiny on', 'Open'), bClosed = el('button', 'btn tiny', 'Closed');
    sw.append(bOpen, bClosed);
    h.appendChild(sw);
    card.appendChild(h);
    const body = el('div');
    body.appendChild(el('div', 'empty', 'reading…'));
    card.appendChild(body);
    const load = rows ? Promise.resolve(rows) : U.pulls(where, 'all').then(d => d.changes || []);
    load.then(rows => {
      const open = rows.filter(c => c.open);
      const closed = rows.filter(c => !c.open);
      bOpen.textContent = `${open.length} Open`; bClosed.textContent = `${closed.length} Closed`;
      const draw = (which) => {
        bOpen.classList.toggle('on', which === open); bClosed.classList.toggle('on', which === closed);
        body.innerHTML = '';
        if (!which.length) {
          body.appendChild(el('div', 'empty', which === open
            ? (where.number ? `No open pull request has ${where.number} in it. Push a draft branch with `
               + '`-o pr=new` (or open one from the Branches tab) and it lands here.'
               : 'No open pull request. An agent opens one per piece of work and submits it; it lands here.')
            : 'Nothing closed yet.'));
          return;
        }
        for (const c of which) body.appendChild(pullRow(c, where));
      };
      bOpen.onclick = () => draw(open);
      bClosed.onclick = () => draw(closed);
      draw(open.length || !closed.length ? open : closed);
    }).catch(e => { body.innerHTML = ''; body.appendChild(el('div', 'empty', 'Could not read the pull requests: ' + e.message)); });
    return card;
  };

  /* The global list (/pulls, also /approvals): the pull requests — ONE fetch,
   * the masthead count is the in-review rows of it — then the release sets
   * waiting. A proposal outside git-first `write` (an `off`/`read` project) has
   * no pull request; those still appear, in the legacy queue app.js puts below,
   * exactly as before. */
  U.approvalsPage = async function (page) {
    let ch, rs;
    try {
      [ch, rs] = await Promise.all([api(`/api/changes?limit=300&actor=${encodeURIComponent(actorName())}`),
                                    api('/api/release-sets?pending=1')]);
    } catch (_) { return false; }
    const rows = ch.changes || [];
    setApprovalCount(rows.filter(c => c.state === 'open').length + (rs.release_sets || []).length);
    page.appendChild(U.pullList({}, rows));
    const c2 = el('div', 'doc');
    const sets = rs.release_sets || [];
    c2.appendChild(el('h3', null, `Release sets waiting${sets.length ? ' · ' + sets.length : ''}`));
    if (!sets.length) c2.appendChild(el('div', 'empty', 'No release is waiting. request_release on an assembly proposes its unreleased closure as one set.'));
    for (const s of sets) c2.appendChild(setRow(s));
    page.appendChild(c2);
    return true;
  };

  /* "Open a pull request" for a draft branch in none — ONE action, used by the
   * yellow banner and by the Branches tab's rows. `x` = {proposal, title,
   * branch, project, item, push}; the dialog is appended to `host`. */
  U.openPullButton = function (x, host) {
    const open = el('button', 'btn tiny primary go', 'Open a pull request');
    open.onclick = () => {
      if (host.querySelector('.bnr-dlg')) return;
      const dlg = el('div', 'bnr-dlg');
      const inp = el('input'); inp.value = x.title || x.branch; inp.placeholder = 'what this pull request is';
      const make = el('button', 'btn tiny primary', 'Create pull request');
      const out = el('div'); out.style.flexBasis = '100%';
      make.onclick = async () => {
        make.disabled = true; out.innerHTML = '';
        try {
          const c = await post('/api/changes', { title: inp.value.trim() || x.branch,
            project: x.project || '', actor: actorName() });
          await post(`/api/changes/${c.number}/members`, { proposal: x.proposal, actor: actorName() });
          toast(`${PLM_ROUTES.titleRef(c.id)} opened with ${x.item} — submit it when the work is whole`);
          navigate(chgHref(c.id));
        } catch (e) { make.disabled = false; out.appendChild(refusalBox(e)); }
      };
      dlg.append(inp, make, out);
      if (x.push) {
        dlg.appendChild(el('div', 'd pr-dim', 'or, from the clone, on the next push:'));
        dlg.appendChild(copyLine(x.push));
      }
      host.appendChild(dlg);
      inp.focus(); inp.select();
    };
    return open;
  };

  /* ===================================================== BANNERS AND THE BELL
   * Geoff: "on the front end for the site we should have a notification /
   * banner similar to github when i come to the core repo right?"
   *
   * The repo home (`/<N>`) and a project page get GitHub's strip: one row per
   * pending thing (U.pulls — GET /api/items/<N>/pending, /api/projects/<p>/
   * pending — plm/pending.py). A pull request waiting on YOU reads green with
   * Review; one waiting on somebody else reads blue with View; a draft branch
   * pushed into none reads yellow with "Open a pull request". Dismissed per
   * browser session, keyed by the row's last movement — a new push shows it
   * again.  */
  const DISMISS = 'plm-bnr:';
  const dismissed = (k) => { try { return sessionStorage.getItem(DISMISS + k) === '1'; } catch (_) { return false; } };
  const dismiss = (k) => { try { sessionStorage.setItem(DISMISS + k, '1'); } catch (_) {} };

  U.banners = async function (host, where) {
    let d;
    try { d = await U.pulls(where); } catch (_) { return; }
    if (!document.body.contains(host)) return;
    host.innerHTML = '';
    host.className = 'bnr-wrap';
    for (const c of d.changes || []) {
      const k = `c${c.id}:${c.updated_at}`;
      if (dismissed(k)) continue;
      const mine = c.can_decide;
      const row = el('div', 'bnr ' + (mine ? 'mine' : 'chg'));
      const t = el('div', 't');
      const scope = where.number && c.members.some(m => m.item === where.number)
        ? PLM_ROUTES.P.pull(where.number, c.id) : chgHref(c.id);
      const b = el('b', null, PLM_ROUTES.titleRef(c.id));
      t.appendChild(b);
      const items = c.members.map(m => `${m.item} v${m.version}`).join(', ');
      if (c.state === 'open') t.appendChild(document.createTextNode(mine
        ? ' is waiting for your approval'
        : ` is in review (waiting on ${(c.waiting_on || []).join(', ') || 'an approver'})`));
      else if (c.state === 'draft') t.appendChild(document.createTextNode(' is a draft — not submitted for review yet'));
      else t.appendChild(document.createTextNode(' was refused — the next push to its branches re-opens it'));
      t.appendChild(el('div', 'd', `${c.title} · ${items} · ${c.author || '—'} · ${fmtAgo(c.at)}`));
      row.appendChild(t);
      const go = el('a', 'btn tiny go' + (mine ? ' primary' : ''), mine ? 'Review' : 'View');
      go.href = scope;
      row.appendChild(go);
      row.appendChild(closer(row, k));
      host.appendChild(row);
    }
    const RECENT = 14 * 86400;
    const now = Date.now() / 1000;
    const drafts = (d.drafts || []).filter(x => now - (x.pushed_at || 0) < RECENT);
    for (const x of drafts) {
      const k = `p${x.proposal}:${x.head_sha}`;
      if (dismissed(k)) continue;
      const row = el('div', 'bnr push');
      const t = el('div', 't');
      if (!where.number) t.append(el('b', null, x.item), document.createTextNode(' '));
      t.append(el('code', null, x.branch), document.createTextNode(` had recent pushes ${fmtAgo(x.pushed_at)}`));
      t.appendChild(el('div', 'd', `${x.title || 'no summary'} · v${x.version} · ${x.author || '—'}`
        + (x.base ? ` · against ${x.base}` : '') + ' · in no pull request, so nobody has been asked'));
      row.appendChild(t);
      const cmp = el('a', 'btn tiny go', 'Compare');
      cmp.href = x.base_sha ? cmpPath(x.item, x.base_sha, x.branch) : PLM_ROUTES.P.tree(x.item, x.branch);
      row.append(cmp, U.openPullButton(x, row), closer(row, k));
      host.appendChild(row);
    }
    const older = (d.drafts || []).length - drafts.length;
    if (older && where.number) {
      const a = el('a', 'pr-dim', `${plural(older, 'older draft branch')} in no pull request — the Branches tab`);
      a.href = PLM_ROUTES.P.tab(where.number, 'drafts');
      a.style.cssText = 'display:block;margin:-2px 2px 6px;font-size:12px';
      host.appendChild(a);
    }
  };
  function closer(row, k) {
    const x = el('button', 'x', '×');
    x.title = 'Dismiss for this session — a new push shows it again';
    x.setAttribute('aria-label', 'Dismiss');
    x.onclick = () => { dismiss(k); row.remove(); };
    return x;
  }

  /* The masthead BELL: the approvals count (the badge it always had) and a
   * dropdown of what waits on YOU — pull requests and release sets — then what waits
   * on others. GET /api/waiting carries can_decide per row for the viewer. */
  function bellPop(btn) {
    const old = $('bellPop');
    if (old) { old.remove(); btn.setAttribute('aria-expanded', 'false'); return; }
    const pop = el('div'); pop.id = 'bellPop'; pop.setAttribute('role', 'menu');
    pop.appendChild(el('div', 'e', 'reading…'));
    document.body.appendChild(pop);
    const r = btn.getBoundingClientRect();
    pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
    pop.style.left = Math.max(8, Math.min(r.right + window.scrollX - pop.offsetWidth, window.innerWidth - pop.offsetWidth - 8)) + 'px';
    btn.setAttribute('aria-expanded', 'true');
    api(`/api/waiting?actor=${encodeURIComponent(actorName())}`).then(w => {
      pop.innerHTML = '';
      const rows = w.waiting || [];
      const mine = rows.filter(x => x.can_decide), rest = rows.filter(x => !x.can_decide);
      const item = (x, other) => {
        const isChg = x.kind === 'change';
        const a = el('a', 'r' + (other ? ' other' : ''));
        a.href = isChg ? chgHref(x.id) : rsHref(x.id);
        a.setAttribute('role', 'menuitem');
        a.appendChild(el('span', 'k' + (isChg ? '' : ' rel'), isChg ? 'pull request' : 'release'));
        const tt = el('span', 'tt');
        tt.append(el('span', 'num', isChg ? x.ref : x.number), document.createTextNode(isChg ? x.title
          : `${x.root_number} v${x.root_version}`));
        a.appendChild(tt);
        a.appendChild(el('span', 'm', [x.project || ((isChg ? x.members : x.items) || []).map(i => i.item).slice(0, 2).join(' · '),
          fmtAgo(x.at), other ? `waiting on ${(x.waiting_on || []).join(', ') || 'an approver'}` : '']
          .filter(Boolean).join(' · ')));
        a.onclick = () => pop.remove();
        return a;
      };
      pop.appendChild(el('div', 'h', `Waiting on you · ${mine.length}`));
      if (!mine.length) pop.appendChild(el('div', 'e', 'Nothing is waiting on you.'));
      for (const x of mine.slice(0, 10)) pop.appendChild(item(x, false));
      if (rest.length) {
        pop.appendChild(el('div', 'h', `Waiting on others · ${rest.length}`));
        for (const x of rest.slice(0, 5)) pop.appendChild(item(x, true));
      }
      const all = el('a', 'f', 'All pull requests →'); all.href = PLM_ROUTES.PULLS;
      all.onclick = () => pop.remove();
      pop.appendChild(all);
    }).catch(e => { pop.innerHTML = ''; pop.appendChild(el('div', 'e', 'Could not read: ' + e.message)); });
  }
  (function installBell() {
    const b = document.getElementById('bell');
    if (!b) return;
    b.onclick = (e) => { e.stopPropagation(); bellPop(b); };
    document.addEventListener('click', (e) => {
      const p = document.getElementById('bellPop');
      if (p && !p.contains(e.target) && e.target !== b) { p.remove(); b.setAttribute('aria-expanded', 'false'); }
    });
    document.addEventListener('keydown', (e) => {
      const p = document.getElementById('bellPop');
      if (e.key === 'Escape' && p) { p.remove(); b.setAttribute('aria-expanded', 'false'); b.focus(); }
    });
    addEventListener('popstate', () => { const p = document.getElementById('bellPop'); if (p) p.remove(); });
  })();

  window.CHANGES_UI = U;
})();
