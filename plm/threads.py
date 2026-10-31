"""THREADS — comments, context and markup become one thing (stage 13).

docs/PLM_REVIEW_MODEL.md §2.4, built on the August markup design
(docs/PLM_MARKUP.md, docs/PLM_MARKUP_UX.md) — with the one thing those two
documents called the hard problem dissolved by git: an anchor is
`(commit, path, locator)`, and "stale" is no longer a geometric guess. It is
`git` saying that blob (or that key, or that hunk) changed between the commit
the thread was anchored at and the head the reader is looking at.

ONE COMMENT SYSTEM. A thread's comments are `context` rows on target type
`thread` (plm/context.py's TARGETS registry — one dict entry, no second log, no
second FTS feed). What this table adds is what a comment cannot hold: the
ANCHOR, the LIFECYCLE (open | resolved, who, why, `addressed_by` — the commit
that fixed it), opt-in BLOCKING, and a SUGGESTED CHANGE.

THE ANCHORS (`ANCHORS`, a registry — add one with a dict entry):

    subject   the proposal / release itself            never outdated
    line      file + line @ sha (head or base side)    that hunk changed
    key       a plm.toml key: an attribute, a [[bom]]  that key's value changed
              line, a pin, `files.primary` …
    mesh      a point on the 3-D mesh @ sha: position  that mesh blob changed
              + normal in MODEL space (the STL's own
              mm, Z-up — never the viewer's display
              frame), triangle index as a WITNESS, and
              the camera pose for "show me what they saw"
    image     a region of an image @ sha               that image blob changed

THE RULES, each a decision recorded somewhere before this file:

* **Outdated is computed, never guessed and never written.** Nothing
  transitions a thread automatically (August: "an auto-resolve is the failure
  that makes people stop trusting the list"); the reader is told.
* **Blocking is opt-in per thread** (§2.4, superseding August's absolute "a
  markup never blocks"). Honoured by the RELEASED gates only: a proposal
  targeting RELEASED (`ops.decide_approval`) and a promotion
  (`ops.threads_gate_check`, beside the §5.1 child gate). An approver can always
  ship over it by resolving it WITH A REASON, which is recorded.
* **Bots propose, humans decide** (August, kept). A bot's thread is advisory:
  it may not be blocking, and it is stamped BOT wherever it is shown. A thread
  is resolved by the person who raised it or by an approver; anybody else —
  including the agent that fixed it — records `addressed_by` (a CLAIM) and the
  raiser decides. Applying a reviewer's own suggested change is the one
  exception: the suggestion WAS the reviewer's decision, so applying it
  verbatim resolves it, with the commit.
* **A suggested change is one commit through the existing write path**
  (`ops.git_draft` + `ops.git_write`): validated like a push, CAS on the branch,
  re-indexed — and therefore a head move like any other (stale reviews are
  dismissed by it).
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import db

ZERO = {"open": 0, "resolved": 0, "blocking": 0, "total": 0, "addressed": 0}

SUBJECTS = ("proposal", "release", "change")
#: stage 14 built the CHANGE subject (§2.5) — nothing is reserved any more
RESERVED_SUBJECTS: tuple = ()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY,
  subject_type TEXT NOT NULL,             -- proposal | release (| change, stage 14)
  subject_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items(id),
  revision_id INTEGER,
  anchor_kind TEXT NOT NULL DEFAULT 'subject',
  anchor_sha TEXT NOT NULL DEFAULT '',    -- the commit it was anchored AT
  anchor_path TEXT NOT NULL DEFAULT '',
  anchor TEXT NOT NULL DEFAULT '{}',      -- the locator (JSON, model space)
  anchor_ident TEXT NOT NULL DEFAULT '',  -- what "unchanged" is measured by
  title TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open',     -- open | resolved
  blocking INTEGER NOT NULL DEFAULT 0,
  author TEXT NOT NULL DEFAULT '',
  author_kind TEXT NOT NULL DEFAULT '',   -- human | bot
  suggestion TEXT NOT NULL DEFAULT '',    -- JSON {value}|{text}; '' = none
  suggestion_commit TEXT NOT NULL DEFAULT '',
  addressed_by TEXT NOT NULL DEFAULT '',  -- the commit that fixed it (a claim
  addressed_by_actor TEXT NOT NULL DEFAULT '',  -- until somebody resolves)
  addressed_at REAL,
  resolved_by TEXT NOT NULL DEFAULT '',
  resolved_kind TEXT NOT NULL DEFAULT '',
  resolved_at REAL,
  resolution TEXT NOT NULL DEFAULT '',    -- the reason — required to ship over
  created_at REAL NOT NULL,               -- a blocking thread you did not raise
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_subject ON threads(subject_type, subject_id, state);
CREATE INDEX IF NOT EXISTS idx_threads_item ON threads(item_id, state);
"""


class ThreadError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def ensure(conn) -> None:
    from .proposals import _statements
    for stmt in _statements(_SCHEMA):
        conn.execute(stmt)


def _ensure(conn) -> None:
    from . import proposals
    proposals.ensure(conn)


