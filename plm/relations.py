"""Typed item-to-item relations: one table, one registry, no per-type code.

The `relations` table has existed since fork() needed somewhere to record
provenance. This module promotes it from an internal detail to a first-class,
typed, queryable surface (docs/PLM_RELATIONS.md): every future link — coupons
today; fixtures, test reports, supersedes, mating parts tomorrow — is one row
and one REGISTRY entry, never a new table, endpoint or UI card.

THE REGISTRY IS THE FEATURE. Labels (both directions), kind constraints and
whether a type is revision-level all live in the dict below. The HTTP API, the
MCP and the web UI render and validate from it, so adding a relation type is a
dict entry and nothing else. If you find yourself writing `if type ==
"coupon_for"` anywhere outside this file, the design has slipped.

Revision pointers are TEXT versions, not revision ids — the same idiom BOM
lines use for `child_version` (docs/PLM_DATA_MODEL.md), and the shape the
pre-existing derived_from rows are already written in. Empty means item-level:
`derived_from` says "this item came from that item", while `coupon_for` says
"this coupon validates THAT revision", because a coupon for v0.3.0 says nothing
about v0.4.0.

WHERE A LINK LIVES — `home` (stage 10).
The rule, taken from git forges and Teamcenter alike: a link is recorded ONCE,
by the side that makes the claim, in the home whose lifetime matches the claim;
the other direction is COMPUTED (`for_item`'s `in` rows are the forge's "Used
by" — an index, never a second record). And attaching evidence to a released
revision never revises it. Three homes, one per lifetime:

  revision  a claim THIS REVISION makes (`coupon_for`, `integration_envelope_for`):
            `[[relations]]` in that revision's `plm.toml`. Carried forward on
            revise like everything else in the tree, editable on a draft, frozen
            at release. The target is pinned by version AND commit.
  item      a claim the ITEM makes across its revisions (`derived_from`,
            `bench_test_of`, `mounts_to`, `cut_from`, `instance_of`):
            `[[relations]]` in `item.toml` on `refs/plm/item`.
  note      a post-release FACT about a revision (`installed_on`): an entry in
            that revision's `refs/notes/plm` note. Never in a tree.

In `off` mode none of this is visible — the table is the record, as on master.
The homes say where git keeps each claim; `claimant()` below says which revision
a revision- or note-home claim belongs to, and is the ONE place that answers it.
"""
from __future__ import annotations

import sqlite3

from . import db

