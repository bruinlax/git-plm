"""The revision report, from git (stage 19, handoff finding 7).

The workspace skill `report-plm-revision-diff` had a helper that snapshots
`plm/parts.csv`, `versions.csv`, `bom.csv` and `pbom_current.csv` — the frozen
legacy ledgers. In a git-first project those are not the record; the item
repos are. Codex (CHG-379) hand-captured commit/manifest/file-hash snapshots
and wrote REVISION.md itself. This is the helper, rebuilt on the model plm
actually has: an item at a ref is a commit, a revision is its `plm.toml`, a
BOM line's pin is a gitlink, a file is a blob (an LFS object by its oid).

    python -m plm.revreport CID-PRT-0001@0c38698 CID-PRT-0001@draft/manual-charge-retention
    python -m plm.revreport --change CHG-379                     # every member: baseline -> head
    python -m plm.revreport --change CHG-379 --watch CID-PRT-0004 --watch CID-CPN-0001
    python -m plm.revreport … --disposition CID-PRT-0001=REPRINT:"new anchors"
    python -m plm.revreport … --json
    GET /api/revreport?pair=N@a..b&watch=N&change=CHG-n&disposition=N=REPRINT:why&format=md

What it emits (the skill's three sections, from data):

  * REVISION DIFF — per item: before/after ref @ sha, version, YES/NO (the
    same commit, or the same TREE, is NO), what changed (files, manifest
    fields, attributes, BOM pins / qty / find, purchase requirements), the
    physical disposition (`--disposition`; never guessed) and ADOPTION: does
    an assembly in the report pin the item's after-commit (YES), its
    before-commit (NO — the parent still pins the old one), or neither.
  * BOM STATUS — each assembly's gitlink moves (child, old → new, with the
    tag/pseudo-version labels), `[[bom]]` qty/find changes, requirements
    added/removed, and the catalog pin.
  * FILES — every changed file with its content hash before → after
    (sha256 of the bytes; an LFS pointer's oid IS that sha256, so a mesh is
    hashed without fetching it).

Read-only: it runs git plumbing against the bare repos and writes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

from . import gitindex, gitrepo, gitvalidate, lfs, manifest

#: blobs larger than this are reported by their git blob id, not re-hashed
HASH_MAX = 64 * 1024 * 1024
DISPOSITIONS = ("REPRINT", "REWORK", "REUSE", "FIT-TEST-ONLY", "N/A")


class ReportError(ValueError):
    pass


def _resolve(number: str, ref: str) -> tuple[str, str]:
    """(ref as given, commit sha). `ref` may be a tag, a branch (`draft/x`,
    `main`), a full or abbreviated sha; empty = the item's shipping ref."""
    try:
        repo = gitrepo.repo_path(number)
    except gitrepo.RepoError as exc:
        raise ReportError(f"{number}: {exc}") from None
    if not ref:
        ref = gitvalidate.default_ref(repo).replace("refs/heads/", "")
    for cand in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}"):
        sha = gitvalidate.git(repo, "rev-parse", "--verify", "--quiet",
                              "--end-of-options", f"{cand}^{{commit}}",
                              check=False).strip()
        if sha:
            return ref, sha
    raise ReportError(f"{number}: no ref or commit {ref!r}")


def _manifest(repo, sha: str) -> dict:
    try:
        return manifest.parse(gitvalidate.git(repo, "cat-file", "blob",
                                              f"{sha}:plm.toml", check=False) or "")
    except Exception:                                          # noqa: BLE001
        return {}


