"""Fabrication: routings, raw stock, and the cut list.

The point of this module, stated once so nobody has to infer it: **plm should
turn fabricated-part structure into actionable cut/drill/notch/weld work, not
classify every non-printed child as something to buy.** A weldment made of
repeatable pieces of 1x2x0.120 tube is not "unknown" and it is not "purchased" —
it is a stack of CUT operations against a purchasable stick of steel, and the
questions it has to answer are: how long, how many, off what, with how much
kerf, and how many sticks do I buy.

== FOUR THINGS, AND WHERE EACH ONE LIVES ==

1. **A ROUTING BELONGS TO A REVISION.** Geometry changes change the ops: shorten
   a rail and the CUT length changes; move a receiver and the NOTCH datums
   change. So `operations` rows hang off `revision_id`, they carry forward on
   revise() exactly like files and attributes (ops.revise), and nothing here is
   item-level. That is the same argument attributes.py makes for mass and
   material, applied to work instead of properties.

   ONE TABLE, NOT A ROUTING/OPERATION/RESOURCE TRIO. The shape of an operation
   is "a verb, a place in the order, and some numbers"; the numbers differ per
   verb, which is exactly a JSON params column with a registry gloss — the shape
   `attributes` already took and the shape `relations` already took. A grown-up
   PLM adds a work-centre master, standard times, setup times and a resource
   model on top of that. Deliberately NOT built: there is one garage with one
   chop saw, one drill press, one grinder and one MIG welder, so `work_center`
   is a text hint until there are two of something, and a fabricated standard
   time on a controlled record is worse than no standard time at all. When a
   second machine appears, `work_center` earns a table and this surface does not
   change — that is the seam.

2. **THE OP VOCABULARY IS A REGISTRY DICT** (OPERATIONS below), for the reason
   relations.REGISTRY is one: adding a verb is a dict entry, not a feature and
   not a migration. The plasma lane (devops/docs/projects/2026-08-13-cnc-plasma)
   adds CUT_PROFILE and SCRIBE as members of THIS set — they are already
   registered here so that lane needs no schema change at all.

   GATE ON THE VERB, GLOSS ON THE PARAMS. Unlike attributes (pure gloss) and
   like relations (pure gate), this registry is both, on purpose: a typo'd verb
   (`NOCH`) is an operation no shop surface will ever route, so the name is
   validated; a param name we have not thought of yet is a fact somebody
   measured, so params are open and merely reported as `extra`.

3. **RAW STOCK IS A `purchased` ITEM.** Not a new kind, not a new number class.
   A 10-foot stick of 1x2x0.120 is BOUGHT: it has a supplier, a price and a
   purchase length, it belongs in the BUY list, and it must keep appearing in
   /api/products. A new kind would have to be added to _build_plan's allow-list
   to be bought at all, and the moment it was forgotten there the steel would
   silently vanish from purchasing — the exact failure config.NON_STRUCTURAL_KINDS
   exists to make impossible. House precedent agrees: "the role is a relation,
   not a kind, and never a number" (docs/PLM_RELATIONS.md).

   What makes a purchased item STOCK is data on its revision, and it is ordinary
   attributes (STOCK_ATTRS): stock_form, stock_profile, stock_section,
   stock_length, kerf, remnant_min. Those are a GLOSS, so a typo lands — and the
   NEST refuses it by name, at the point where it matters, instead of the write
   path pretending to be a schema. Permissive store, strict computation.

4. **THE CUT PART -> STOCK LINK IS A RELATION** (`cut_from`, item-level; see
   relations.REGISTRY). A relation and not a column, because the registry is the
   estate's answer to "add a link type" and because relations.for_item already
   returns BOTH directions, already labelled — "cut from" on the part, "stock
   for" on the steel. That is the traceability requirement (weldment -> finished
   cut part -> raw stock) satisfied in both directions with no new read path, no
   new UI card and no new MCP tool. A column would answer one direction and
   would need all three.

== `process`, NOT `disposition` ==

`disposition` is BURNED: docs/PLM_CHANGES.md uses it for the ECN affected-item
concept (pending|revise|use_as_is|no_impact). The make/buy/print fact is
`process`, and it is a REVISION column (see PROCESSES) because it DISPATCHES
_build_plan and dispatch keys belong where the query can see them — the same
argument that keeps material and mass_g in columns.

⭐ THERE IS NO `process='buy'`, AND THAT IS THE DESIGN. "Buy it" is already
answered by `kind` (purchased/hardware). A second way to say the same thing is
two homes for one fact, which is how registries rot. So the BUILD PLAN gains a
MAKE bucket while the FIELD only has to separate print from make, and both of
_build_plan's branches stay allow-lists on both axes.

== UNITS: HIS STOCK IS IMPERIAL AND IT STAYS IMPERIAL ==

86-inch rails, 1-inch Sch40, 0.125-inch kerf. attributes.py's rule applies
without exception: carry the unit, never default it. A CUT with a length and no
unit is REFUSED and the refusal names the part — "86 what?" is not a question a
cut list may answer by guessing. Conversion happens in exactly ONE function
(`convert`) and only when a nest group mixes units, and it converts INTO THE
STOCK'S OWN UNIT, because the stick is what you buy and the purchase order is
the thing that has to be right.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from . import db

# =============================================================================
# THE REGISTRIES
# =============================================================================

# -- process: how this revision comes into existence --------------------------
# Values are validated (this dispatches the build plan). See the module
# docstring for why `buy` is deliberately absent.
PROCESSES: dict[str, dict] = {
    "print": {
        "label": "3D print",
        "hint": "additive: the revision's own mesh goes to a slicer. This is "
                "also what an unset `process` means for a part, so every "
                "existing lantern part keeps printing with no data change.",
        "kinds": ("part",),
    },
    "make": {
        "label": "Make (fabricate)",
        "hint": "subtractive/joining: cut, drill, notch, cope, bend, weld, "
                "finish. Carries a ROUTING (operations) and, for a cut part, a "
                "cut_from link to the raw stock it is cut off.",
        "kinds": ("part", "assembly"),
    },
}

# -- end treatments: what happens at each end of a cut ------------------------
# A gloss on the CUT operation's end_a/end_b, not a gate: "square" and "miter"
# cover his saw, and the next end treatment somebody meets should not need a
# code change to be recorded.
END_TREATMENTS: dict[str, str] = {
    "square": "cut square, 90 deg to the axis",
    "miter": "angled cut; give angle_a/angle_b in degrees",
    "cope": "saddle-cut to wrap a mating round section (see the COPE op for the "
            "operation itself; this records that the END is coped)",
    "bevel": "edge prepared for a groove weld",
    "open": "left long on purpose — a downstream op sets the finished length",
    "capped": "closed with a purchased cap (the cap is a BOM line, not an op)",
}

# -- stock forms: how a piece of raw material is shaped ------------------------
# `linear` nests in 1-D (this module). `sheet` nests in 2-D and that nest lives
# in the shop-floor service, NOT here — a 2-D nest is bound to one physical remnant on one
# machine at one moment, and plm's revisions are immutable
# (devops/docs/projects/2026-08-13-cnc-plasma/DESIGN.md 9.1). plm still owns the
# PURCHASING question for sheet: how many sheets, which is a count, not a layout.
STOCK_FORMS: dict[str, dict] = {
    "linear": {"label": "Linear stock (tube, pipe, bar, angle)",
               "nest": "1-D, in plm (fab.nest_linear)"},
    "sheet": {"label": "Sheet / plate",
              "nest": "2-D, in the shop-floor service — plm counts sheets, it does not lay "
                      "parts out on one"},
}

# -- stock attributes, on the purchased item's revision -----------------------
# Registered in attributes.WELL_KNOWN for display; listed here because this is
# the module that READS them, and a reader that names its inputs is a contract.
STOCK_ATTRS = ("stock_form", "stock_profile", "stock_section", "stock_length",
               "kerf", "remnant_min")

# -- operations: the routing vocabulary ---------------------------------------
#   label          how a human surface titles it
#   consumes_stock this op is where a piece of raw stock becomes this part.
#                  EXACTLY ONE per revision is allowed: a part is one piece of
#                  stock, and two of them is two parts (that is what a BOM is
#                  for). The cut list is built from these ops and nothing else.
#   lane           linear | sheet | any — which fabrication lane the verb serves
#   params         the well-known params: name -> {kind, required?, values?}
#                  kind: dimension  {"value": n, "unit": "in"} — UNIT REQUIRED
#                        dimensions {"values": [...], "unit": "in"} — a set at
#                                   one unit (hole positions, notch datums)
#                        number     a bare count
#                        text       free text
#                        bool       true/false
#                  Unknown params are ALLOWED and reported as `extra`.
#   hint           what the verb means, for the agent reading the registry
OPERATIONS: dict[str, dict] = {
    "CUT": {
        "label": "Cut to length",
        "consumes_stock": True,
        "lane": "linear",
        "params": {
            "length": {"kind": "dimension", "required": True},
            "kerf": {"kind": "dimension"},
            "end_a": {"kind": "text", "values": tuple(END_TREATMENTS)},
            "end_b": {"kind": "text", "values": tuple(END_TREATMENTS)},
            "angle_a": {"kind": "angle"},
            "angle_b": {"kind": "angle"},
            "allowance": {"kind": "dimension"},
        },
        "hint": "THE op that turns raw stock into this part: one piece, one "
                "finished length, two ends. `length` is the FINISHED length and "
                "needs a unit. `kerf` overrides the stock's own kerf when this "
                "cut is different (a miter through 1x2 eats more than a square "
                "chop). `allowance` is extra length left on for a downstream "
                "cope/fit — it is CONSUMED from the stick, so the nest charges "
                "length + allowance + kerf.",
    },
    "DRILL": {
        "label": "Drill",
        "consumes_stock": False,
        "lane": "any",
        "params": {
            "dia": {"kind": "dimension", "required": True},
            "positions": {"kind": "dimensions"},
            "datum": {"kind": "text"},
            "count": {"kind": "number"},
            "axis": {"kind": "text"},
            "through": {"kind": "bool"},
        },
        "hint": "Holes. `positions` is a set of distances at ONE unit and "
                "`datum` says what they are measured FROM — a hole datum with "
                "no origin is not a hole datum, it is a number. `axis` records "
                "which face/rotation ('0/90 deg' for a receiver drilled on two "
                "perpendicular axes).",
    },
    "NOTCH": {
        "label": "Notch",
        "consumes_stock": False,
        "lane": "any",
        "params": {
            "profile": {"kind": "text", "required": True},
            "positions": {"kind": "dimensions"},
            "datum": {"kind": "text"},
            "depth": {"kind": "dimension"},
            "count": {"kind": "number"},
        },
        "hint": "Material removed from a face so something else sits INTO it — "
                "a receiver saddle in a rail. `profile` says what shape and for "
                "what ('saddle for 1.660 in OD'); `positions`/`datum` say "
                "where, the same way DRILL does.",
    },
    "COPE": {
        "label": "Cope / fishmouth",
        "consumes_stock": False,
        "lane": "any",
        "params": {
            "mates": {"kind": "text", "required": True},
            "end": {"kind": "text", "values": ("a", "b", "both")},
            "finished_length": {"kind": "dimension"},
            "angle": {"kind": "angle"},
        },
        "hint": "An END shaped to wrap a round mating section. Distinct from "
                "NOTCH because it is an END treatment and it CHANGES THE "
                "FINISHED LENGTH: the blank is cut long (CUT.length + "
                "allowance) and `finished_length` is the centre-to-centre "
                "dimension after coping. The cut list nests the BLANK, because "
                "the blank is what comes off the stick.",
    },
    "BEND": {
        "label": "Bend",
        "consumes_stock": False,
        "lane": "any",
        "params": {
            "angle": {"kind": "angle", "required": True},
            "radius": {"kind": "dimension"},
            "position": {"kind": "dimension"},
            "datum": {"kind": "text"},
        },
        "hint": "Formed, not cut. Registered because it is unambiguously a "
                "fabrication verb for tube work; no CCR part uses it yet. NOTE "
                "for whoever first does: a bend changes developed length, so "
                "CUT.length on a bent part is the FLAT length and the note "
                "should say so.",
    },
    "DEBURR": {
        "label": "Deburr",
        "consumes_stock": False,
        "lane": "any",
        "params": {"detail": {"kind": "text"}},
        "hint": "Its own step because it is its own station (file, flap disc) "
                "and because his own cut list says 'square/deburr' — two verbs "
                "in one cell is the thing this table is replacing.",
    },
    "WELD": {
        "label": "Weld",
        "consumes_stock": False,
        "lane": "any",
        "params": {
            "method": {"kind": "text"},
            "joint": {"kind": "text"},
            "size": {"kind": "dimension"},
            "length": {"kind": "dimension"},
            "count": {"kind": "number"},
            "fixture": {"kind": "text"},
        },
        "hint": "⭐ A WELD BELONGS TO THE WELDMENT, NOT TO THE PIECE. The rear "
                "rack's welds are operations of CCR-ASM-0003 (process='make'), "
                "because the joint does not exist until the pieces are "
                "together; 'WELD in rear rack' written on the rail is the piece "
                "claiming to know about an assembly. WELDMENT MEMBERSHIP IS THE "
                "BOM and needs no relation — what makes the joint a weld rather "
                "than a bolt is this operation.",
    },
    "FINISH": {
        "label": "Finish",
        "consumes_stock": False,
        "lane": "any",
        "params": {"spec": {"kind": "text", "required": True},
                   "coverage": {"kind": "text"}},
        "hint": "Coating/passivation, and it is almost always an operation of "
                "the WELDMENT — you paint after you weld. `spec` is the real "
                "spec ('wire wheel, self-etching primer, 2 coats enamel'), not "
                "'paint'.",
    },
    # -- the sheet lane (CNC plasma). Registered HERE, not there. -------------
    # devops/docs/projects/2026-08-13-cnc-plasma/DESIGN.md 9.2 assigns the
    # operation vocabulary to and names these two as members of it. They
    # are in the dict today so that lane ships a consumer, not a migration.
    "CUT_PROFILE": {
        "label": "Cut profile (2-D)",
        "consumes_stock": True,
        "lane": "sheet",
        "params": {
            "flat_file": {"kind": "text"},
            "thickness": {"kind": "dimension"},
            "pierce_count": {"kind": "number"},
            "cut_length": {"kind": "dimension"},
            "kerf": {"kind": "dimension"},
        },
        "hint": "A 2-D contour cut from sheet — the sheet-lane twin of CUT, and "
                "the second consumes_stock verb. Its input is a role='flat' "
                "file on the revision. plm owns the DEFINITION and the SHEET "
                "COUNT; the 2-D layout onto a specific physical remnant is "
                "the shop-floor service's, because a nest is bound to one sheet on one "
                "machine at one moment and plm's revisions are immutable.",
    },
    "SCRIBE": {
        "label": "Scribe",
        "consumes_stock": False,
        "lane": "sheet",
        "params": {"content": {"kind": "text"}, "detail": {"kind": "text"}},
        "hint": "Marking rather than cutting — bend lines, part numbers, hole "
                "centres — on the plasma table's scriber (M8/M9 with an XY "
                "offset, not a tool change).",
    },
}

# Length units, as multiples of a millimetre. The ONLY conversion table in plm.
# Deliberately short: these are the units a steel yard and a machine actually
# quote. An unknown unit is a refusal, never a guess.
_MM: dict[str, float] = {
    "mm": 1.0, "cm": 10.0, "m": 1000.0,
    "in": 25.4, '"': 25.4, "inch": 25.4, "inches": 25.4,
    "ft": 304.8, "'": 304.8, "foot": 304.8, "feet": 304.8,
    "yd": 914.4,
}
# The spelling each alias normalizes to, so a cut list does not show three
# words for an inch.
_CANON = {"inch": "in", "inches": "in", '"': "in",
          "foot": "ft", "feet": "ft", "'": "ft"}
# Angles are dimensions too (a 45-degree miter) but they are never converted:
# one unit, and degrees is the one every saw scale is marked in.
_ANGLE = {"deg", "degree", "degrees", "°"}

DEFAULT_KERF = {"value": 0.125, "unit": "in"}
DEFAULT_REMNANT_MIN = {"value": 12.0, "unit": "in"}


class FabError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# =============================================================================
# UNITS — one place, and it refuses rather than guesses
# =============================================================================

def canon_unit(unit: str) -> str:
    u = (unit or "").strip()
    return _CANON.get(u.lower(), u.lower())


def convert(value: float, frm: str, to: str) -> float:
    """Length conversion, and the only one in plm. Rounded to 4 decimals so the
    same input always produces the same output byte-for-byte (the estate's
    hashing discipline applies to generated plans, not just files)."""
    a, b = canon_unit(frm), canon_unit(to)
    if not a or not b:
        raise FabError("cannot convert a length with no unit — pass the unit "
                       "with the value, e.g. {\"value\": 86, \"unit\": \"in\"}")
    for u in (a, b):
        if u not in _MM:
            raise FabError(
                f"unknown length unit {u!r} — known: "
                f"{', '.join(sorted({canon_unit(k) for k in _MM}))}")
    if a == b:
        return round(float(value), 4)
    return round(float(value) * _MM[a] / _MM[b], 4)


def _dim(raw: Any, what: str, *, where: str = "",
         angle: bool = False) -> tuple[float, str]:
    """One {value, unit} dimension, or a refusal that names the offender.
    THE UNIT IS NOT OPTIONAL — see the module docstring."""
    at = f" on {where}" if where else ""
    if not isinstance(raw, dict):
        raise FabError(
            f"{what}{at} must be {{\"value\": n, \"unit\": \"in\"}} — a bare "
            f"{raw!r} does not say what it is measured in, and a cut list may "
            f"not guess ({raw!r} what?)")
    if "value" not in raw:
        raise FabError(f"{what}{at} has no `value`")
    try:
        val = float(raw["value"])
    except (TypeError, ValueError):
        raise FabError(f"{what}{at}: {raw.get('value')!r} is not a number") from None
    unit = canon_unit(str(raw.get("unit", "")))
    if not unit:
        raise FabError(
            f"{what}{at} needs a unit — {val:g} what? (his stock is imperial: "
            f"\"in\"; the machine world is \"mm\". plm carries the unit and "
            f"never defaults it — see plm/attributes.py)")
    _check_unit(unit, what, at, angle=angle)
    return val, unit


def _check_unit(unit: str, what: str, at: str, *, angle: bool = False) -> None:
    """The unit is GATED, not glossed — unlike an attribute's unit, which is
    free text somebody typed. plm owns the conversion table (`convert`), so a
    unit plm cannot convert is a length no nest can use; catching it at the write
    is the difference between a refusal on the routing you are editing and a
    refusal on a cut list three days later. An ANGLE param takes degrees and
    nothing else — degrees is the one unit every saw scale is marked in, and an
    angle is never converted."""
    if unit in (_ANGLE if angle else _MM):
        return
    raise FabError(
        f"{what}{at}: plm cannot work in {unit!r} here — "
        + (f"an angle takes {', '.join(sorted(_ANGLE))}."
           if angle else
           f"a length takes {', '.join(sorted({canon_unit(k) for k in _MM}))} "
           f"(an angle takes deg, on the angle_* params)."))


def _dims(raw: Any, what: str, *, where: str = "") -> tuple[list[float], str]:
    """A SET of dimensions at one unit: {"values": [1.4, 7.5], "unit": "in"}."""
    at = f" on {where}" if where else ""
    if not isinstance(raw, dict) or "values" not in raw:
        raise FabError(
            f"{what}{at} must be {{\"values\": [..], \"unit\": \"in\"}} — one "
            f"unit for the set, because hole datums are measured off one datum "
            f"in one system")
    unit = canon_unit(str(raw.get("unit", "")))
    if not unit:
        raise FabError(f"{what}{at} needs a unit")
    _check_unit(unit, what, at)
    out = []
    for v in raw["values"]:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            raise FabError(f"{what}{at}: {v!r} is not a number") from None
    return out, unit


# =============================================================================
# THE REGISTRY, AS DATA
# =============================================================================

def op_spec(op: str) -> dict:
    """The registry entry for a verb. Unlike relations.spec this does NOT
    degrade for unknown names, because add_operations validates the verb — an
    unknown op in the table could only come from a newer build, and reading it
    back as a gloss is still better than dropping it."""
    if op in OPERATIONS:
        return {"op": op, "known": True, **OPERATIONS[op]}
    return {"op": op, "known": False, "label": op.replace("_", " ").title(),
            "consumes_stock": False, "lane": "any", "params": {},
            "hint": "not in this build's registry"}


def registry() -> dict:
    """Everything a caller needs to build a UI or a prompt from: the verbs, the
    process values, the end treatments, the stock forms and the stock
    attributes. One read, because they are one vocabulary."""
    return {
        "operations": [{"op": o, **OPERATIONS[o]} for o in OPERATIONS],
        "processes": [{"process": p, **PROCESSES[p]} for p in PROCESSES],
        "end_treatments": [{"value": v, "hint": h}
                           for v, h in END_TREATMENTS.items()],
        "stock_forms": [{"form": f, **STOCK_FORMS[f]} for f in STOCK_FORMS],
        "stock_attributes": list(STOCK_ATTRS),
        "units": sorted({canon_unit(u) for u in _MM}),
        "defaults": {"kerf": DEFAULT_KERF, "remnant_min": DEFAULT_REMNANT_MIN},
    }


# =============================================================================
# process — the make/buy/print word
# =============================================================================

def prepare_process(conn: sqlite3.Connection, rev_id: int, process: str):
    """(revision row, canonical process) — or the refusal. Split out for the
    same reason `prepare_routing` is: the git path asks these questions before
    it writes a commit, and there is one set of answers."""
    process = (process or "").strip().lower()
    if process and process not in PROCESSES:
        raise FabError(
            f"unknown process {process!r} — known: {', '.join(PROCESSES)}. "
            "There is deliberately no 'buy': that is what kind="
            "purchased/hardware already says, and one fact gets one home.")
    rev = conn.execute(
        """SELECT r.*, i.number, i.kind, i.id AS iid FROM revisions r
           JOIN items i ON i.id=r.item_id WHERE r.id=?""", (rev_id,)).fetchone()
    if not rev:
        raise FabError(f"no revision {rev_id}", 404)
    if rev["state"] != "DRAFT":
        raise FabError(
            f"{rev['number']} v{rev['version']} is {rev['state']}, not a DRAFT "
            "— `process` is manufacturing DEFINITION, not a measurement, so it "
            "only changes on a working revision. Revise ↑ first.", 409)
    if process:
        kinds = PROCESSES[process]["kinds"]
        if rev["kind"] not in kinds:
            raise FabError(
                f"process={process!r} applies to {' or '.join(kinds)} — "
                f"{rev['number']} is a {rev['kind']}"
                + (". A purchased/hardware item is bought by definition; that "
                   "is what its kind says." if rev["kind"] in
                   ("purchased", "hardware") else ""))
    return rev, process


def set_process(conn: sqlite3.Connection, rev_id: int, process: str, *,
                actor: str = "") -> dict:
    """Set how a revision comes into existence. DRAFT-ONLY, and that is the
    difference from attributes: an attribute is a MEASUREMENT (you can weigh a
    released part), while `process` is DESIGN INTENT and changing it on a
    released revision changes what was approved. Same discipline as the five
    BOM verbs."""
    rev, process = prepare_process(conn, rev_id, process)
    conn.execute("UPDATE revisions SET process=? WHERE id=?", (process, rev_id))
    db.add_event(conn, "process", item_id=rev["iid"], revision_id=rev_id,
                 actor=actor, data={"process": process})
    return {"number": rev["number"], "version": rev["version"],
            "process": process}


def process_of(conn: sqlite3.Connection, rev_id: int) -> str:
    row = conn.execute("SELECT process FROM revisions WHERE id=?",
                       (rev_id,)).fetchone()
    return (row["process"] if row else "") or ""


# =============================================================================
# operations — the routing
# =============================================================================

def _validate(op: str, params: dict) -> dict:
    """Gate the verb, gloss the params. Returns {params, extra, warnings}."""
    if op not in OPERATIONS:
        raise FabError(
            f"unknown operation {op!r} — known verbs: "
            f"{', '.join(OPERATIONS)}. The vocabulary is a registry dict in "
            "plm/fab.py: if you need a new verb, add the entry (and say why in "
            "docs/PLM_DATA_MODEL.md) rather than smuggling it into a note.")
    if not isinstance(params, dict):
        raise FabError(f"{op} params must be an object, got {type(params).__name__}")
    spec = OPERATIONS[op]["params"]
    warnings: list[str] = []
    for name, ps in spec.items():
        if ps.get("required") and name not in params:
            raise FabError(
                f"{op} needs `{name}` — {OPERATIONS[op]['hint'].split('.')[0]}")
    for name, raw in sorted(params.items()):
        ps = spec.get(name)
        if not ps:
            continue                      # open params: the gloss, not the gate
        kind = ps["kind"]
        if kind in ("dimension", "angle"):
            _dim(raw, f"`{name}`", where=op, angle=kind == "angle")
        elif kind == "dimensions":
            _dims(raw, f"`{name}`", where=op)
        elif kind == "number":
            try:
                float(raw)
            except (TypeError, ValueError):
                raise FabError(f"{op}.{name} must be a number, got {raw!r}") from None
        elif kind == "bool" and not isinstance(raw, bool):
            raise FabError(f"{op}.{name} must be true or false, got {raw!r}")
        vals = ps.get("values")
        if vals and kind == "text" and str(raw) not in vals:
            # A GLOSS, not a gate: end treatments we have not met are real.
            warnings.append(
                f"{op}.{name}={raw!r} is not one of the well-known values "
                f"({', '.join(vals)}) — recorded as-is")
    extra = sorted(set(params) - set(spec))
    return {"params": params, "extra": extra, "warnings": warnings}


def prepare_routing(conn: sqlite3.Connection, rev_id: int,
                    operations: list[dict], *, replace: bool = True) -> dict:
    """Everything `set_routing` decides before it writes a row: the revision is
    a DRAFT, every verb is in the registry, every parameter carries a unit it
    understands, and a cut part is one piece of stock.

    Split out so the GIT path asks exactly the same questions (ops.set_routing
    commits `[[routing]]` to `plm.toml` and lets the indexer write the rows) —
    one set of refusals, whichever door the routing came through.
    """
    rev = conn.execute(
        """SELECT r.id, r.state, r.version, i.number, i.kind, i.id AS iid
           FROM revisions r JOIN items i ON i.id=r.item_id WHERE r.id=?""",
        (rev_id,)).fetchone()
    if not rev:
        raise FabError(f"no revision {rev_id}", 404)
    if rev["state"] != "DRAFT":
        raise FabError(
            f"{rev['number']} v{rev['version']} is {rev['state']} — a routing "
            "is manufacturing definition, not a measurement. Revise ↑ first, "
            "so the change lands on a revision that can be approved.", 409)
    if not isinstance(operations, list):
        raise FabError("operations must be a list of {op, params?, "
                       "work_center?, notes?}")

    prepared, warnings = [], []
    for i, raw in enumerate(operations):
        if not isinstance(raw, dict):
            raise FabError(f"operation {i} must be an object, got {raw!r}")
        op = str(raw.get("op", "")).strip().upper()
        if not op:
            raise FabError(f"operation {i} has no `op` — one of "
                           f"{', '.join(OPERATIONS)}")
        res = _validate(op, raw.get("params") or {})
        warnings += res["warnings"]
        prepared.append({
            "op": op, "params": res["params"],
            "work_center": str(raw.get("work_center", "") or ""),
            "notes": str(raw.get("notes", "") or ""),
        })

    # ONE stock-consuming op per revision. A part is ONE piece of stock; two
    # CUTs is two parts, and that is what a BOM is for. Without this the cut
    # list would silently double-count a stick.
    kept = [p for p in prepared if OPERATIONS[p["op"]]["consumes_stock"]]
    if replace and len(kept) > 1:
        raise FabError(
            f"{rev['number']} v{rev['version']} would have "
            f"{len(kept)} stock-consuming operations "
            f"({', '.join(p['op'] for p in kept)}) — a cut part is ONE piece of "
            "raw stock. Two pieces is two parts: give the second one its own "
            "number and put both in the weldment's BOM.")
    return {"rev": rev, "prepared": prepared, "warnings": warnings,
            "stock_ops": kept}


def set_routing(conn: sqlite3.Connection, rev_id: int,
                operations: list[dict], *, actor: str = "",
                replace: bool = True) -> dict:
    """Write a revision's routing. A ROUTING IS A LIST, so it is edited as a
    list: `replace` (the default) swaps the whole sequence, which is what an
    agent regenerating from CAD actually wants and what makes the write
    idempotent. `replace=False` appends after the current last seq.

    DRAFT-only, for the same reason the BOM verbs are: a routing is
    manufacturing definition, and a released revision's routing is what somebody
    approved."""
    ready = prepare_routing(conn, rev_id, operations, replace=replace)
    rev, prepared, warnings = ready["rev"], ready["prepared"], ready["warnings"]
    kept = ready["stock_ops"]

    base = 0
    if replace:
        conn.execute("DELETE FROM operations WHERE revision_id=?", (rev_id,))
    else:
        row = conn.execute("SELECT MAX(seq) AS m FROM operations WHERE revision_id=?",
                           (rev_id,)).fetchone()
        base = int(row["m"] or 0)
        if kept and stock_op(conn, rev_id):
            raise FabError(
                f"{rev['number']} v{rev['version']} already has a "
                "stock-consuming operation — a cut part is one piece of stock.")
    for n, p in enumerate(prepared, start=1):
        conn.execute(
            """INSERT INTO operations (revision_id, seq, op, params,
                                       work_center, notes, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (rev_id, base + n * 10, p["op"], json.dumps(p["params"],
                                                        sort_keys=True),
             p["work_center"], p["notes"], db.now()))
    db.add_event(conn, "routing", item_id=rev["iid"], revision_id=rev_id,
                 actor=actor, data={"replace": replace,
                                    "ops": [p["op"] for p in prepared]})
    return {"number": rev["number"], "version": rev["version"],
            "operations": get_routing(conn, rev_id), "warnings": warnings}