# type -> {label, inverse_label, rev_level, home, from_kinds, to_kinds, hint}
#
#   label          how the FROM side reads it   (CPN-0001 "verifies" PRT-1011)
#   inverse_label  how the TO side reads it     (PRT-1011 "verified by" CPN-0001)
#   rev_level      the versions matter; they default to each side's latest and
#                  the target must actually have a revision to point at
#   from_kinds     item kinds allowed on the from side (() = any)
#   to_kinds       item kinds allowed on the to side   (() = any)
#   home           revision | item | note — where git keeps the claim (HOMES)
#   results        the claim may carry a test RESULT (result/result_note/
#                  measured) and so can stand as release EVIDENCE (release.py)
#   note_key       home=note only: the `[[<note_key>]]` array in the note
REGISTRY: dict[str, dict] = {
    "derived_from": {
        "label": "derived from",
        "inverse_label": "forks",
        "rev_level": False,
        "home": "item",
        "from_kinds": (),
        "to_kinds": (),
        "hint": "provenance of a fork: this item was seeded from that one. "
                "Written by fork(); item-level, because a fork diverges from "
                "its source and stops tracking it.",
    },
    "coupon_for": {
        "label": "verifies",
        "inverse_label": "verified by",
        "rev_level": True,
        "home": "revision",
        "results": True,
        "from_kinds": ("coupon",),
        "to_kinds": (),
        "hint": "a test coupon validates ONE specific revision (fit, "
                "tolerance, print settings). V&V trace, not structure: "
                "coupons never appear in a BOM.",
    },
    "bench_test_of": {
        "label": "bench test of",
        "inverse_label": "bench tested by",
        "rev_level": False,
        "home": "item",
        "from_kinds": ("assembly", "part"),
        "to_kinds": (),
        "hint": "a bench-test build — a rig, a fixture, a partial reprint made "
                "to exercise something — and the item it exercises. ITEM-level: "
                "a rig follows the thing it tests across revisions. This is a "
                "V&V artifact, NOT product structure: a bench test is never a "
                "product (items.product) and never a BOM parent of the real "
                "assembly. Use it instead of leaving a test build dangling at "
                "the top of a project, where every dashboard mistakes it for "
                "the thing being built. When the test article validates ONE "
                "revision and nothing else, it is a coupon — use coupon_for.",
    },
    # -- ASSETS: the thing you attach to, which you neither build nor buy ------
    # (docs/PLM_RELATIONS.md). kind `asset`, class AST. The three
    # relations below are deliberately three, not one: an interface, a state and
    # a constraint are different facts, and one word for all three cannot answer
    # "which of my two racks is on the camper right now".
    "mounts_to": {
        "label": "mounts to",
        "inverse_label": "mounting point for",
        "rev_level": False,
        "home": "item",
        "from_kinds": (),
        "to_kinds": ("asset",),
        "hint": "the PHYSICAL INTERFACE: this item is designed to bolt/clamp/"
                "hang on that asset. ITEM-level, because an interface follows "
                "the design across revisions — the same reason bench_test_of "
                "is item-level. Says nothing about whether it is on there "
                "today (that is installed_on) and nothing about the geometry "
                "it must clear (that is integration_envelope_for). An asset is "
                "what this mounts to, NOT a BOM child: you neither build nor "
                "buy the camper.",
    },
    "installed_on": {
        "label": "installed on",
        "inverse_label": "has installed",
        "rev_level": True,
        "home": "note",
        "note_key": "installed",
        "from_kinds": (),
        "to_kinds": ("asset",),
        "hint": "the STATE: this article is on that asset right now. "
                "REVISION-level, and the from_version is the point — it is the "
                "as-built fact 'the rack currently bolted to the camper is "
                "v1.0.1'. Two racks can both mounts_to one camper; only one is "
                "installed_on it. Remove the link when it comes off; the event "
                "log keeps the history.",
    },
    "integration_envelope_for": {
        "label": "integration envelope for",
        "inverse_label": "constrained by envelope",
        "rev_level": True,
        "home": "revision",
        "from_kinds": ("asset",),
        "to_kinds": (),
        "hint": "REFERENCE GEOMETRY THAT CONSTRAINS A DESIGN WITHOUT ENTERING "
                "ITS STRUCTURE: this asset revision's measured shape is the "
                "envelope that design revision has to fit. Revision-level on "
                "BOTH sides on purpose — 'does the rack fit?' is a question "
                "about one scan and one design, and pinning both is what makes "
                "it answerable from data (and redlinable by 3D markup). Use it "
                "when the asset supplies clearances/datums rather than (or as "
                "well as) a bolt pattern.",
    },
    # -- RAW STOCK: what a fabricated part is cut off (plm/fab.py) ----
    "cut_from": {
        "label": "cut from",
        "inverse_label": "stock for",
        "rev_level": False,
        "home": "item",
        "from_kinds": ("part",),
        "to_kinds": ("purchased",),
        "hint": "THE RAW STOCK THIS PART COMES OFF: an 86-inch rail cut from a "
                "10-foot stick of 1x2x0.120 tube. The stock is a `purchased` "
                "item — it is bought, it has a supplier, it belongs in the BUY "
                "list — and this link is what makes the cut list and the "
                "1-D nest possible (fab.cut_list groups by the stock's "
                "`stock_profile` and packs into its `stock_length`).\n"
                "A RELATION AND NOT A COLUMN, because relations.for_item "
                "already returns BOTH directions already labelled: 'what is "
                "this cut from' on the part and 'what is cut from this stock' on "
                "the steel. That is the weldment -> cut part -> raw stock "
                "traceability requirement satisfied both ways with no new read "
                "path, no new UI card and no new tool. A column would answer one "
                "direction and would need all three.\n"
                "ITEM-level, on the mounts_to/bench_test_of precedent: which "
                "stock a design is cut from follows it across revisions, and "
                "revise() does not copy relations — a revision-pinned link "
                "would go stale at the next bump and silently drop the part out "
                "of the cut list. The LENGTH is revision-level, and it lives "
                "where it belongs: the CUT operation's `length` param.\n"
                "The stock is normally NOT a BOM child, and this is GUIDANCE, not "
                "a guard (purchased items are legitimate BOM children — screws "
                "are). A BOM line is an OCCURRENCE, 'one of these is in there'. A "
                "purchased length used WHOLE and uncut is a fine BOM line; what "
                "is wrong is listing a 10-foot stick as an occurrence in a "
                "weldment that consumes 18 inches of it. The stick is what you "
                "BUY (fab.cut_list says how many); the piece is what you BUILD "
                "WITH, and the piece is the part.",
    },
    "instance_of": {
        "label": "instance of",
        "inverse_label": "instantiated by",
        "rev_level": False,
        "home": "item",
        "from_kinds": ("asset",),
        "to_kinds": (),
        "hint": "THE SPECIFIC ONE vs THE MODEL: this asset is the particular "
                "unit in the driveway, with its own measured dimensions; the "
                "target is the type/model it is an example of. Fit work should "
                "point at the INSTANCE — the real rail spacing is the datum, "
                "not the brochure's. Only create the type when there are two "
                "units, or when the published spec is a fact worth keeping "
                "beside the measured one.",
    },
}

