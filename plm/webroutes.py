"""The web UI's addresses — real paths, GitHub's shape (stage 22).

Geoff, 2026-10-16: "please move to real paths". `/#/change/CHG-380` became
`/changes/CHG-380` — and, the same day, "let's just call it pull request":
`/pulls/380` (GitHub's word, GitHub's path; the old spellings redirect, see
`_LEGACY`) — `/#/item/N/compare/a...b` became `/N/compare/a...b`, and so
on — the full table is in web/routes.js, which is the UI's copy of these rules.
This module is the SERVER's copy, and it has two jobs:

1. **Which GET paths are the app.** `is_ui_path` says whether a path is a page
   of the single-page UI (so the server answers it with the shell, index.html)
   or nothing at all (a small HTML 404 — a typo must not spin up the app). A
   UI path's first segment is an item number or one of `WORDS`.
2. **Every link plm hands out.** `item`, `tree`, `compare`, `pull`, `release`,
   `change`, `release_set`, `snip`, `project` build the PATH (no origin);
   callers prefix PLM_PUBLIC_URL / PLM_WEB_BASE themselves. Notifications, the
   MCP's `say` lines, the push door's `remote:` lines and the API's `page`
   fields all go through here, so the shape changes in one place.
3. **Where an old page path went.** `legacy_redirect` (the `_LEGACY` table) —
   the server's 301, and the only copy of those rules.

A ref may contain `/` (`draft/v0.4.1`). It is written with its slashes literal
(each segment percent-encoded, the separators not), like GitHub, and read back
longest-first against the repo's ref names (`split_ref`, the same rule as
web/routes.js `splitRef`).
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote

# PREFIX-CLASS-SEQ (plm/ops.py NUMBER_RE), loosened to admit the one legacy
# two-class shape (LFD-PRT-PRT-0001). web/routes.js ITEM_RE is the same regex.
ITEM_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z]+)+-\d+$")

# The fixed first segments of a UI page. Lower-case, so never an item number.
WORDS = frozenset({"registry", "approvals", "search", "projects", "pulls",
                   "release-sets", "snip"})

#: `CHG-380`, `CHG380`, `#380` or `380` — every spelling a pull request has had.
#: THE parser (`changes.parse_id`, `change`, `legacy_redirect` all use it);
#: web/routes.js `chgId` is its twin.
LABEL_RE = re.compile(r"^\s*(?:CHG-?|#)?(\d+)\s*$", re.I)

# A pull request's tabs (GitHub's): `/pulls/n` is Conversation,
# `/pulls/n/<tab>` the others; `/<N>/pull/<n>/<tab>` the same, scoped to
# one member. web/routes.js CHANGE_TABS is the same list.
CHANGE_TABS = ("commits", "checks", "files")

# encodeURIComponent leaves A-Z a-z 0-9 - _ . ! ~ * ' ( ) — match it exactly,
# so a link built here and one built by the UI are the same string.
_SAFE = "!~*'()"


def enc(s) -> str:
    return quote(str(s), safe=_SAFE)


def segs(p: str) -> str:
    """A ref or a repo path: each segment encoded, the slashes kept."""
    return "/".join(enc(s) for s in str(p or "").split("/"))


def is_ui_path(path: str) -> bool:
    """True when `path` (no query) is a page of the web UI."""
    p = path or "/"
    if p == "/":
        return True
    first = unquote(p.lstrip("/").split("/", 1)[0])
    return first in WORDS or bool(ITEM_RE.match(first))


def split_ref(rest: str, ref_names) -> tuple[str, str]:
    """`draft/v0.4.1/cad/x.scad` → ("draft/v0.4.1", "cad/x.scad").

    Longest prefix that is a known ref wins; a first segment that decodes to
    something containing `/` (the hash era's `%2F` spelling) is the ref whole;
    no match → the first segment (a tag, `main`, a sha the caller will verify).
    """
    raw = str(rest or "").strip("/")
    if not raw:
        return "", ""
    parts = raw.split("/")
    first = unquote(parts[0])
    if "/" in first:
        return first, "/".join(unquote(x) for x in parts[1:])
    d = [unquote(x) for x in parts]
    names = set(ref_names or ())
    for k in range(len(d), 0, -1):
        cand = "/".join(d[:k])
        if cand in names:
            return cand, "/".join(d[k:])
    if not names and d[0] == "draft" and len(d) > 1:
        return "/".join(d[:2]), "/".join(d[2:])
    return d[0], "/".join(d[1:])


# -- builders: PATHS (prefix an origin yourself) ---------------------------------

def item(number: str) -> str:
    return f"/{enc(number)}"


def tab(number: str, name: str) -> str:
    return f"/{enc(number)}/{name}"


def tree(number: str, ref: str, path: str = "") -> str:
    out = f"/{enc(number)}/tree/{segs(ref)}"
    return out + (f"/{segs(path)}" if path else "")


def blob(number: str, ref: str, path: str) -> str:
    return f"/{enc(number)}/blob/{segs(ref)}/{segs(path)}"


def compare(number: str, base: str, head: str) -> str:
    return f"/{enc(number)}/compare/{segs(base)}...{segs(head)}"


def pull(number: str, pid, tab: str = "") -> str:
    """A proposal of `number` — GitHub's /pull/<n>[/files|checks|commits]."""
    return f"/{enc(number)}/pull/{enc(pid)}" + (f"/{tab}" if tab in CHANGE_TABS else "")


def release(number: str, rid=None, tag: str = "") -> str:
    """A release: by tag when it has one (GitHub's shape), else by record id."""
    if tag:
        return f"/{enc(number)}/releases/tag/{segs(tag)}"
    return f"/{enc(number)}/releases/{enc(rid)}"


#: the list of pull requests (the Approvals page, GitHub's /pulls)
PULLS = "/pulls"


def change(cid, tab: str = "") -> str:
    """A pull request (CHG-n) — Conversation, or one of its other tabs:
    `/pulls/380[/files]`. `CHG-380`, `#380` and `380` are all accepted."""
    m = LABEL_RE.match(str(cid))
    return f"{PULLS}/{enc(m.group(1) if m else cid)}" + (
        f"/{tab}" if tab in CHANGE_TABS else "")


def _from_changes(rest: list[str]) -> str:
    m = LABEL_RE.match(unquote(rest[0])) if rest and rest[0] else None
    return change(m.group(1), rest[1] if len(rest) > 1 else "") if m else PULLS


def _from_item(rest: list[str]) -> str | None:
    # `/item/<N>/…` — a hash-era link that lost its `#`, and what plm-git's
    # legacy hook writes — is `/<N>/…`
    return "/" + "/".join(rest) if rest and rest[0] else None


#: ⭐ THE LEGACY PAGE PATHS, one table: first segment → the path it moved to.
#: The server answers them with a 301 before the shell (`plm/api.py`
#: `_ShellStatic`); they are not UI paths, so an in-page link to one is a full
#: load that lands on that 301 (web/routes.js has no copy of this table).
_LEGACY = {"changes": _from_changes, "item": _from_item}


def legacy_redirect(path: str) -> str | None:
    """`/changes/CHG-380/files` → `/pulls/380/files`, `/changes` → `/pulls`,
    `/item/N/bom` → `/N/bom`. None for every other path."""
    first, *rest = (path or "").strip("/").split("/")
    fn = _LEGACY.get(first)
    return fn(rest) if fn else None


def release_set(sid) -> str:
    return f"/release-sets/{enc(sid)}"


def project(name: str) -> str:
    return f"/projects/{enc(name)}"


def snip(token: str, info: bool = False) -> str:
    return f"/snip/{enc(token)}" + ("/info" if info else "")