def get_routing(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    """The routing, in order, each row already glossed from the registry so no
    caller re-implements it."""
    out = []
    for row in conn.execute(
            "SELECT * FROM operations WHERE revision_id=? ORDER BY seq",
            (rev_id,)):
        sp = op_spec(row["op"])
        try:
            params = json.loads(row["params"] or "{}")
        except ValueError:
            params = {}
        out.append({"id": row["id"], "seq": row["seq"], "op": row["op"],
                    "label": sp["label"], "known": sp["known"],
                    "consumes_stock": bool(sp["consumes_stock"]),
                    "lane": sp["lane"], "params": params,
                    "work_center": row["work_center"], "notes": row["notes"],
                    "updated_at": row["updated_at"],
                    "summary": op_summary(row["op"], params)})
    return out


def stock_op(conn: sqlite3.Connection, rev_id: int) -> dict | None:
    """The one operation on this revision that consumes raw stock, if any. The
    whole cut list is built from these and nothing else."""
    for o in get_routing(conn, rev_id):
        if o["consumes_stock"]:
            return o
    return None


def copy(conn: sqlite3.Connection, from_rev: int, to_rev: int) -> int:
    """Carry the routing forward onto a new revision — the same dataset
    semantics files and attributes have (ops.revise). A new DRAFT starts life as
    a copy of what it was seeded from; the ops are then corrected where the
    geometry actually moved."""
    rows = conn.execute(
        "SELECT seq,op,params,work_center,notes FROM operations "
        "WHERE revision_id=? ORDER BY seq", (from_rev,)).fetchall()
    for r in rows:
        conn.execute(
            """INSERT INTO operations (revision_id,seq,op,params,work_center,
                                       notes,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(revision_id,seq) DO NOTHING""",
            (to_rev, r["seq"], r["op"], r["params"], r["work_center"],
             r["notes"], db.now()))
    return len(rows)


def _fmt(v: float) -> str:
    return f"{v:g}"


def op_summary(op: str, params: dict) -> str:
    """One readable line per operation — what the old free-text
    `fabrication_operations` attribute was trying to be, except DERIVED from
    structured params instead of typed by hand. Never parsed; display only."""
    p = params or {}

    def dim(name: str) -> str:
        raw = p.get(name)
        if isinstance(raw, dict) and "value" in raw:
            return f"{_fmt(float(raw['value']))} {canon_unit(str(raw.get('unit','')))}".strip()
        return ""

    def dims(name: str) -> str:
        raw = p.get(name)
        if isinstance(raw, dict) and "values" in raw:
            u = canon_unit(str(raw.get("unit", "")))
            return "/".join(_fmt(float(v)) for v in raw["values"]) + (f" {u}" if u else "")
        return ""

    bits: list[str] = []
    if op == "CUT":
        bits.append(f"to {dim('length')}" if dim("length") else "to length")
        ends = [e for e in (p.get("end_a"), p.get("end_b")) if e]
        if ends:
            bits.append("ends " + "/".join(str(e) for e in ends))
        for a in ("angle_a", "angle_b"):
            if dim(a):
                bits.append(f"{a[-1]} at {dim(a)}")
        if dim("allowance"):
            bits.append(f"+{dim('allowance')} allowance")
        if dim("kerf"):
            bits.append(f"kerf {dim('kerf')}")
    elif op == "DRILL":
        n = p.get("count") or (len(p["positions"]["values"])
                               if isinstance(p.get("positions"), dict)
                               and "values" in p["positions"] else "")
        bits.append(f"{_fmt(float(n))} ×" if n not in (None, "") else "")
        bits.append(f"{dim('dia')} dia" if dim("dia") else "")
        if dims("positions"):
            bits.append(f"at {dims('positions')}")
        if p.get("datum"):
            bits.append(f"from {p['datum']}")
        if p.get("axis"):
            bits.append(f"axes {p['axis']}")
        if p.get("through"):
            bits.append("through")
    elif op in ("NOTCH", "COPE"):
        bits.append(str(p.get("profile") or p.get("mates") or ""))
        if p.get("end"):
            bits.append("both ends" if p["end"] == "both" else f"end {p['end']}")
        if dims("positions"):
            bits.append(f"at {dims('positions')}")
        if p.get("datum"):
            bits.append(f"from {p['datum']}")
        if dim("depth"):
            bits.append(f"{dim('depth')} deep")
        if dim("finished_length"):
            bits.append(f"finished {dim('finished_length')}")
    elif op == "BEND":
        bits += [f"{dim('angle')}" if dim("angle") else "",
                 f"r{dim('radius')}" if dim("radius") else "",
                 f"at {dim('position')}" if dim("position") else ""]
    elif op == "WELD":
        bits += [str(p.get("joint") or ""), str(p.get("method") or "")]
        if dim("size"):
            bits.append(f"{dim('size')} fillet")
        if p.get("count"):
            bits.append(f"{_fmt(float(p['count']))} joints")
        if p.get("fixture"):
            bits.append(f"in {p['fixture']}")
    elif op == "FINISH":
        bits.append(str(p.get("spec") or ""))
        if p.get("coverage"):
            bits.append(str(p["coverage"]))
    elif op == "CUT_PROFILE":
        bits += [f"{dim('thickness')} plate" if dim("thickness") else "",
                 str(p.get("flat_file") or "")]
    else:
        bits.append(str(p.get("detail") or p.get("content") or ""))
    label = OPERATIONS.get(op, {}).get("label", op)
    tail = " ".join(b for b in bits if b).strip()
    return f"{label} {tail}".strip() if tail else label


def search_text(conn: sqlite3.Connection, revision_id: int) -> str:
    """The routing as one searchable string, so 'notch' or 'cope' finds the
    revisions that carry the operation (plm/search.py)."""
    parts = []
    for o in get_routing(conn, revision_id):
        parts.append(f"{o['op']} {o['summary']} {o['work_center']} {o['notes']}".strip())
    return "\n".join(parts)


# =============================================================================
# RAW STOCK
# =============================================================================

def stock_of(conn: sqlite3.Connection, item_id: int) -> dict | None:
    """The raw stock this part is cut from, through the item-level `cut_from`
    relation, plus that stock's spec attributes.

    ITEM-level on purpose (see relations.REGISTRY): which stock a rail comes off
    follows the design across revisions, and revise() does not copy relations,
    so a revision-pinned link would go stale at the next bump and silently drop
    the part out of the cut list."""
    from . import attributes as attrs
    row = conn.execute(
        """SELECT i.id, i.number, i.name, i.kind, i.latest_rev_id, r.note
           FROM relations r JOIN items i ON i.id=r.target_item_id
           WHERE r.item_id=? AND r.relation='cut_from'
           ORDER BY r.id LIMIT 1""", (item_id,)).fetchone()
    if not row:
        return None
    spec: dict[str, Any] = {"number": row["number"], "name": row["name"],
                            "item_id": row["id"], "note": row["note"] or ""}
    a = attrs.get(conn, row["latest_rev_id"]) if row["latest_rev_id"] else {}
    for name in STOCK_ATTRS:
        spec[name] = a[name]["value"] if name in a else ""
        spec[name + "_unit"] = a[name]["unit"] if name in a else ""
    spec["material"] = a["material"]["value"] if "material" in a else ""
    return spec


def _stock_len(spec: dict) -> tuple[float, str]:
    """The purchasable stick length and its unit — the canonical unit of the
    whole nest group, because the stick is what you buy."""
    raw, unit = spec.get("stock_length", ""), canon_unit(spec.get("stock_length_unit", ""))
    if raw in ("", None):
        raise FabError(
            f"{spec['number']} has no `stock_length` — a nest needs to know how "
            f"long a stick you can buy. Set it on the stock revision: "
            f"set_attributes('{spec['number']}', "
            f"{{'stock_length': {{'value': 120, 'unit': 'in'}}}})")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise FabError(f"{spec['number']} stock_length={raw!r} is not a number") from None
    if not unit:
        raise FabError(
            f"{spec['number']} stock_length has no unit — 10 ft and 10 in are "
            f"different purchases. Pass {{'value': {val:g}, 'unit': 'ft'}}.")
    return val, unit


def _attr_dim(spec: dict, name: str) -> tuple[float, str] | None:
    raw, unit = spec.get(name, ""), canon_unit(spec.get(name + "_unit", ""))
    if raw in ("", None):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise FabError(f"{spec['number']} {name}={raw!r} is not a number") from None
    if not unit:
        raise FabError(f"{spec['number']} {name} has no unit — plm never "
                       f"defaults a physical unit (plm/attributes.py)")
    return val, unit


# =============================================================================
# THE CUT LIST
# =============================================================================

def cut_list(conn: sqlite3.Connection, rev_id: int, *,
             kerf: dict | None = None, remnant_min: dict | None = None) -> dict:
    """The cut list for everything under one revision: every MAKE part that
    consumes raw stock, its finished length, its quantity rolled up through the
    weldments, its follow-on operations, and the weldment(s) it belongs to —
    then grouped by stock profile and nested into purchasable sticks.

    TRACEABILITY, BOTH WAYS. Each row carries `weldments` (the BOM paths that
    got here) and `stock` (the purchased item it is cut off), so
    weldment -> finished cut part -> raw stock reads forwards, and
    relations.for_item on the stock reads it backwards.

    NOTHING IS RAISED FOR ONE BAD PART. A cut list that refuses wholesale
    because one of twelve parts is missing a unit is useless; every problem lands
    in `issues`, named, and the other eleven still nest. What DOES refuse is an
    impossible conversion inside a group — that group is excluded and named,
    because a stick count computed across units nobody could reconcile is worse
    than no stick count.
    """
    from . import attributes as attrs
    rows: list[dict] = []
    issues: list[dict] = []
    weld_ops: list[dict] = []

    for line in db.rolled_bom(conn, rev_id):
        if line["kind"] not in ("part", "assembly"):
            continue
        bound = (db.find_revision(conn, line["number"], line["version"])
                 or db.find_revision(conn, line["number"]))
        if not bound:
            continue
        proc = (bound.get("process") or "")
        routing = get_routing(conn, bound["id"])
        if proc != "make":
            if routing:
                issues.append({
                    "number": line["number"],
                    "problem": f"has a routing ({len(routing)} operations) but "
                               f"process={proc or 'unset'!r} — it will not "
                               f"appear in the MAKE plan. set_process("
                               f"'{line['number']}', 'make')."})
            continue
        cut = next((o for o in routing if o["consumes_stock"]), None)
        follow = [o for o in routing if not o["consumes_stock"]]
        if line["kind"] == "assembly":
            # A MAKE assembly is a WELDMENT: its operations are joining and
            # finishing work, and it consumes no stock of its own.
            weld_ops.append({
                "number": line["number"], "name": line["name"],
                "version": line["version"], "revision_id": bound["id"],
                "qty": line["qty"], "unit": line["unit"],
                "operations": routing, "paths": line["paths"]})
            if cut:
                issues.append({
                    "number": line["number"],
                    "problem": f"is an assembly with a {cut['op']} operation — "
                               "an assembly is welded from pieces, it is not cut "
                               "from stock. Give the piece its own part number."})
            continue
        if not cut:
            issues.append({
                "number": line["number"],
                "problem": "process=make with no stock-consuming operation — "
                           f"add a CUT (or {', '.join(o for o in OPERATIONS if OPERATIONS[o]['consumes_stock'] and o != 'CUT')})"
                           " so plm knows what comes off the stick"})
            continue
        spec = stock_of(conn, line["item_id"])
        if not spec:
            issues.append({
                "number": line["number"],
                "problem": "no `cut_from` link — plm cannot tell what stock "
                           f"this is cut off. link_items('cut_from', "
                           f"'{line['number']}', '<the purchased stock>')"})
            continue
        try:
            length, lunit = _dim(cut["params"].get("length"), "CUT `length`",
                                 where=line["number"])
            allow = cut["params"].get("allowance")
            a_val, a_unit = _dim(allow, "CUT `allowance`",
                                 where=line["number"]) if allow else (0.0, lunit)
            k = _attr_dim({**spec}, "kerf")
            k_src = "stock"
            if cut["params"].get("kerf"):
                k = _dim(cut["params"]["kerf"], "CUT `kerf`", where=line["number"])
                k_src = "operation"
            if not k:
                kd = kerf or DEFAULT_KERF
                k = _dim(kd, "kerf", where="request")
                k_src = "request" if kerf else "plm default"
        except FabError as exc:
            issues.append({"number": line["number"], "problem": str(exc)})
            continue
        rows.append({
            "number": line["number"], "name": line["name"],
            "version": line["version"], "revision_id": bound["id"],
            "qty": line["qty"],
            "op": cut["op"],
            "length": length, "unit": lunit,
            "allowance": a_val, "allowance_unit": a_unit,
            "kerf": k[0], "kerf_unit": k[1], "kerf_source": k_src,
            "end_a": cut["params"].get("end_a", ""),
            "end_b": cut["params"].get("end_b", ""),
            "cut_summary": cut["summary"],
            "operations": follow,
            "follow_on": "; ".join(o["summary"] for o in follow),
            "material": (bound.get("material") or "") or spec.get("material", ""),
            "stock": spec,
            "weldments": sorted({p.rsplit(" > ", 1)[0].rsplit(" > ", 1)[-1]
                                 for p in line["paths"]}),
            "paths": line["paths"],
        })

    rows.sort(key=lambda r: r["number"])
    groups, gissues = _group_and_nest(rows, remnant_min=remnant_min)
    issues += gissues
    return {
        "cuts": rows,
        "stock": groups,
        "weldments": sorted(weld_ops, key=lambda w: w["number"]),
        "issues": issues,
        "totals": {
            "cut_parts": len(rows),
            "pieces": sum(int(math.ceil(r["qty"])) for r in rows),
            "profiles": len(groups),
            "sticks": sum(g["stick_count"] for g in groups),
        },
        "algorithm": ALGORITHM,
    }


ALGORITHM = {
    "name": "first-fit-decreasing (FFD), 1-D",
    "deterministic": True,
    "how": "Pieces are expanded one per required quantity, each charged "
           "length + allowance + kerf, sorted by descending charged length with "
           "(part number, instance index) as the tie-break, then placed in the "
           "first stick with room; a new stick is opened when none has room. No "
           "randomness and no dependence on dict or row order, so the same "
           "input always produces the same plan — plm's hashing discipline "
           "applies to generated plans, not only to files.",
    "honest_limits": [
        "FFD IS NOT OPTIMAL. Classic bin packing: FFD uses at most "
        "11/9·OPT + 6/9 bins, and on a bad instance it really does buy one "
        "stick more than a human with a pencil would. It is chosen because it "
        "is deterministic, explicable and never under-buys — not because it is "
        "the best packing.",
        "KERF IS CHARGED FOR EVERY PIECE, including the last one off a stick. "
        "That over-reserves by at most one kerf per stick and is deliberately "
        "the conservative direction: a plan that under-buys steel is a second "
        "trip to the yard.",
        "NO DROP INVENTORY. The nest starts from full sticks every time; the "
        "offcuts on the rack from the last job are not modelled, so the stick "
        "count is 'what to buy if you had nothing', which is the right answer "
        "for a purchase order and the wrong one for a Saturday.",
        "NO CUT SEQUENCE, ORIENTATION, GRAIN OR DEFECT AVOIDANCE. Which end of "
        "the stick, which order, and dodging a bad spot are all shop-floor "
        "judgement; plm reports the assignment, not the procedure.",
        "MITERS ARE NOT NESTED HEAD-TO-TAIL. Two 45-degree miters can sometimes "
        "share a cut and save material; the nest treats every piece as a plain "
        "length. That is under-optimising, never under-buying.",
    ],
}


def _group_and_nest(rows: list[dict], *,
                    remnant_min: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Group cuts by the stock they come off, then nest each group."""
    issues: list[dict] = []
    by_key: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["stock"]["number"], str(r["stock"].get("stock_profile") or ""))
        by_key.setdefault(key, []).append(r)

    out: list[dict] = []
    for (stock_number, profile), members in sorted(by_key.items()):
        spec = members[0]["stock"]
        form = (spec.get("stock_form") or "").strip().lower()
        try:
            if form and form not in STOCK_FORMS:
                raise FabError(
                    f"{stock_number} has stock_form={form!r} — known forms: "
                    f"{', '.join(STOCK_FORMS)}. (Attributes are a gloss, so the "
                    "write was allowed; the NEST is where a typo has to be "
                    "caught, and this is that refusal.)")
            if form == "sheet":
                raise FabError(
                    f"{stock_number} is stock_form='sheet' — plm does not lay "
                    "parts out on a sheet. A 2-D nest is bound to one physical "
                    "remnant on one machine at one moment, and plm's revisions "
                    "are immutable; that nest lives in the shop-floor service "
                    "(devops/docs/projects/2026-08-13-cnc-plasma). plm still "
                    "owns the sheet COUNT — which is not this function.")
            stick, unit = _stock_len(spec)
            rmin = _attr_dim(spec, "remnant_min")
            if remnant_min:
                rmin = _dim(remnant_min, "remnant_min", where="request")
            rmin_val = convert(rmin[0], rmin[1], unit) if rmin else \
                convert(DEFAULT_REMNANT_MIN["value"], DEFAULT_REMNANT_MIN["unit"], unit)
            nest = nest_linear(members, stick, unit, rmin_val)
        except FabError as exc:
            issues.append({"number": stock_number, "problem": str(exc)})
            continue
        out.append({
            "stock": spec, "stock_number": stock_number,
            "stock_profile": profile,
            "stock_section": spec.get("stock_section") or "",
            "stock_form": form or "linear",
            "material": spec.get("material") or "",
            "stock_length": stick, "unit": unit,
            "remnant_min": rmin_val,
            "parts": [{"number": m["number"], "name": m["name"],
                       "qty": m["qty"], "length": m["length"],
                       "unit": m["unit"]} for m in members],
            **nest,
        })
    return out, issues


def nest_linear(members: list[dict], stick: float, unit: str,
                remnant_min: float) -> dict:
    """First-fit-decreasing into sticks of `stick` length, all arithmetic in
    `unit`. See ALGORITHM for the method and its honest limits.

    Every piece is charged `length + allowance + kerf`. `allowance` is real
    material (a cope blank is cut long); `kerf` is the material the blade turns
    into sparks. They are reported separately because they are different
    questions: allowance is design, kerf is process.
    """
    pieces: list[dict] = []
    for m in sorted(members, key=lambda r: r["number"]):
        length = convert(m["length"], m["unit"], unit)
        allow = convert(m["allowance"], m["allowance_unit"], unit) if m["allowance"] else 0.0
        kerf = convert(m["kerf"], m["kerf_unit"], unit)
        need = round(length + allow + kerf, 4)
        if need > stick + 1e-9:
            raise FabError(
                f"{m['number']} needs {need:g} {unit} off a {stick:g} {unit} "
                f"stick — the piece is longer than the stock. Either the stock "
                f"length is wrong, or this part is a splice and needs to be "
                f"modelled as two pieces in the weldment's BOM.")
        for i in range(int(math.ceil(m["qty"]))):
            pieces.append({"number": m["number"], "name": m["name"],
                           "length": length, "allowance": allow, "kerf": kerf,
                           "need": need, "idx": i})
    # Deterministic: longest first, then part number, then instance index.
    pieces.sort(key=lambda p: (-p["need"], p["number"], p["idx"]))

    sticks: list[dict] = []
    for p in pieces:
        for s in sticks:
            if s["remaining"] + 1e-9 >= p["need"]:
                s["cuts"].append(p)
                s["remaining"] = round(s["remaining"] - p["need"], 4)
                break
        else:
            sticks.append({"cuts": [p], "remaining": round(stick - p["need"], 4)})

    cut_total = round(sum(p["length"] for p in pieces), 4)
    allow_total = round(sum(p["allowance"] for p in pieces), 4)
    kerf_total = round(sum(p["kerf"] for p in pieces), 4)
    stock_total = round(len(sticks) * stick, 4)
    usable = round(sum(s["remaining"] for s in sticks
                       if s["remaining"] >= remnant_min), 4)
    scrap = round(stock_total - cut_total - allow_total - kerf_total - usable, 4)
    return {
        "stick_count": len(sticks),
        "sticks": [{
            "index": i + 1,
            "cuts": [{"number": c["number"], "length": c["length"],
                      "allowance": c["allowance"], "kerf": c["kerf"],
                      "charged": c["need"]} for c in s["cuts"]],
            "used": round(stick - s["remaining"], 4),
            "remnant": s["remaining"],
            "remnant_usable": s["remaining"] >= remnant_min,
        } for i, s in enumerate(sticks)],
        "piece_count": len(pieces),
        "cut_length_total": cut_total,
        "allowance_total": allow_total,
        "kerf_total": kerf_total,
        "stock_length_total": stock_total,
        "usable_remnant_total": usable,
        "scrap_total": scrap,
        # Waste = kerf + unusable drop. A remnant long enough to keep is NOT
        # waste, and calling it waste is how a shop convinces itself it is 30%
        # wasteful when it is 6% wasteful with a full rack of drops.
        "waste_pct": round((kerf_total + scrap) / stock_total * 100, 1)
        if stock_total else 0.0,
        "yield_pct": round(cut_total / stock_total * 100, 1) if stock_total else 0.0,
    }


def markdown(plan: dict, *, title: str = "Cut list") -> str:
    """The cut list as markdown — the same artifact somebody used to maintain by
    hand (CCR-ASM-0002 v0.1.0 shipped a written CUT_LIST.md), now GENERATED.
    Kept here rather than in a template so that the API, the MCP and anything
    that wants it on paper (the shop-floor service print_markdown) all emit the same page.
    """
    L: list[str] = [f"# {title}", ""]
    if plan["cuts"]:
        L += ["| Part | Name | Stock | Cut | Qty | Ends | Follow-on |",
              "|---|---|---|--:|--:|---|---|"]
        for r in plan["cuts"]:
            ends = "/".join(x for x in (r["end_a"], r["end_b"]) if x) or "—"
            L.append(
                f"| {r['number']} | {r['name']} | {r['stock'].get('stock_section') or r['stock']['number']} "
                f"| {_fmt(r['length'])} {r['unit']} | {_fmt(r['qty'])} | {ends} "
                f"| {r['follow_on'] or '—'} |")
        L.append("")
    for g in plan["stock"]:
        L += [f"## {g['stock_profile'] or g['stock_number']} — "
              f"{g['stock_section'] or ''}".rstrip(" —"), ""]
        L.append(f"{g['stick_count']} × {_fmt(g['stock_length'])} {g['unit']} "
                 f"({g['stock_number']}) · yield {g['yield_pct']}% · "
                 f"waste {g['waste_pct']}% "
                 f"(kerf {_fmt(g['kerf_total'])} + scrap {_fmt(g['scrap_total'])} "
                 f"{g['unit']}) · usable remnant "
                 f"{_fmt(g['usable_remnant_total'])} {g['unit']}")
        L.append("")
        for s in g["sticks"]:
            cuts = " + ".join(f"{c['number'].rsplit('-', 1)[-1]}@{_fmt(c['length'])}"
                              for c in s["cuts"])
            L.append(f"- Stick {s['index']}: {cuts} → remnant "
                     f"{_fmt(s['remnant'])} {g['unit']}"
                     f"{' (usable)' if s['remnant_usable'] else ''}")
        L.append("")
    if plan["weldments"]:
        L += ["## Weldments", ""]
        for w in plan["weldments"]:
            L.append(f"- **{w['number']}** {w['name']} ×{_fmt(w['qty'])}: "
                     + ("; ".join(o["summary"] for o in w["operations"]) or "no operations"))
        L.append("")
    if plan["issues"]:
        L += ["## Issues", ""]
        for i in plan["issues"]:
            L.append(f"- **{i['number']}**: {i['problem']}")
        L.append("")
    L += ["---", f"Nesting: {plan['algorithm']['name']}. "
                 f"{plan['algorithm']['honest_limits'][0]}"]
    return "\n".join(L)