# =============================================================================
# KEYS — `plm.toml` locators. One small grammar, shared by the read (outdated)
# and the write (a suggested change), so the two can never disagree.
#
#   process | summary | name …          a top-level field
#   attributes.<name>[.value|.unit]     an attribute (its table, or one field)
#   bom[<path>][.qty|.unit|.find|…]     a [[bom]] line, by its submodule path
#   files.primary | files.<role>        the file roles
#   pin:<path>                          the gitlink at that path (not in TOML)
# =============================================================================

_TOK = re.compile(r"([A-Za-z_][\w-]*)(?:\[([^\]]+)\])?")


def _key_parts(key: str) -> list[tuple[str, str]]:
    parts, rest = [], key
    while rest:
        m = _TOK.match(rest)
        if not m:
            raise ThreadError(f"not a plm.toml key: {key!r}")
        parts.append((m.group(1), m.group(2) or ""))
        rest = rest[m.end():]
        if rest.startswith("."):
            rest = rest[1:]
        elif rest:
            raise ThreadError(f"not a plm.toml key: {key!r}")
    return parts


def key_get(doc: dict, key: str):
    """The value at a key, or None when it is absent."""
    cur = doc
    for name, sel in _key_parts(key):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(name)
        if sel:
            if not isinstance(cur, list):
                return None
            cur = next((x for x in cur if isinstance(x, dict)
                        and str(x.get("path") or x.get("name") or "") == sel), None)
    return cur


def key_set(doc: dict, key: str, value) -> None:
    """Write `value` at a key in a parsed manifest (a suggested change). An
    attribute named without `.value` gets its value set and keeps its unit and
    source; a missing attribute is created."""
    parts = _key_parts(key)
    if parts[0][0] == "attributes" and len(parts) == 2 and not parts[1][1]:
        attrs = doc.setdefault("attributes", {})
        cur = attrs.get(parts[1][0])
        attrs[parts[1][0]] = ({**cur, "value": str(value)} if isinstance(cur, dict)
                              else {"value": str(value)})
        return
    cur = doc
    for i, (name, sel) in enumerate(parts):
        last = i == len(parts) - 1
        if sel:
            lst = cur.get(name)
            if not isinstance(lst, list):
                raise ThreadError(f"{key}: no list `{name}` to select [{sel}] in")
            hit = next((x for x in lst if isinstance(x, dict)
                        and str(x.get("path") or x.get("name") or "") == sel), None)
            if hit is None:
                raise ThreadError(f"{key}: no {name} entry [{sel}]")
            if last:
                raise ThreadError(f"{key}: name a field of the {name} entry "
                                  f"(e.g. {name}[{sel}].qty) to suggest a value")
            cur = hit
            continue
        if last:
            if parts[0][0] == "attributes" and isinstance(value, (int, float)):
                value = str(value)          # plm.toml attribute values are text
            cur[name] = value
        else:
            cur = cur.setdefault(name, {})


# =============================================================================
# THE ANCHORS — each says how to fingerprint itself at a commit, and how to
# tell whether the head still says what the anchor was about.
# =============================================================================

def _repo(number: str):
    from . import gitrepo
    try:
        return gitrepo.repo_path(number)
    except gitrepo.RepoError:
        return None


def _git(repo, *args, check=False) -> str:
    from . import safegit
    return safegit.git(repo, *args, check=check)


def _commit(repo, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet",
                f"{ref}^{{commit}}").strip() if ref else ""


def blob_ident(repo, sha: str, path: str) -> str:
    """The identity of a file at a commit: its git blob id when it is in the
    tree, `sha256:<…>` for a `[[files.external]]` row (bytes outside the tree —
    the manifest's own hash is their identity), '' when it is not there."""
    if not (repo and sha and path):
        return ""
    b = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}:{path}").strip()
    if b:
        return b
    try:
        from . import manifest
        doc = manifest.parse(_git(repo, "cat-file", "blob", f"{sha}:plm.toml"))
    except Exception:                                          # noqa: BLE001
        return ""
    for e in ((doc.get("files") or {}).get("external") or []):
        if e.get("name") == path and e.get("sha256"):
            return "sha256:" + e["sha256"]
    return ""


def _doc_at(repo, sha: str) -> dict:
    from . import manifest
    try:
        return manifest.parse(_git(repo, "cat-file", "blob", f"{sha}:plm.toml",
                                   check=True))
    except Exception:                                          # noqa: BLE001
        return {}


def key_value(repo, sha: str, key: str):
    if key.startswith("pin:"):
        line = _git(repo, "ls-tree", sha, "--", key[4:]).strip()
        return line.split()[2] if line.startswith("160000") else None
    return key_get(_doc_at(repo, sha), key)