def _hashes(repo, entries: dict, paths: list[str]) -> dict[str, dict]:
    """path -> {sha256, size, lfs} for blobs; an LFS pointer is hashed as the
    OBJECT it points at (its oid) — never fetched."""
    want = [entries[p] for p in paths if p in entries
            and entries[p].mode != gitindex.MODE_GITLINK]
    small = [e.sha for e in want if e.size <= HASH_MAX]
    body = gitvalidate._cat_batch(repo, small) if small else {}
    out = {}
    for e in want:
        data = body.get(e.sha)
        ptr = lfs.parse_pointer(data) if data is not None and \
            len(data) <= lfs.POINTER_MAX else None
        if ptr:
            out[e.path] = {"sha256": ptr[0], "size": ptr[1], "lfs": True}
        elif data is not None:
            out[e.path] = {"sha256": hashlib.sha256(data).hexdigest(),
                           "size": len(data), "lfs": False}
        else:
            out[e.path] = {"sha256": "", "blob": e.sha, "size": e.size,
                           "lfs": False}
    return out


def _bom_key(line: dict) -> str:
    return str(line.get("path") or "") or \
        "requirement:" + str(line.get("requirement") or "")


def _label(child: str, sha: str) -> str:
    if not sha:
        return "—"
    try:
        lab = gitindex.pin_label(child, sha)
    except Exception:                                          # noqa: BLE001
        return sha[:10]
    v = lab.get("version") or ""
    return (f"v{v}" if lab.get("released") else (v or sha[:10])) + \
        f" @{sha[:10]}"


def compare(number: str, before: str, after: str) -> dict:
    """One item, two refs -> the report row's facts."""
    repo = gitrepo.repo_path(number)
    bref, bsha = _resolve(number, before)
    aref, asha = _resolve(number, after)
    out = {"item": number, "before": {"ref": bref, "sha": bsha},
           "after": {"ref": aref, "sha": asha}}
    ma, mb = _manifest(repo, bsha), _manifest(repo, asha)
    out["name"] = mb.get("name") or ma.get("name") or ""
    out["kind"] = mb.get("kind") or ma.get("kind") or ""
    out["before"]["version"] = str(ma.get("version") or "")
    out["after"]["version"] = str(mb.get("version") or "")
    ea = gitvalidate.tree_entries(repo, bsha)
    eb = gitvalidate.tree_entries(repo, asha)
    same_tree = gitvalidate.git(repo, "rev-parse", f"{bsha}^{{tree}}").strip() \
        == gitvalidate.git(repo, "rev-parse", f"{asha}^{{tree}}").strip()
    out["changed"] = bsha != asha and not same_tree
    out["same_tree"] = same_tree
    # files (gitlinks are pins, reported under bom)
    blobs = lambda e: {p: x for p, x in e.items()                # noqa: E731
                       if x.mode != gitindex.MODE_GITLINK}
    ba, bb = blobs(ea), blobs(eb)
    changed = sorted(p for p in set(ba) | set(bb)
                     if (ba.get(p).sha if p in ba else None)
                     != (bb.get(p).sha if p in bb else None))
    ha, hb = _hashes(repo, ea, changed), _hashes(repo, eb, changed)
    files = []
    for p in changed:
        st = ("added" if p not in ba else "removed" if p not in bb
              else "modified")
        x, y = ha.get(p, {}), hb.get(p, {})
        storage = ("plain → LFS" if x and y and not x.get("lfs") and y.get("lfs")
                   else "LFS" if (y or x).get("lfs") else "")
        files.append({"path": p, "status": st,
                      "before": x.get("sha256") or (x.get("blob") or ""),
                      "after": y.get("sha256") or (y.get("blob") or ""),
                      "size_before": x.get("size"), "size_after": y.get("size"),
                      "content_same": bool(x and y and x.get("sha256")
                                           and x.get("sha256") == y.get("sha256")),
                      "storage": storage})
    out["files"] = files
    out["files_unchanged"] = len(set(ba) & set(bb)) - sum(
        1 for f in files if f["status"] == "modified")
    # manifest: fields, attributes
    fields = []
    for k in ("version", "name", "kind", "process", "summary", "state"):
        if str(ma.get(k) or "") != str(mb.get(k) or ""):
            fields.append({"field": k, "from": ma.get(k, ""), "to": mb.get(k, "")})
    for k in ("source",):
        if (ma.get(k) or {}) != (mb.get(k) or {}):
            fields.append({"field": "[source]", "from": ma.get(k), "to": mb.get(k)})
    aa, ab = ma.get("attributes") or {}, mb.get("attributes") or {}
    attrs = [{"name": n, "from": (aa.get(n) or {}).get("value"),
              "to": (ab.get(n) or {}).get("value")}
             for n in sorted(set(aa) | set(ab)) if aa.get(n) != ab.get(n)]
    out["manifest"] = {"fields": fields, "attributes": attrs}
    # BOM: pins (gitlinks), and the [[bom]] line facts
    la = {p: e.sha for p, e in ea.items() if e.mode == gitindex.MODE_GITLINK}
    lb = {p: e.sha for p, e in eb.items() if e.mode == gitindex.MODE_GITLINK}
    pins = []
    for p in sorted(set(la) | set(lb)):
        if la.get(p) == lb.get(p):
            continue
        child = p.split("/", 1)[1] if "/" in p else p
        pins.append({"path": p, "child": child, "from": la.get(p, ""),
                     "to": lb.get(p, ""),
                     "from_label": _label(child, la.get(p, "")) if p != "catalog"
                     else (la.get(p, "")[:10] or "—"),
                     "to_label": _label(child, lb.get(p, "")) if p != "catalog"
                     else (lb.get(p, "")[:10] or "—")})
    ba_, bb_ = ({_bom_key(x): x for x in (m.get("bom") or [])} for m in (ma, mb))
    lines = []
    for k in sorted(set(ba_) | set(bb_)):
        x, y = ba_.get(k), bb_.get(k)
        if x == y:
            continue
        d = {"line": k}
        if x is None:
            d["status"] = "added"
        elif y is None:
            d["status"] = "removed"
        else:
            d["status"] = "changed"
            d["fields"] = {f: [x.get(f), y.get(f)] for f in sorted(set(x) | set(y))
                           if x.get(f) != y.get(f)}
        d["requirement"] = k.startswith("requirement:")
        lines.append(d)
    pinned = [x for x in (mb.get("bom") or []) if x.get("path")]
    out["bom"] = {"pins": pins, "lines": lines,
                  "lines_after": len(pinned),
                  "requirements_after": sum(1 for x in (mb.get("bom") or [])
                                            if not x.get("path")
                                            and x.get("requirement"))}
    out["_gitlinks_after"] = lb
    out["_gitlinks_before"] = la
    return out


