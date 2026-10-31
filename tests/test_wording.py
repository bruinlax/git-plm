"""The noun is PULL REQUEST (Geoff, 2026-10-16: "if you call it 'pull' then
let's just call it pull request"), its mark is `#380`, and a proposal inside one
is `p12`. One function says each (`changes.ref` / `title_ref` / `names`,
`proposals.label`, web/routes.js `ref` / `titleRef` / `proposalLabel`); these
tests hold those functions and what their callers SAY — API payload strings,
the submit's say line, the push door's `remote:` lines, the MCP instructions
and tool descriptions — not the source text that builds them.

`change` stays where it means the physical change itself ("a design change",
"what changed"), in identifiers (`/api/changes`, `submit_change`, `-o change=`
— kept for compatibility) and in comments. Two places are still read as
SOURCE literals: the web UI's strings (only a browser renders them) and the
push hook's inline `remote:` text."""
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

from plm import changes, db, pending, proposals, threads, webroutes

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = re.compile(
    r"change request"
    r"|\b(?:a|the|this|that|one|each|every|your|open|new) change\b(?![-_=.(\w])"
    r"|\bchange of one\b|\bchanges waiting\b|\bopen a change\b"
    r"|\bCHG-\d+ (?:is|was|needs|has)\b", re.I)

#: phrases where `change` is the physical change, said on purpose
ALLOWED = (
    "a change to the product", "a change to that item", "every change either of us",
    "drives a change", "your change moved the geometry", "before your change",
    "Propose a change", "Why did this change?", "Commit exactly this change",
    "where the change should be", "YOUR change made it worse",
    "a change that\\nmeant two revisions",    # a source token: the \\n is literal
)


def _hits(strings):
    out = []
    for s in strings:
        for m in FORBIDDEN.finditer(s):
            if not any(a.lower() in s.lower() for a in ALLOWED):
                out.append(f"{m.group(0)!r} in {s[:120]!r}")
    return out


def test_the_rule_catches_what_it_should():
    assert _hits(["opened the change", "Open a change", "CHG-380 is waiting",
                  "a change request"]) and len(_hits(["opened the change"])) == 1
    assert not _hits(["what changed", "a design change", "changes.label",
                      "Pull request #380 is waiting", "-o change=CHG-1"])


# -- the functions ---------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["CHG-380", "chg380", "#380", "380", " 380 ", 380])
def test_one_parser_for_every_spelling(spelling):
    assert changes.parse_id(spelling) == 380
    assert webroutes.change(spelling) == "/pulls/380"
    assert changes.LABEL_RE is webroutes.LABEL_RE


def test_one_parser_refuses_the_rest():
    for bad in ("RS-3", "X-PRT-0001", "", "p12", "CHG-"):
        with pytest.raises(changes.ChangeError):
            changes.parse_id(bad)


def test_the_labels():
    assert changes.ref(380) == "#380"
    assert changes.title_ref(380) == "Pull request #380"
    assert changes.label(380) == "CHG-380"          # the legacy label, still `number`
    assert proposals.label(12) == "p12"
    assert changes.names(380) == {"id": 380, "number": "CHG-380", "ref": "#380",
                                  "url": "http://testserver/pulls/380",
                                  "page": "/pulls/380"}
    assert threads.subject_name("proposal", 12) == "proposal p12"
    assert threads.subject_name("change", 380) == "pull request #380"


def test_the_js_twins_say_the_same():
    node = os.environ.get("NODE") or next(
        (str(p) for p in (Path(d) / "node" for d in os.environ.get("PATH", "").split(":"))
         if p.is_file()), "")
    if not node:
        pytest.skip("node is not installed on this host")
    script = ("const R=require(process.argv[1]);console.log(JSON.stringify("
              "[R.chgId('CHG-380'),R.ref(380),R.titleRef('#380'),R.proposalLabel(12)]))")
    r = subprocess.run([node, "-e", script, str(ROOT / "web" / "routes.js")],
                       capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout) == ["380", changes.ref(380), changes.title_ref(380),
                                    proposals.label(12)]


# -- what the callers say ----------------------------------------------------------

def test_the_say_line_names_the_pull_request():
    assert changes.say(483, "open") == \
        "Pull request #483 is waiting for your approval: http://testserver/pulls/483"
    assert changes.say(483, "merged").startswith("Pull request #483 was merged by policy")
    assert changes.say(483, "open", ["an impact is undisposed"]).startswith(
        "Pull request #483 is waiting for your approval — \"just build it\" would have")
    assert changes.say(483, "draft") == "Pull request #483 is draft: http://testserver/pulls/483"