def _line_text(repo, sha: str, path: str, line: int) -> str | None:
    raw = _git(repo, "cat-file", "blob", f"{sha}:{path}")
    lines = raw.split("\n")
    return lines[line - 1] if 1 <= line <= len(lines) else None


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def line_moved(repo, a: str, b: str, path: str, line: int) -> dict:
    """Where line `line` of `path` at commit `a` is at commit `b` — or that a
    hunk TOUCHED it, which is what "outdated" means for a line comment (a forge
    keeps a comment on a line that merely moved; one whose line was edited is
    outdated). `{outdated, line_now, why}`."""
    if a == b:
        return {"outdated": False, "line_now": line}
    if not _git(repo, "rev-parse", "--verify", "--quiet", f"{b}:{path}").strip():
        return {"outdated": True, "line_now": None,
                "why": f"{path} is not in the head any more"}
    diff = _git(repo, "diff", "--no-renames", "-U0", a, b, "--", path)
    shift = 0
    for ln in diff.splitlines():
        m = _HUNK.match(ln)
        if not m:
            continue
        os_, oc = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        nc = int(m.group(4) if m.group(4) is not None else 1)
        if oc == 0:
            # pure insertion AFTER old line `os_`
            if os_ < line:
                shift += nc
            continue
        if os_ <= line <= os_ + oc - 1:
            return {"outdated": True, "line_now": None,
                    "why": f"line {line} was changed by a later commit"}
        if os_ + oc - 1 < line:
            shift += nc - oc
    return {"outdated": False, "line_now": line + shift,
            **({"why": f"moved to line {line + shift}"} if shift else {})}


def _id_none(repo, sha, a) -> str:
    return ""


def _id_blob(repo, sha, a) -> str:
    return blob_ident(repo, sha, a.get("path") or "")


def _id_key(repo, sha, a) -> str:
    return json.dumps(key_value(repo, sha, a["key"]), sort_keys=True, default=str)


def _st_never(repo, t, head) -> dict:
    return {"outdated": False}


def _st_blob(repo, t, head) -> dict:
    now = blob_ident(repo, head, t["anchor_path"])
    if not now:
        return {"outdated": True, "why": f"{t['anchor_path']} is not in the head"}
    if now != t["anchor_ident"]:
        return {"outdated": True,
                "why": f"{t['anchor_path']} changed since "
                       f"{t['anchor_sha'][:10]} ({t['anchor_ident'][:12]} → "
                       f"{now[:12]})", "ident_now": now}
    return {"outdated": False}


def _st_key(repo, t, head) -> dict:
    a = json.loads(t["anchor"] or "{}")
    now = json.dumps(key_value(repo, head, a["key"]), sort_keys=True, default=str)
    if now != t["anchor_ident"]:
        return {"outdated": True, "why": f"`{a['key']}` changed since "
                f"{t['anchor_sha'][:10]}", "value_then": json.loads(t["anchor_ident"]
                                                                      or "null"),
                "value_now": json.loads(now)}
    return {"outdated": False, "value_now": json.loads(now)}


def _st_line(repo, t, head) -> dict:
    a = json.loads(t["anchor"] or "{}")
    if blob_ident(repo, head, t["anchor_path"]) == t["anchor_ident"]:
        return {"outdated": False, "line_now": a.get("line")}
    return line_moved(repo, t["anchor_sha"], head, t["anchor_path"],
                      int(a.get("line") or 0))


def _v_line(repo, sha, a) -> dict:
    path, line = a.get("path") or "", int(a.get("line") or 0)
    if line < 1:
        raise ThreadError("a line anchor needs line >= 1")
    text = _line_text(repo, sha, path, line)
    if text is None:
        raise ThreadError(f"{path} has no line {line} at {sha[:10]}")
    return {"path": path, "line": line, "side": a.get("side") or "head",
            "text": text[:400]}


def _vec(v, n=3) -> list[float]:
    if not isinstance(v, (list, tuple)) or len(v) != n:
        raise ThreadError(f"expected {n} numbers, got {v!r}")
    return [round(float(x), 5) for x in v]


def _v_mesh(repo, sha, a) -> dict:
    """Model space, and only model space (PLM_MARKUP_UX §3.1): `p` and `n` are
    the STL's own mm, Z-up coordinates — `vPickAt`'s `local` — and the camera is
    pushed through `V.group.worldToLocal` before it is stored, so a revision
    that gains a part (and so re-centres the display group) restores the same
    view. `tri` is a WITNESS: honoured only while the blob is unchanged."""
    out = {"path": a.get("path") or "", "frame": "model_mm_z_up",
           "p": _vec(a.get("p")), "n": _vec(a.get("n"))}
    if a.get("tri") is not None:
        out["tri"] = int(a["tri"])
    out["snapped"] = bool(a.get("snapped"))
    cam = a.get("camera") or {}
    if cam:
        out["camera"] = {"position": _vec(cam.get("position")),
                         "target": _vec(cam.get("target")),
                         "up": _vec(cam.get("up") or [0, 0, 1]),
                         "fov": float(cam.get("fov") or 45)}
    for k in ("explode", "side", "label"):
        if a.get(k) not in (None, ""):
            out[k] = a[k]
    return out


def _v_image(repo, sha, a) -> dict:
    r = _vec(a.get("rect"), 4)
    if not all(0 <= x <= 1 for x in r):
        raise ThreadError("an image rect is [x, y, w, h], each 0..1 of the image")
    return {"path": a.get("path") or "", "rect": r}