def report(pairs: list[tuple[str, str, str]], *, watches: list[tuple[str, str]]
           = (), dispositions: dict | None = None, change: str = "") -> dict:
    """pairs: (number, before, after); watches: (number, ref) — before ==
    after, shown as inspected-only. Adds adoption across the report."""
    rows = [compare(n, b, a) for n, b, a in pairs]
    have = {r["item"] for r in rows}
    for n, ref in watches:
        if n in have:
            continue
        r = compare(n, ref, ref)
        r["watched"] = True
        rows.append(r)
    disp = dispositions or {}
    asm = [r for r in rows if r["_gitlinks_after"]]
    for r in rows:
        d = disp.get(r["item"])
        r["disposition"] = d or {"value": "N/A" if not r["changed"] else "",
                                 "why": ""}
        adopt = []
        for p in asm:
            if p is r:
                continue
            path = f"parts/{r['item']}"
            after_pin = p["_gitlinks_after"].get(path)
            if not after_pin:
                continue
            if after_pin == r["after"]["sha"]:
                adopt.append({"parent": p["item"], "adopted": "YES",
                              "pin": after_pin})
            elif after_pin == r["before"]["sha"]:
                adopt.append({"parent": p["item"], "adopted": "NO",
                              "pin": after_pin,
                              "why": "the parent still pins the before-commit"})
            else:
                adopt.append({"parent": p["item"], "adopted": "OTHER",
                              "pin": after_pin, "label": _label(r["item"],
                                                                after_pin)})
        r["adoption"] = adopt
    for r in rows:
        r.pop("_gitlinks_after", None)
        r.pop("_gitlinks_before", None)
    return {"change": change, "items": rows,
            "changed": sum(1 for r in rows if r["changed"]),
            "watched_unchanged": [r["item"] for r in rows if not r["changed"]]}