_DEFAULT = {"rev_level": False, "from_kinds": (), "to_kinds": (), "hint": "",
            "home": "item", "results": False, "note_key": ""}

#: The three homes, as data — what the UI, the MCP and the readiness report say
#: about where a claim is written. `where` is a sentence a person can act on.
#: ⚠️ It is served as the ADDITIVE key `home_where` on every type: the `hint`
#: strings are master's text verbatim, because `off` serves them (compat.sh).
HOMES: dict[str, dict] = {
    "revision": {
        "label": "the claimant revision's plm.toml",
        "where": "`[[relations]]` in the claimant REVISION's plm.toml — carried "
                 "forward on revise, editable on a draft, frozen at release",
        "file": "plm.toml",
    },
    "item": {
        "label": "item.toml on refs/plm/item",
        "where": "`[[relations]]` in item.toml on refs/plm/item — one claim for "
                 "every revision of the item",
        "file": "item.toml",
    },
    "note": {
        "label": "a refs/notes/plm note on the revision",
        "where": "a post-release fact in the revision's refs/notes/plm note — "
                 "never in a tree, so the tag does not move",
        "file": "refs/notes/plm",
    },
}

#: The outcomes a `results` claim may record. `pending` = printed, not judged.
RESULTS = ("pass", "fail", "pending")


class RelationError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def spec(type_: str) -> dict:
    """The registry entry for a type. Unknown types (rows written by an older
    or newer version of this code) degrade to a readable label instead of
    disappearing from the UI — the table is the record, the dict is the gloss."""
    if type_ in REGISTRY:
        return {**_DEFAULT, **REGISTRY[type_]}
    return {**_DEFAULT, "label": type_.replace("_", " "),
            "inverse_label": f"{type_.replace('_', ' ')} (inverse)"}


def types() -> list[dict]:
    """The registry, for callers that build their own UI or prompt from it."""
    return [{"type": t, **spec(t), "home_where": HOMES[spec(t)["home"]]["where"]}
            for t in sorted(REGISTRY)]


def home(type_: str) -> str:
    """`revision` | `item` | `note` — where git keeps a claim of this type."""
    return spec(type_)["home"]


def types_with_home(h: str) -> list[str]:
    return sorted(t for t in REGISTRY if spec(t)["home"] == h)


# -- the claimant: which REVISION makes a revision- or note-home claim ---------

def claimant(conn: sqlite3.Connection, row) -> dict:
    """`{revision_id, version, how}` for one relations row. THE one answer.

    `how` says how sure we are, because the honest answer differs by row:

      declared   `revision_id` (or the older `from_version`) names it.
      inferred   neither does — a link written before its claimant had a
                 revision (add_coupon, then check-in: every such row on prod was
                 made minutes BEFORE the coupon's first revision). The claimant
                 is the item's first revision created at or after the link, else
                 its oldest. `migrate_cleanup --only relations` reports each one
                 and `--apply` writes it down, after which it is `declared`.
      none       the claimant item has no revision at all. There is no tree to
                 declare the claim in; it stays a registry row.
      item       an item-home type — no revision makes the claim.
    """
    r = dict(row)
    if home(r["relation"]) == "item":
        return {"revision_id": None, "version": "", "how": "item"}
    rid = r.get("revision_id")
    if rid:
        v = conn.execute("SELECT version FROM revisions WHERE id=?",
                         (rid,)).fetchone()
        if v:
            return {"revision_id": rid, "version": v["version"],
                    "how": "declared"}
    if r.get("from_version"):
        v = conn.execute("SELECT id FROM revisions WHERE item_id=? AND "
                         "version=?", (r["item_id"], r["from_version"])
                         ).fetchone()
        if v:
            return {"revision_id": v["id"], "version": r["from_version"],
                    "how": "declared"}
    revs = conn.execute(
        "SELECT id, version, created_at FROM revisions WHERE item_id=? "
        "ORDER BY created_at, vmaj, vmin, vpat", (r["item_id"],)).fetchall()
    if not revs:
        return {"revision_id": None, "version": "", "how": "none"}
    at = float(r.get("created_at") or 0)
    pick = next((v for v in revs if float(v["created_at"] or 0) >= at), None)
    pick = pick or min(revs, key=lambda v: db.parse_version(v["version"]))
    return {"revision_id": pick["id"], "version": pick["version"],
            "how": "inferred"}