def _v_key(repo, sha, a) -> dict:
    key = str(a.get("key") or "").strip()
    if not key:
        raise ThreadError("a key anchor needs `key` (e.g. attributes.mass_g)")
    if not key.startswith("pin:"):
        _key_parts(key)
    return {"key": key, "value": key_value(repo, sha, key)}


ANCHORS: dict[str, dict] = {
    "subject": {"label": "the subject itself", "path": False,
                "validate": lambda r, s, a: {}, "ident": _id_none,
                "status": _st_never,
                "hint": "a conversation on the proposal or release — never outdated"},
    "line": {"label": "a line", "path": True, "validate": _v_line,
             "ident": _id_blob, "status": _st_line,
             "hint": "{path, line, side: head|base} — outdated when a later "
                     "commit edits that line; follows it when it only moves"},
    "key": {"label": "a plm.toml key", "path": False, "validate": _v_key,
            "ident": _id_key, "status": _st_key,
            "hint": "{key}: attributes.<n> · bom[<path>].qty · files.primary · "
                    "pin:<path> · <field> — outdated when its value changes"},
    "mesh": {"label": "a point on the mesh", "path": True, "validate": _v_mesh,
             "ident": _id_blob, "status": _st_blob,
             "hint": "{path, p:[x,y,z], n:[x,y,z], tri?, camera?} in MODEL space "
                     "(the STL's mm, Z-up) — outdated when that mesh blob changes"},
    "image": {"label": "an image region", "path": True, "validate": _v_image,
              "ident": _id_blob, "status": _st_blob,
              "hint": "{path, rect:[x,y,w,h] 0..1} — outdated when the image changes"},
}


def anchors() -> list[dict]:
    return [{"kind": k, "label": v["label"], "hint": v["hint"]}
            for k, v in ANCHORS.items()]


# =============================================================================
# SUBJECTS
# =============================================================================

def _subject(conn, st: str, sid: int) -> dict:
    """{type, id, item_id, number, revision_id, head, writable, row}."""
    if st in RESERVED_SUBJECTS:
        raise ThreadError(f"`{st}` is a reserved subject (stage 14 — a CHANGE, "
                          "PLM_REVIEW_MODEL §2.5); it is not built yet", 422)
    if st == "proposal":
        from . import proposals
        p = proposals._row(conn, sid)
        if not p:
            raise ThreadError(f"no proposal {proposals.label(sid)}", 404)
        number = conn.execute("SELECT number FROM items WHERE id=?",
                              (p["item_id"],)).fetchone()[0]
        return {"type": st, "id": sid, "item_id": p["item_id"], "number": number,
                "revision_id": p["revision_id"], "head": p["head_sha"],
                "base": proposals.baseline(p, number).get("sha") or "",
                "row": p}
    if st == "change":
        # The change-level CONVERSATION. A thread needs an item to hang its
        # events and search rows on; a change's is its HOME item (its first
        # member). Only `subject` anchors — a line or a mesh point belongs to
        # one member's diff, where the member's proposal is the subject.
        from . import changes
        c = changes.row(conn, sid)
        home = conn.execute("SELECT p.item_id, i.number FROM proposals p JOIN "
                            "items i ON i.id=p.item_id WHERE p.change_id=? "
                            "ORDER BY p.id LIMIT 1", (c["id"],)).fetchone()
        if not home:
            raise ThreadError(f"{changes.title_ref(c['id'])} has no proposals yet — "
                              "a conversation needs something to be about", 409)
        return {"type": st, "id": c["id"], "item_id": home["item_id"],
                "number": home["number"], "revision_id": None, "head": "",
                "base": "", "row": c}
    if st == "release":
        r = conn.execute("SELECT r.*, i.number FROM releases r JOIN items i "
                         "ON i.id=r.item_id WHERE r.id=?", (sid,)).fetchone()
        if not r:
            raise ThreadError(f"no release {sid}", 404)
        return {"type": st, "id": sid, "item_id": r["item_id"],
                "number": r["number"], "revision_id": r["revision_id"],
                "head": r["tag_sha"], "base": "", "row": dict(r)}
    raise ThreadError(f"a thread's subject is one of {', '.join(SUBJECTS)}")


def _event(conn, type_: str, t: dict, *, actor: str, data: dict | None = None):
    db.add_event(conn, type_, item_id=t["item_id"], revision_id=t["revision_id"],
                 actor=actor, data={t["subject_type"]: t["subject_id"],
                                    "thread": t["id"], **(data or {})})


def _row(conn, tid: int) -> dict:
    r = conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
    if not r:
        raise ThreadError(f"no thread {tid}", 404)
    return dict(r)


def _kind(conn, actor: str) -> str:
    return (db.get_user(conn, actor) or {}).get("kind") or ""


def _approver(conn, actor: str) -> bool:
    return db.user_has_role(conn, actor, "approver")


# =============================================================================
# WRITES
# =============================================================================