def test_the_push_lines():
    assert pending.push_line('USB "notch"', "draft/usb") == \
        "git push -o pr=new -o title=\"USB 'notch'\" origin draft/usb"
    assert pending.push_line("", "draft/x").endswith('title="draft/x" origin draft/x')
    sys.path.insert(0, str(ROOT / "plm-git"))
    try:
        from plm_git import gitfirst
    finally:
        sys.path.pop(0)
    with db.connect() as conn:
        changes.ensure(conn)
        conn.execute("INSERT OR REPLACE INTO changes (id, title, state, author, "
                     "created_at, updated_at) VALUES (951, 'USB notch', 'draft', "
                     "'wbot', 0, 0)")
        brief = changes.brief(conn, changes.row(conn, 951))
    lines = gitfirst.pr_lines(brief)
    assert lines[0] == "plm: pull request #951 (CHG-951, draft, 0 items) — USB notch"
    assert lines[1].endswith("http://testserver/pulls/951")
    assert "git push -o pr=951 …" in lines[2]
    assert gitfirst.pr_lines({}) == []
    assert not _hits(lines)
    env = {"GIT_PUSH_OPTION_COUNT": "2", "GIT_PUSH_OPTION_0": "change=CHG-3",
           "GIT_PUSH_OPTION_1": "title=x"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        assert gitfirst.push_options() == {"pr": "CHG-3", "title": "x"}
    finally:
        for k, v in old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)


def test_payload_strings(monkeypatch):
    """What the API answers: a banner row's `why_not`, a member's label, the
    refusals `add_member` raises — all say pull request #n and proposal p<n>."""
    with db.connect() as conn:
        changes.ensure(conn)
        proposals.ensure(conn)
        for cid, st in ((952, "draft"), (953, "rejected"), (954, "merged")):
            conn.execute("INSERT OR REPLACE INTO changes (id, title, state, author, "
                         "created_at, updated_at) VALUES (?, 't', ?, 'wbot', 0, 0)",
                         (cid, st))
        why = {cid: pending.change_entry(conn, changes.row(conn, cid))["why_not"]
               for cid in (952, 953, 954)}
        assert why[952].startswith("Pull request #952 is a draft")
        assert why[953].startswith("Pull request #953 was refused")
        assert why[954] == "Pull request #954 is merged"
        with pytest.raises(changes.ChangeError, match=r"^no proposal p999999$"):
            changes.add_member(conn, 952, 999999, actor="wbot")
        with pytest.raises(changes.ChangeError, match=r"^Pull request #954 is merged"):
            changes.add_member(conn, 954, 1, actor="wbot")
        assert not _hits(why.values())


def test_the_mcp_says_pull_request():
    """The MCP instructions as an agent receives them, and every tool's
    description."""
    from plm_mcp import instructions, tools_changes
    texts = [v for k, v in vars(instructions).items()
             if k.isupper() and isinstance(v, str)] + [instructions.for_instance()]
    texts += [inspect.getdoc(f) or "" for _, f in inspect.getmembers(
        tools_changes, inspect.isfunction) if not f.__name__.startswith("_")]
    bad = _hits(texts)
    assert not bad, "\n".join(bad)


# -- still read as source: the web UI's literals, the push hook's text --------------

JS = ["web/app.js", "web/changes.js", "web/checks.js", "web/review.js",
      "web/diffwidgets.js", "web/routes.js", "web/index.html"]
_JS_STR = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`")
_JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:'\"\\])//[^\n]*", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def test_ui_strings_say_pull_request():
    bad = []
    for f in JS:
        text = _JS_COMMENT.sub("", _HTML_COMMENT.sub("", (ROOT / f).read_text()))
        bad += [f"{f}: {h}" for h in _hits(_JS_STR.findall(text))]
    assert not bad, "\n".join(bad)


def test_ui_has_no_hand_written_pull_request_label():
    """`#${…}` / `Pull request #` / `proposal #` are built by PLM_ROUTES (ref,
    titleRef, proposalLabel), never by hand at a render site."""
    pat = re.compile(r"Pull request #|[Pp]roposal #\$\{|pull request #\$\{")
    bad = [f"{f}:{i}: {ln.strip()[:100]}" for f in JS if f.endswith(".js")
           for i, ln in enumerate((ROOT / f).read_text().splitlines(), 1)
           if pat.search(ln) and not ln.lstrip().startswith(("//", "*", "/*"))]
    assert not bad, "\n".join(bad)


def test_push_door_strings_say_pull_request():
    """The push hook prints most of its `remote:` text inline, inside the one
    hook function (only `pr_lines` is a function of its own, tested above) —
    so its string literals are read as source too."""
    kinds = (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1))
    text = (ROOT / "plm-git" / "plm_git" / "gitfirst.py").read_text()
    toks = [t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type in kinds]
    bad = _hits(toks)
    assert not bad, "\n".join(bad)