def _revision_id(conn: sqlite3.Connection, item_id: int, version: str):
    if not version:
        return None
    v = conn.execute("SELECT id FROM revisions WHERE item_id=? AND version=?",
                     (item_id, version)).fetchone()
    return v["id"] if v else None


# -- the target pin: version AND commit ----------------------------------------

def pin_commit(number: str, version: str) -> str:
    """The commit `number`'s repo names for `version`: its release tag, else the
    tip of the draft branch whose plm.toml claims that version. '' when there is
    no repo (off mode, a catalog item) or no such ref.

    The same resolution a BOM gitlink gets, asked of the repo rather than of a
    build in progress — so a claim written today and a claim re-derived
    tomorrow name the same commit.
    """
    if not number or not version:
        return ""
    try:
        from . import gitvalidate, gitwrite, safegit
        repo = safegit.repo_dir(gitwrite.REPOS, number)
        if not (repo / "HEAD").exists():
            return ""
        sha = gitvalidate.releases(repo).get(version, "")
        if sha:
            return sha
        tips = gitvalidate.draft_branches(repo)
        for ref, v in gitvalidate.draft_versions(repo).items():
            if v == version and tips.get(ref):
                return tips[ref]
    except Exception:  # noqa: BLE001 — no repo is an answer, not an error
        return ""
    return ""


def same_design(number: str, pinned: str, sha: str) -> bool:
    """Does a claim pinned at `pinned` speak for commit `sha`?

    Exactly when they are the same commit — or when `sha` is plm's RELEASE
    commit of it: approval re-emits plm.toml with the granted state on top of
    the commit that was reviewed (gitindex.approve_commit, `Plm-Origin:
    release`), and a coupon printed from the reviewed commit tested every byte
    of the design the tag ships. Nothing looser: a coupon that tested an older
    commit of a re-pushed draft did not test this one, and saying so is the
    point of pinning the commit at all.
    """
    if not pinned or not sha:
        return False
    if pinned == sha:
        return True
    try:
        from . import gitvalidate, gitwrite, safegit
        repo = safegit.repo_dir(gitwrite.REPOS, number)
        body = gitvalidate.git(repo, "log", "-1", "--format=%B", sha,
                               check=False)
        if "Plm-Origin: release" not in body:
            return False
        parents = gitvalidate.git(repo, "rev-list", "--parents", "-n", "1",
                                  sha, check=False).split()[1:]
        if pinned in parents:
            return True
        # a merge-tree release: the reviewed commit is the merge's 2nd parent
        for p in parents:
            pp = gitvalidate.git(repo, "rev-list", "--parents", "-n", "1", p,
                                 check=False).split()[1:]
            if pinned in pp[1:]:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def target_commit(row) -> str:
    """The commit a claim pins: what was recorded, else what its version
    resolves to now (a row written before commits were recorded)."""
    r = dict(row)
    return r.get("target_commit") or pin_commit(
        r.get("to_number") or r.get("target_number") or "",
        r.get("target_version") or r.get("to_version") or "")