def open_thread(conn, *, subject_type: str, subject_id: int, body: str,
                actor: str, anchor: dict | None = None, title: str = "",
                blocking: bool = False, suggestion: dict | None = None) -> dict:
    """Start a thread. `anchor = {kind, sha?, …locator}`; `sha` defaults to the
    subject's head (the proposal's head, the release's tag commit)."""
    from . import context, proposals
    _ensure(conn)
    if not (body or "").strip():
        raise ThreadError("a thread starts with a comment — say what you saw")
    if not db.get_user(conn, actor):
        raise ThreadError(f"unknown user: {actor}", 403)
    s = _subject(conn, subject_type, int(subject_id))
    a = dict(anchor or {"kind": "subject"})
    kind = a.pop("kind", "subject") or "subject"
    spec = ANCHORS.get(kind)
    if not spec:
        raise ThreadError(f"unknown anchor kind {kind!r} — one of "
                          f"{', '.join(ANCHORS)}")
    author_kind = _kind(conn, actor)
    if blocking and author_kind == "bot":
        raise ThreadError(
            "a bot's thread is advisory and cannot be blocking — bots propose, "
            "humans decide (PLM_REVIEW_MODEL §2.4). Open it without `blocking`; "
            "a human can mark their own thread blocking.", 403)
    repo = _repo(s["number"])
    sha, path, loc, ident = "", "", {}, ""
    if kind != "subject":
        if repo is None:
            raise ThreadError(f"{s['number']} has no repository, so only a "
                              "`subject` thread can be anchored here", 422)
        sha = _commit(repo, a.pop("sha", "") or s["head"])
        if not sha:
            raise ThreadError("the anchor's commit is not in this repository")
        loc = spec["validate"](repo, sha, a)
        path = loc.get("path") or ""
        if spec["path"]:
            if not path:
                raise ThreadError(f"a {kind} anchor needs `path`")
            if not blob_ident(repo, sha, path):
                raise ThreadError(f"{path} is not in {s['number']} at {sha[:10]}")
        ident = spec["ident"](repo, sha, loc)
    sug = ""
    if suggestion:
        if s["type"] != "proposal":
            raise ThreadError("a suggested change applies to a proposal's draft")
        if kind == "key" and "value" in suggestion:
            sug = json.dumps({"value": suggestion["value"]})
        elif kind == "line" and "text" in suggestion:
            sug = json.dumps({"text": str(suggestion["text"])})
        else:
            raise ThreadError("a suggestion is {value} on a `key` anchor or "
                              "{text} on a `line` anchor")
    now = db.now()
    cur = conn.execute(
        "INSERT INTO threads (subject_type, subject_id, item_id, revision_id, "
        "anchor_kind, anchor_sha, anchor_path, anchor, anchor_ident, title, state, "
        "blocking, author, author_kind, suggestion, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s["type"], s["id"], s["item_id"], s["revision_id"], kind, sha, path,
         json.dumps(loc, default=str), ident,
         (title or body.strip().splitlines()[0])[:200], "open", int(bool(blocking)),
         actor, author_kind, sug, now, now))
    tid = cur.lastrowid
    context.add(conn, body, target_type="thread", target=tid, actor=actor,
                event=False)
    t = _row(conn, tid)
    _event(conn, "thread.opened", t, actor=actor,
           data={"anchor": kind, "path": path, "key": loc.get("key", ""),
                 "line": loc.get("line"), "blocking": bool(blocking),
                 "suggestion": bool(sug), "title": t["title"]})
    if sha:
        proposals.keep_sha(s["number"], sha, f"thread #{tid}")
    events = []
    if blocking and s["type"] == "proposal":
        events.append(proposals.attention(
            conn, "thread.blocking", s["row"], reason="blocking thread opened",
            actor=actor, headline=f"{proposals.label(s['id'])} {s['number']}: {actor} opened a "
                                  f"BLOCKING thread — {t['title']}",
            extra={"thread": tid}))
    out = get(conn, tid)
    if events:
        out["_events"] = events
    return out


def reply(conn, tid: int, *, body: str, actor: str) -> dict:
    from . import context
    _ensure(conn)
    t = _row(conn, tid)
    if not (body or "").strip():
        raise ThreadError("empty reply")
    context.add(conn, body, target_type="thread", target=tid, actor=actor,
                event=False)
    conn.execute("UPDATE threads SET updated_at=? WHERE id=?", (db.now(), tid))
    _event(conn, "thread.reply", t, actor=actor, data={"chars": len(body)})
    return get(conn, tid)


def _resolve_commit(conn, t: dict, sha: str) -> str:
    """`addressed_by` must be a real commit of this item that the subject's
    head CONTAINS — a fix that is not in what will be merged fixes nothing."""
    s = _subject(conn, t["subject_type"], t["subject_id"])
    repo = _repo(s["number"])
    full = _commit(repo, sha) if repo is not None else ""
    if not full:
        raise ThreadError(f"addressed_by {sha!r} is not a commit in "
                          f"{s['number']}.git")
    from . import safegit
    if s["head"] and not safegit.ok(repo, "merge-base", "--is-ancestor", full,
                                    s["head"]):
        raise ThreadError(
            f"{full[:10]} is not in {subject_name(s['type'], s['id'])}'s head "
            f"({s['head'][:10]}) — push it first; a fix that is not in what "
            "will be merged addresses nothing")
    return full