def from_change(cid: str) -> tuple[list[tuple[str, str, str]], str]:
    """Every member of a change: REVIEW BASELINE -> head (stage 19)."""
    from . import changes, db, proposals
    with db.connect() as conn:
        c = changes.row(conn, cid)
        pairs = []
        for m in changes.members(conn, c["id"]):
            rb = proposals.baseline(m, m["item_number"])
            head = m["merged_sha"] or m["head_sha"]
            if not head:
                continue
            pairs.append((m["item_number"], rb.get("sha") or head, head))
        return pairs, changes.label(c["id"])


def _parse_pair(spec: str) -> tuple[str, str, str]:
    """`N@before..after` or the two-argument form handled by the CLI."""
    n, _, refs = spec.partition("@")
    b, sep, a = refs.partition("..")
    if not n or not sep:
        raise ReportError(f"a pair is N@<before>..<after>: {spec!r}")
    return n, b, a


def _parse_disp(specs: list[str]) -> dict:
    out = {}
    for s in specs or []:
        n, _, rest = s.partition("=")
        v, _, why = rest.partition(":")
        v = v.strip().upper()
        if v not in DISPOSITIONS:
            raise ReportError(f"disposition is one of {', '.join(DISPOSITIONS)}: "
                              f"{s!r}")
        out[n.strip()] = {"value": v, "why": why.strip()}
    return out