def claims_on(conn: sqlite3.Connection, number: str, sha: str, *,
              results_only: bool = True) -> list[dict]:
    """Every claim made ABOUT commit `sha` of `number` — the derived inverse,
    by commit. The `verified-by-coupon` check and the release page's
    "attest by reference" both read exactly this."""
    item = db.resolve_item(conn, number)
    if not item or not sha:
        return []
    out = []
    for r in conn.execute(
            """SELECT r.*, f.number AS from_number, t.number AS to_number
               FROM relations r JOIN items f ON f.id=r.item_id
                                JOIN items t ON t.id=r.target_item_id
               WHERE r.target_item_id=? ORDER BY r.id""", (item["id"],)):
        sp = spec(r["relation"])
        if sp["home"] != "revision" or (results_only and not sp["results"]):
            continue
        pinned = target_commit(r)
        if not same_design(item["number"], pinned, sha):
            continue
        c = claimant(conn, r)
        out.append({"id": r["id"], "type": r["relation"],
                    "label": sp["label"], "from": r["from_number"],
                    "from_version": c["version"], "claimant": c["how"],
                    "to": item["number"], "to_version": r["target_version"],
                    "commit": pinned, "exact": pinned == sha,
                    "result": r["result"] or "",
                    "result_note": r["result_note"] or "",
                    "measured": _measured(r["measured"]),
                    "note": r["note"] or "",
                    "ref": f"{r['from_number']}@v{c['version']}"
                           if c["version"] else r["from_number"]})
    return out


def home_facts(conn: sqlite3.Connection, item_id: int) -> dict:
    """`{"item": [...], "note": {version: [...]}}` — the item-home and note-home
    claims of one item as comparable tuples. The revision-home ones are in
    each revision's manifest facts (`manifest.db_facts`); these two homes are
    in no tree, so `--rebuild --check` and the round-trip compare them here,
    through the same projection on both sides."""
    out: dict = {"item": [], "note": {}}
    for r in conn.execute(
            "SELECT rel.*, t.number AS target_number FROM relations rel "
            "JOIN items t ON t.id=rel.target_item_id WHERE rel.item_id=? "
            "ORDER BY rel.id", (item_id,)):
        h = home(r["relation"])
        tup = (r["relation"], r["target_number"], r["target_version"] or "",
               r["note"] or "")
        if h == "item":
            out["item"].append(tup)
        elif h == "note":
            v = claimant(conn, r)["version"]
            out["note"].setdefault(v, []).append(tup)
    out["item"].sort()
    for v in out["note"]:
        out["note"][v].sort()
    return out


def _measured(text) -> dict:
    import json
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def check_result(type_: str, result: str, measured=None) -> None:
    """A result is only for a type that carries one, and is one of RESULTS."""
    if not result and not measured:
        return
    if not spec(type_)["results"]:
        raise RelationError(
            f"{type_} does not carry a test result — only "
            f"{', '.join(t for t in sorted(REGISTRY) if spec(t)['results'])} "
            "does (the registry's `results`)")
    if result and result not in RESULTS:
        raise RelationError(
            f"result must be one of {', '.join(RESULTS)} (got {result!r})")
    if measured is not None and not isinstance(measured, dict):
        raise RelationError("measured is a table of name = value")


def _item(conn: sqlite3.Connection, number: str) -> sqlite3.Row:
    """By id, current number, or a number the item used to have — the same
    resolution every other read path uses (db.resolve_item), so a link written
    against an old number still lands on the right item."""
    row = db.resolve_item(conn, number)
    if not row:
        raise RelationError(f"unknown item: {number}", 404)
    return row


def _check_kind(sp: dict, key: str, row: sqlite3.Row, type_: str, side: str) -> None:
    allowed = sp.get(key) or ()
    if allowed and row["kind"] not in allowed:
        raise RelationError(
            f"{type_} needs a {' or '.join(allowed)} on the {side} side — "
            f"{row['number']} is a {row['kind']}")


def _resolve_version(conn: sqlite3.Connection, row: sqlite3.Row, version: str,
                     mode: str, type_: str) -> str:
    """Which revision an endpoint binds. mode:

      free     item-level (derived_from): record what was passed, nothing more.
      prefer   the SUBJECT side of a revision-level link — pin it to its
               current revision if it has one. A coupon registered before its
               own first check-in is legitimate (add_coupon then checkin), and
               refusing it would push callers into linking later, i.e. never.
      require  the OBJECT side — the whole point of coupon_for is that it names
               ONE revision, so an unpinnable target is an error, not a ''.
    """
    if version:
        if not db.find_revision(conn, row["number"], version):
            raise RelationError(f"no revision {version} on {row['number']}", 404)
        return version
    if mode == "free":
        return ""
    latest = conn.execute("SELECT version FROM revisions WHERE id=?",
                          (row["latest_rev_id"],)).fetchone()
    if latest:
        return latest["version"]
    if mode == "require":
        raise RelationError(
            f"{type_} points at a specific revision, and {row['number']} has "
            "no revisions yet — check one in first, or pass the version")
    return ""