def resolve(conn, tid: int, *, actor: str, reason: str = "",
            addressed_by: str = "") -> dict:
    """Resolve — or, for anybody who is neither the raiser nor an approver,
    CLAIM it addressed (with the commit) and leave the decision to the raiser.

    Shipping over a BLOCKING thread somebody else raised needs a reason: that
    is the recorded decision August wanted kept possible, and it is the only
    way past the RELEASED gate other than fixing the thing."""
    from . import context, proposals
    _ensure(conn)
    t = _row(conn, tid)
    if t["state"] != "open":
        raise ThreadError(f"thread {tid} is already {t['state']} "
                          f"(by {t['resolved_by'] or '—'})", 409)
    full = _resolve_commit(conn, t, addressed_by) if addressed_by else ""
    may = actor == t["author"] or _approver(conn, actor)
    now = db.now()
    out_events = []
    if not may:
        if not full:
            raise ThreadError(
                f"thread {tid} is resolved by the person who raised it "
                f"({t['author']}) or an approver. Record the commit that "
                "addresses it — resolve with addressed_by=<sha> — and they "
                "decide.", 403)
        conn.execute("UPDATE threads SET addressed_by=?, addressed_by_actor=?, "
                     "addressed_at=?, updated_at=? WHERE id=?",
                     (full, actor, now, now, tid))
        context.add(conn, f"**Addressed** by `{full[:10]}`"
                    + (f" — {reason}" if reason else "")
                    + f" — *{t['author']} (or an approver) resolves.*",
                    target_type="thread", target=tid, actor=actor, event=False)
        t = _row(conn, tid)
        _event(conn, "thread.claimed", t, actor=actor,
               data={"addressed_by": full, "reason": reason})
    else:
        if t["blocking"] and actor != t["author"] and not reason.strip():
            raise ThreadError(
                f"thread {tid} is BLOCKING and was raised by {t['author']}. To "
                "ship over it, resolve it WITH A REASON — it is recorded, and "
                "it is what the release gate will cite.", 422)
        conn.execute(
            "UPDATE threads SET state='resolved', resolved_by=?, resolved_kind=?, "
            "resolved_at=?, resolution=?, addressed_by=CASE WHEN ?<>'' THEN ? "
            "ELSE addressed_by END, updated_at=? WHERE id=?",
            (actor, _kind(conn, actor), now, reason, full, full, now, tid))
        context.add(conn, "**Resolved**"
                    + (f" — addressed by `{full[:10]}`" if full else "")
                    + (f": {reason}" if reason else ""),
                    target_type="thread", target=tid, actor=actor, event=False)
        t = _row(conn, tid)
        _event(conn, "thread.resolved", t, actor=actor,
               data={"addressed_by": t["addressed_by"], "reason": reason,
                     "blocking": bool(t["blocking"]),
                     "shipped_over": bool(t["blocking"] and actor != t["author"])})
    if t["subject_type"] == "proposal":
        left = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE subject_type='proposal' AND "
            "subject_id=? AND state='open' AND addressed_by=''",
            (t["subject_id"],)).fetchone()[0]
        if left == 0:
            p = proposals._row(conn, t["subject_id"])
            out_events.append(proposals.attention(
                conn, "threads.addressed", p, reason="all threads addressed",
                actor=actor, headline=f"{proposals.label(p['id'])}: every thread is resolved or "
                                      "addressed — ready for another look"))
    out = get(conn, tid)
    if out_events:
        out["_events"] = out_events
    return out


def reopen(conn, tid: int, *, actor: str, body: str = "") -> dict:
    from . import context
    _ensure(conn)
    t = _row(conn, tid)
    if t["state"] == "open":
        raise ThreadError(f"thread {tid} is already open", 409)
    if actor != t["author"] and not _approver(conn, actor):
        raise ThreadError("a thread is reopened by the person who raised it or "
                          "an approver", 403)
    conn.execute("UPDATE threads SET state='open', resolved_by='', resolved_kind='', "
                 "resolved_at=NULL, resolution='', updated_at=? WHERE id=?",
                 (db.now(), tid))
    context.add(conn, "**Reopened**" + (f": {body}" if body else ""),
                target_type="thread", target=tid, actor=actor, event=False)
    _event(conn, "thread.reopened", _row(conn, tid), actor=actor)
    return get(conn, tid)