def markdown(rep: dict, *, web: str = "") -> str:
    from . import config, webroutes
    web = (web or config.PUBLIC_URL).rstrip("/")
    L = []
    if rep.get("change"):
        L += [f"Change: [{rep['change']}]({web}{webroutes.change(rep['change'])})", ""]
    L += ["## Revision diff", "",
          "| Item | Before (ref @ sha) | After (ref @ sha) | Changed? | What "
          "changed | Physical disposition | Adopted by parent? |",
          "|---|---|---|---|---|---|---|"]
    for r in rep["items"]:
        b, a = r["before"], r["after"]
        what = []
        if r.get("watched") or not r["changed"]:
            what.append("inspected only" if r.get("watched") else
                        "same tree" if r.get("same_tree") else "unchanged")
        else:
            if b["version"] != a["version"]:
                what.append(f"v{b['version'] or '—'} → v{a['version'] or '—'}")
            nf = len(r["files"])
            if nf:
                what.append(f"{nf} file{'s' if nf != 1 else ''} "
                            + "(" + ", ".join(f"{f['status']} `{f['path']}`"
                                              for f in r["files"][:4])
                            + (" …" if nf > 4 else "") + ")")
            if r["manifest"]["attributes"]:
                what.append(", ".join(f"{x['name']} {x['from']} → {x['to']}"
                                      for x in r["manifest"]["attributes"][:4]))
            for p in r["bom"]["pins"]:
                what.append(f"pin `{p['path']}` {p['from_label']} → {p['to_label']}")
            for ln in r["bom"]["lines"]:
                what.append(f"BOM {ln['status']} {ln['line']}"
                            + (f" ({', '.join(f'{k} {v[0]}→{v[1]}' for k, v in ln.get('fields', {}).items())})"
                               if ln.get("fields") else ""))
        d = r["disposition"]
        dv = (d["value"] + (f" — {d['why']}" if d.get("why") else "")) \
            if d.get("value") else "**state it** (REPRINT/REWORK/REUSE/FIT-TEST-ONLY)"
        ad = "; ".join(f"{x['parent']}: {x['adopted']}"
                       + (f" ({x['why']})" if x.get("why") else "")
                       + (f" ({x['label']})" if x.get("label") else "")
                       for x in r["adoption"]) or "—"
        def side(x):
            ref = "" if x["sha"].startswith(x["ref"]) else f"{x['ref']} @ "
            return f"{ref}{x['sha'][:10]} (v{x['version'] or '—'})"
        L.append(f"| {r['item']} | {side(b)} | {side(a)} "
                 f"| {'YES' if r['changed'] else 'NO'} | {'; '.join(what)} "
                 f"| {dv} | {ad} |")
    L += ["", "## BOM status", ""]
    asm = [r for r in rep["items"] if r["bom"]["pins"] or r["bom"]["lines"]
           or r["bom"]["lines_after"]]
    if not asm:
        L.append("- BOM changed: **NO** — no item in this report carries BOM lines.")
    for r in asm:
        moved = bool(r["bom"]["pins"] or r["bom"]["lines"])
        L.append(f"- **{r['item']}** BOM changed: **{'YES' if moved else 'NO'}** "
                 f"({r['bom']['lines_after']} pinned line(s)"
                 + (f", {r['bom']['requirements_after']} purchase requirement(s) "
                    "not selected" if r["bom"]["requirements_after"] else "")
                 + ")")
        for p in r["bom"]["pins"]:
            L.append(f"  - `{p['path']}`: {p['from_label']} → {p['to_label']}")
        for ln in r["bom"]["lines"]:
            L.append(f"  - line {ln['status']}: {ln['line']}"
                     + (f" — {ln['fields']}" if ln.get("fields") else ""))
        L.append(f"  - [BOM]({web}{webroutes.tab(r['item'], 'bom')}) · [compare]"
                 f"({web}{webroutes.compare(r['item'], r['before']['sha'][:12], r['after']['sha'][:12])})")
    L += ["", "## Files (content sha256; an LFS file by its object id)", ""]
    for r in rep["items"]:
        if not r["files"]:
            continue
        L.append(f"**{r['item']}** ({r['files_unchanged']} file(s) unchanged)")
        for f in r["files"]:
            L.append(f"- `{f['status']}` `{f['path']}` "
                     f"{(f['before'] or '—')[:16]} → {(f['after'] or '—')[:16]}"
                     + (f" [{f['storage']}]" if f["storage"] else "")
                     + (" — same content, storage only" if f["content_same"]
                        else ""))
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m plm.revreport",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("refs", nargs="*",
                    help="N@before N@after (pairs), or N@before..after")
    ap.add_argument("--change", default="", help="CHG-n: every member, "
                    "review baseline -> head")
    ap.add_argument("--watch", action="append", default=[],
                    help="N[@ref] — an item to show as inspected (unchanged "
                         "unless it is also a pair)")
    ap.add_argument("--disposition", action="append", default=[],
                    help="N=REPRINT|REWORK|REUSE|FIT-TEST-ONLY|N/A[:why]")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        pairs: list = []
        chg = ""
        if a.change:
            pairs, chg = from_change(a.change)
        refs = list(a.refs)
        while refs:
            r = refs.pop(0)
            if ".." in r.partition("@")[2]:
                pairs.append(_parse_pair(r))
                continue
            if not refs:
                raise ReportError(f"{r!r} needs its after-side: N@before N@after")
            r2 = refs.pop(0)
            n1, _, b = r.partition("@")
            n2, _, af = r2.partition("@")
            if n1 != n2:
                raise ReportError(f"a pair names one item: {r} {r2}")
            pairs.append((n1, b, af))
        watches = [(w.partition("@")[0], w.partition("@")[2]) for w in a.watch]
        rep = report(pairs, watches=watches,
                     dispositions=_parse_disp(a.disposition), change=chg)
    except ReportError as exc:
        print(f"revreport: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=1) if a.json else markdown(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