def add(conn: sqlite3.Connection, type_: str, from_number: str, to_number: str, *,
        from_version: str = "", to_version: str = "", note: str = "",
        actor: str = "", result: str = "", result_note: str = "",
        measured: dict | None = None, commit: str = "") -> dict:
    """Link two items. One code path for every type: the registry decides which
    kinds may take part and whether the versions are pinned.

    Stage 10: the claimant REVISION is recorded as `revision_id` (NULL for an
    item-home type, whatever version was passed); the target COMMIT is recorded
    beside its version when the target has a repo (`pin_commit`); a `results`
    type may carry its outcome. All three are additive columns — `off` writes
    them too, and a master reader never looks at them."""
    import json
    check_result(type_, result, measured)
    if type_ not in REGISTRY:
        raise RelationError(
            f"unknown relation type: {type_!r} — known types: "
            f"{', '.join(sorted(REGISTRY))}")
    sp = spec(type_)
    a, b = _item(conn, from_number), _item(conn, to_number)
    if a["id"] == b["id"]:
        raise RelationError(f"{from_number} cannot relate to itself")
    _check_kind(sp, "from_kinds", a, type_, "from")
    _check_kind(sp, "to_kinds", b, type_, "to")
    rev = bool(sp["rev_level"])
    from_version = _resolve_version(conn, a, from_version,
                                    "prefer" if rev else "free", type_)
    to_version = _resolve_version(conn, b, to_version,
                                  "require" if rev else "free", type_)

    # A claim's identity includes WHO claims it. For an item-home type that is
    # the item (as it always was); for a revision- or note-home type it is the
    # claimant revision — coupon v0.1.0 and coupon v0.2.0 both verifying part
    # v0.14.0 are two claims, the second carried forward by revise (stage 10).
    dup_sql = ("SELECT id FROM relations WHERE relation=? AND item_id=? AND "
               "target_item_id=? AND target_version=?")
    dup_args: tuple = (type_, a["id"], b["id"], to_version)
    if sp["home"] != "item" and from_version:
        dup_sql += " AND from_version IN (?, '')"
        dup_args += (from_version,)
    dup = conn.execute(dup_sql, dup_args).fetchone()
    if dup:
        raise RelationError(
            f"{from_number} already {sp['label']} {to_number}"
            f"{' v' + to_version if to_version else ''} (relation {dup['id']})", 409)

    rev_id = (_revision_id(conn, a["id"], from_version)
              if sp["home"] != "item" else None)
    pinned = (commit or pin_commit(b["number"], to_version)) if to_version else ""
    cur = conn.execute(
        """INSERT INTO relations (item_id, relation, target_item_id,
                                  target_version, from_version, note, actor,
                                  created_at, revision_id, target_commit,
                                  result, result_note, measured)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (a["id"], type_, b["id"], to_version, from_version, note, actor, db.now(),
         rev_id, pinned, result or "", result_note or "",
         json.dumps(measured, sort_keys=True) if measured else ""))
    db.add_event(conn, "relation.add", item_id=a["id"], actor=actor,
                 data={"type": type_, "from": from_number, "to": to_number,
                       "from_version": from_version, "to_version": to_version,
                       "note": note})
    if pinned:
        # PIN SITE (stage 11b): the claim names a commit of the TARGET's repo;
        # that repo keeps it reachable. A no-op with no repos (off mode).
        from . import gitkeep
        gitkeep.protect([(b["number"], pinned,
                          f"relation #{cur.lastrowid} {type_} "
                          f"{from_number} -> {to_number} v{to_version}")],
                        where="relations.add")
    return record(conn, cur.lastrowid)


# -- a result that arrives after release: the note overlay (stage 12) ---------

def put_result(note: dict, rec: dict, *, result: str = "",
               result_note: str = "", measured: dict | None = None) -> None:
    """Write (or clear) the `[[results]]` entry for claim `rec` in a note dict.

    The mutate half of `ops.note_write`, for a `results` claim whose claimant
    revision is FROZEN in git. Its tree keeps the claim; this records how it
    came out, keyed by the claim (type, target, target version). Nothing at
    all — no result, no sentence, no table — removes the entry, so a mistaken
    result can be withdrawn without leaving an empty annotation behind.
    """
    from . import gitnotes
    key = (rec["type"], rec["to"], rec.get("to_version") or "")
    rows = [r for r in (note.get(gitnotes.RESULTS) or [])
            if (str(r.get("type") or ""), str(r.get("item") or ""),
                str(r.get("version") or "")) != key]
    if result or result_note or measured:
        entry = {"type": rec["type"], "item": rec["to"],
                 "version": rec.get("to_version") or ""}
        if result:
            entry["result"] = result
        if result_note:
            entry["result_note"] = result_note
        if measured:
            entry["measured"] = dict(sorted(measured.items()))
        rows.append(entry)
    note[gitnotes.RESULTS] = rows


def result_overlay(note: dict) -> dict[tuple, dict]:
    """`{(type, item, version): {result, result_note, measured}}` from a note —
    what `gitindex._index_relations` lays over the tree's claims."""
    from . import gitnotes
    out: dict[tuple, dict] = {}
    for r in note.get(gitnotes.RESULTS) or []:
        if not spec(str(r.get("type") or "")).get("results"):
            continue                   # only a `results` type carries one
        out[(str(r.get("type")), str(r.get("item") or ""),
             str(r.get("version") or ""))] = {
            "result": str(r.get("result") or ""),
            "result_note": str(r.get("result_note") or ""),
            "measured": r.get("measured") if isinstance(r.get("measured"),
                                                        dict) else {}}
    return out


def record(conn: sqlite3.Connection, relation_id: int) -> dict:
    row = conn.execute(
        """SELECT r.*, f.number AS from_number, f.name AS from_name, f.kind AS from_kind,
                  t.number AS to_number, t.name AS to_name, t.kind AS to_kind
           FROM relations r JOIN items f ON f.id=r.item_id
                            JOIN items t ON t.id=r.target_item_id
           WHERE r.id=?""", (relation_id,)).fetchone()
    if not row:
        raise RelationError(f"no relation {relation_id}", 404)
    sp = spec(row["relation"])
    out = {"id": row["id"], "type": row["relation"], "label": sp["label"],
           "inverse_label": sp["inverse_label"], "rev_level": sp["rev_level"],
           "from": row["from_number"], "from_name": row["from_name"],
           "from_kind": row["from_kind"], "from_version": row["from_version"],
           "to": row["to_number"], "to_name": row["to_name"],
           "to_kind": row["to_kind"], "to_version": row["target_version"],
           "note": row["note"], "actor": row["actor"],
           "created_at": row["created_at"]}
    # Stage 10, additive: where git keeps it, which revision claims it, which
    # commit it pins, and — for a `results` type — what it found.
    keys = row.keys()
    out["home"] = sp["home"]
    out["revision_id"] = row["revision_id"] if "revision_id" in keys else None
    out["to_commit"] = (row["target_commit"] if "target_commit" in keys
                        else "") or ""
    if sp["results"]:
        out["result"] = (row["result"] if "result" in keys else "") or ""
        out["result_note"] = (row["result_note"] if "result_note" in keys
                              else "") or ""
        out["measured"] = _measured(row["measured"] if "measured" in keys
                                    else "")
    return out


def remove(conn: sqlite3.Connection, relation_id: int, *, actor: str = "") -> dict:
    rec = record(conn, relation_id)
    conn.execute("DELETE FROM relations WHERE id=?", (relation_id,))
    item = conn.execute("SELECT id FROM items WHERE number=?", (rec["from"],)).fetchone()
    db.add_event(conn, "relation.remove", item_id=item["id"] if item else None,
                 actor=actor, data={"type": rec["type"], "from": rec["from"],
                                    "to": rec["to"], "to_version": rec["to_version"]})
    return rec


def remove_between(conn: sqlite3.Connection, type_: str, from_number: str,
                   to_number: str, *, actor: str = "") -> list[dict]:
    """Unlink by the pair instead of by id — what a caller who just linked two
    numbers actually remembers."""
    a, b = _item(conn, from_number), _item(conn, to_number)
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM relations WHERE relation=? AND item_id=? AND target_item_id=?",
        (type_, a["id"], b["id"]))]
    if not ids:
        raise RelationError(
            f"{from_number} has no {type_} relation to {to_number}", 404)
    return [remove(conn, i, actor=actor) for i in ids]