def apply_suggestion(conn, tid: int, *, actor: str) -> dict:
    """The reviewer proposed a value; make it ONE commit on the draft, through
    the existing git write path, and resolve the thread with that commit."""
    from . import context, ops, proposals
    _ensure(conn)
    t = _row(conn, tid)
    if not t["suggestion"]:
        raise ThreadError(f"thread {tid} carries no suggested change")
    if t["suggestion_commit"]:
        raise ThreadError(f"already applied as {t['suggestion_commit'][:10]}", 409)
    if t["state"] != "open":
        raise ThreadError(f"thread {tid} is {t['state']}", 409)
    s = _subject(conn, t["subject_type"], t["subject_id"])
    if s["type"] != "proposal" or not s["revision_id"]:
        raise ThreadError("a suggestion is applied to an open proposal's draft")
    rev = conn.execute("SELECT r.*, i.number FROM revisions r JOIN items i "
                       "ON i.id=r.item_id WHERE r.id=?",
                       (s["revision_id"],)).fetchone()
    if rev["state"] != "DRAFT":
        raise ThreadError(
            f"{rev['number']} v{rev['version']} is {rev['state']} — it is in "
            "front of an approver. Withdraw the round (or wait for the "
            "decision), then apply.", 409)
    try:
        g = ops.git_draft(conn, dict(rev))
    except ops.OpsError as exc:
        raise ThreadError(str(exc), exc.code) from None
    if not g:
        raise ThreadError("this draft does not live in a repository (git-first "
                          "is not `write` here) — make the change by hand")
    sug = json.loads(t["suggestion"])
    a = json.loads(t["anchor"] or "{}")
    who = t["author"]
    if t["anchor_kind"] == "key":
        key = a["key"]
        if key.startswith("pin:"):
            raise ThreadError("a pin is moved with a BOM verb, not a suggestion")
        key_set(g["doc"], key, sug["value"])
        msg = f"Apply suggestion from thread #{tid}: {key} = {sug['value']}"
        changes, manifest = None, True
    else:
        repo = g["repo"]
        st = line_moved(repo, t["anchor_sha"], g["commit"], t["anchor_path"],
                        int(a.get("line") or 0))
        if st["outdated"]:
            raise ThreadError(f"cannot apply: {st.get('why')} — the line the "
                              "suggestion replaces is not there any more", 409)
        raw = _git(repo, "cat-file", "blob", f"{g['commit']}:{t['anchor_path']}")
        lines = raw.split("\n")
        lines[st["line_now"] - 1] = sug["text"]
        changes = {t["anchor_path"]: "\n".join(lines).encode()}
        msg = (f"Apply suggestion from thread #{tid}: {t['anchor_path']} "
               f"line {st['line_now']}")
        manifest = False
    msg += f"\n\nSuggested-by: {who}\nApplied-by: {actor}\nPlm-Thread: {tid}"
    try:
        res = ops.git_write(conn, g, dict(rev), message=msg, actor=actor,
                            changes=changes, manifest=manifest)
    except ops.OpsError as exc:
        raise ThreadError(str(exc), exc.code) from None
    events = res.pop("_events", None) or []
    sha = res.get("commit") or ""
    now = db.now()
    # The suggestion WAS the reviewer's decision; applying it verbatim is not a
    # bot deciding — so it resolves, with the commit, and says so.
    conn.execute(
        "UPDATE threads SET suggestion_commit=?, state='resolved', resolved_by=?, "
        "resolved_kind=?, resolved_at=?, resolution=?, addressed_by=?, "
        "updated_at=? WHERE id=?",
        (sha, actor, _kind(conn, actor), now,
         f"suggestion applied verbatim ({who}'s change)", sha, now, tid))
    context.add(conn, f"**Suggestion applied** as `{sha[:10]}` — one commit on "
                f"the draft.", target_type="thread", target=tid, actor=actor,
                event=False)
    _event(conn, "thread.applied", _row(conn, tid), actor=actor,
           data={"commit": sha})
    out = get(conn, tid)
    out["commit"] = sha
    out["git"] = res
    # the head moved: the proposal hook already dismissed stale reviews
    out["proposal"] = proposals.brief(conn, proposals._row(conn, s["id"]))
    if events:
        out["_events"] = events
    return out


# =============================================================================
# READS
# =============================================================================

def _status(t: dict, repo, head: str, memo: dict) -> dict:
    if t["anchor_kind"] == "subject" or not repo or not head:
        return {"outdated": False}
    if json.loads(t["anchor"] or "{}").get("side") == "base":
        # anchored on the BASE side of the diff (a deleted line, the old mesh):
        # the base is the release the proposal grew from, which does not move
        # under it — a forge keeps such comments, and so does this
        return {"outdated": False, "side": "base"}
    k = (t["id"], head)
    if k not in memo:
        try:
            memo[k] = ANCHORS[t["anchor_kind"]]["status"](repo, t, head)
        except Exception as exc:                               # noqa: BLE001
            memo[k] = {"outdated": None, "why": f"could not tell: {exc}"[:200]}
    return memo[k]


def _shape(conn, t: dict, *, head: str, repo, memo: dict,
           comments: bool = True) -> dict:
    a = json.loads(t["anchor"] or "{}")
    out = {**t, "anchor": {"kind": t["anchor_kind"], "sha": t["anchor_sha"],
                           **a},
           "suggestion": json.loads(t["suggestion"]) if t["suggestion"] else None,
           "blocking": bool(t["blocking"]),
           "advisory": t["author_kind"] == "bot",
           "status": _status(t, repo, head, memo), "head": head}
    out["outdated"] = bool(out["status"].get("outdated"))
    if comments:
        out["comments"] = [dict(r) for r in conn.execute(
            "SELECT id, body, actor, created_at FROM context WHERE "
            "target_type='thread' AND target_id=? ORDER BY created_at, id",
            (t["id"],))]
        kinds = {}
        for c in out["comments"]:
            if c["actor"] not in kinds:
                kinds[c["actor"]] = _kind(conn, c["actor"])
            c["kind"] = kinds[c["actor"]]
    return out