# -- reads --------------------------------------------------------------------

_SIDE_SQL = """
SELECT r.id, r.relation, r.note, r.created_at, r.actor, r.item_id AS from_id,
       r.from_version, r.revision_id, r.target_commit, r.result, r.result_note,
       r.measured,
       r.{mine} AS my_version, r.{theirs} AS other_version,
       o.id AS other_item_id, o.number, o.name, o.kind,
       lr.version AS latest_version, lr.state AS latest_state
FROM relations r JOIN items o ON o.id=r.{other_id}
LEFT JOIN revisions lr ON lr.id=o.latest_rev_id
WHERE r.{my_id}=?
"""


def for_item(conn: sqlite3.Connection, item_id: int, *,
             presence=None) -> list[dict]:
    """Every relation this item takes part in, BOTH directions, each already
    labelled from the item's own point of view — so a caller renders a list and
    never reasons about direction. Grouping key is `label`.

    Each row carries `comments`: how many context entries are attached to THAT
    LINK (plm/context.py). A link is a thing people argue about — "why is this
    a bench test and not a product?" — so it is a context target like any
    other; the count is here so a reader knows to expand before fetching.

    `thumb` is the other side's render URL, or None when that revision has no
    geometry (plm/thumbs.py). It used to be an unconditional URL, which made
    every coupon/document link a guaranteed 404 in the browser. Pass `presence`
    to share one memo with the rest of the payload."""
    from . import thumbs               # local: thumbs imports db, not relations
    p = presence or thumbs.Presence(conn)
    out: list[dict] = []
    for direction, sql in (
            ("out", _SIDE_SQL.format(mine="from_version", theirs="target_version",
                                     other_id="target_item_id", my_id="item_id")),
            ("in", _SIDE_SQL.format(mine="target_version", theirs="from_version",
                                    other_id="item_id", my_id="target_item_id"))):
        for row in conn.execute(sql, (item_id,)):
            sp = spec(row["relation"])
            out.append({
                "id": row["id"], "type": row["relation"], "direction": direction,
                "label": sp["label"] if direction == "out" else sp["inverse_label"],
                "rev_level": sp["rev_level"], "hint": sp["hint"],
                "my_version": row["my_version"], "version": row["other_version"],
                "item_id": row["other_item_id"],
                "number": row["number"], "name": row["name"], "kind": row["kind"],
                "latest_version": row["latest_version"],
                "state": row["latest_state"],
                "thumb": p.url(row["number"], row["other_version"] or "",
                               kind=row["kind"]),
                "note": row["note"], "actor": row["actor"],
                "created_at": row["created_at"],
                **_home_keys(conn, row, sp, direction)})
    from . import context as ctx   # local: context resolves relations too
    counts = ctx.counts(conn, "relation", [r["id"] for r in out])
    for r in out:
        r["comments"] = counts.get(r["id"], 0)
    out.sort(key=lambda r: (r["label"], r["number"], r["version"] or ""))
    return out