def get(conn, tid: int) -> dict:
    _ensure(conn)
    t = _row(conn, tid)
    s = _subject(conn, t["subject_type"], t["subject_id"])
    out = _shape(conn, t, head=s["head"], repo=_repo(s["number"]), memo={})
    out["subject"] = {"type": s["type"], "id": s["id"], "number": s["number"]}
    return out


def listing(conn, scope: list[tuple[str, int]], *, state: str = "",
            comments: bool = True) -> list[dict]:
    """Every thread on a SCOPE (a list of subjects — one proposal today, a
    change's N proposals in stage 14), with its anchor resolved and whether it
    is outdated against that subject's head. `state` = open | resolved |
    outdated | '' (all)."""
    _ensure(conn)
    out = []
    for st, sid in scope:
        s = _subject(conn, st, int(sid))
        repo, memo = _repo(s["number"]), {}
        rows = conn.execute(
            "SELECT * FROM threads WHERE subject_type=? AND subject_id=? "
            + ("AND state=? " if state in ("open", "resolved") else "")
            + "ORDER BY created_at, id",
            (st, int(sid), *([state] if state in ("open", "resolved") else [])))
        for t in rows:
            x = _shape(conn, dict(t), head=s["head"], repo=repo, memo=memo,
                       comments=comments)
            if state == "outdated" and not x["outdated"]:
                continue
            x["subject"] = {"type": s["type"], "id": s["id"],
                            "number": s["number"]}
            out.append(x)
    return out


def counts(conn, scope: list[tuple[str, int]]) -> dict[tuple, dict]:
    """Badge numbers, no git: {(type, id): {open, resolved, blocking, total,
    addressed}} — `blocking` counts OPEN blocking threads."""
    _ensure(conn)
    out: dict[tuple, dict] = {}
    by: dict[str, list[int]] = {}
    for st, sid in scope:
        by.setdefault(st, []).append(int(sid))
    for st, ids in by.items():
        if not ids:
            continue
        for r in conn.execute(
                f"SELECT subject_id, COUNT(*) AS total, "
                f"SUM(state='open') AS open, SUM(state='resolved') AS resolved, "
                f"SUM(state='open' AND blocking=1) AS blocking, "
                f"SUM(state='open' AND addressed_by<>'') AS addressed "
                f"FROM threads WHERE subject_type=? AND subject_id IN "
                f"({','.join('?' * len(ids))}) GROUP BY subject_id", (st, *ids)):
            out[(st, r["subject_id"])] = {k: int(r[k] or 0) for k in ZERO}
    return out


def blocking_open(conn, scope: list[tuple[str, int]]) -> list[dict]:
    _ensure(conn)
    out = []
    for st, sid in scope:
        out += [dict(r) for r in conn.execute(
            "SELECT id, title, author, subject_type, subject_id, anchor_kind, "
            "anchor_path FROM threads WHERE subject_type=? AND subject_id=? AND "
            "state='open' AND blocking=1", (st, int(sid)))]
    return out


def subject_name(st: str, sid) -> str:
    """A thread's subject in a sentence: `proposal p12`, `pull request #380`."""
    from . import changes, proposals
    if st == "proposal":
        return f"proposal {proposals.label(sid)}"
    if st == "change":
        return f"pull request {changes.ref(sid)}"
    return f"{st} #{sid}"


def refusal(blockers: list[dict], what: str) -> str:
    lines = [f"{what} is held by {len(blockers)} open BLOCKING thread"
             f"{'s' if len(blockers) != 1 else ''}:"]
    for b in blockers:
        where = f" on {b['anchor_path']}" if b.get("anchor_path") else ""
        lines.append(f"  thread {b['id']} ("
                     f"{subject_name(b['subject_type'], b['subject_id'])}{where}) — {b['title']}  [raised by "
                     f"{b['author']}]")
    lines += ["", "Fix it and have the raiser resolve it — or, as an approver, "
              "ship over it by resolving it WITH A REASON:",
              "  POST /api/threads/<id>/resolve {\"reason\": \"…\"}   "
              "(MCP: resolve_thread(id, reason=…))"]
    return "\n".join(lines)


def scope_for_release(conn, rel: dict) -> list[tuple[str, int]]:
    """A promotion is held by threads on the release itself AND by blocking
    threads still open on the proposal(s) that produced that revision — a
    concern raised in review does not evaporate because the change merged."""
    _ensure(conn)
    sc = [("release", int(rel["id"]))]
    sc += [("proposal", r[0]) for r in conn.execute(
        "SELECT id FROM proposals WHERE revision_id=? OR (item_id=? AND "
        "merged_tag=?)", (rel["revision_id"], rel["item_id"],
                          rel.get("tag") or f"v{rel.get('version', '')}"))]
    return sc