def _home_keys(conn: sqlite3.Connection, row, sp: dict, direction: str) -> dict:
    """Stage 10's additive keys on a `for_item` row: where the claim is
    RECORDED, which revision makes it, the commit it pins, and whether this row
    is the record or the derived inverse — "verified by GL-CPN-0001 v0.1.0,
    derived from GL-CPN-0001's repo". The UI and the MCP say it in those words
    so nobody tries to record the inverse."""
    c = claimant(conn, {"relation": row["relation"], "item_id": row["from_id"],
                        "revision_id": row["revision_id"],
                        "from_version": row["from_version"],
                        "created_at": row["created_at"]})
    out = {"home": sp["home"], "claimant_version": c["version"],
           "claimant": c["how"],
           "derived": direction == "in",
           "recorded_in": HOMES[sp["home"]]["label"],
           "to_commit": row["target_commit"] or ""}
    if sp["results"]:
        out["result"] = row["result"] or ""
        out["result_note"] = row["result_note"] or ""
        out["measured"] = _measured(row["measured"])
    return out


def related_numbers(conn: sqlite3.Connection, number: str, *, type_: str = "",
                    direction: str = "both") -> list[str]:
    """The numbers related to one item — the generic filter behind
    'the coupons of PRT-1011' (type_='coupon_for', direction='in')."""
    row = _item(conn, number)
    rels = for_item(conn, row["id"])
    return [r["number"] for r in rels
            if (not type_ or r["type"] == type_)
            and (direction in ("both", "") or r["direction"] == direction)]
