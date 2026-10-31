# plm data model — Teamcenter concepts, workshop scale

*2026-08-11. How the grown-up PLMs model this world, and exactly how plm maps
onto it. Sources: Siemens Teamcenter docs/community material (see foot).*

## The Teamcenter shape

| Teamcenter | What it is | plm mapping |
|---|---|---|
| **Item** | The master record: immutable identity (item ID), owns nothing revision-specific | `items` row (`number` is the identity) |
| **Item Revision** | One revision of an item; everything real (files, BOM, status) hangs off revisions, not items | `revisions` row — **our core data type** |
| **Dataset + named references** | File container attached to a revision; the named references are the actual files (CAD, STL, PDF…) | `files` rows on a revision (role: source / artifact / drawing / doc / thumbnail) |
| **BOMView / BOMView Revision (BVR)** | The structure lives in its own revisioned object under the item revision | `bom` rows keyed by `parent_rev_id` — each parent revision carries its own BOM, which is the same idea flattened |
| **Occurrence (PSOccurrence)** | One placement of a child in a parent BVR: find number, qty, notes; the same child can occur many times | `bom` row with `find_number` (verified: same screw at two stations = two rows) |
| **Precise vs imprecise occurrence** | Precise binds the child to a specific Item *Revision*; imprecise binds to the *Item* and is resolved later | `bom.child_version` non-empty = precise; **empty = imprecise** |
| **Revision rule** ("Latest Working", "Latest Released", "Precise") | At view time, resolves every imprecise occurrence to a concrete revision | `find_revision(number, version="")` resolves to the item's latest; a "latest released" rule is a WHERE state='RELEASED' away |
| **Check-out / check-in** | Lock an item revision / dataset while someone edits; check-in releases | `checkouts` table: item-level advisory lock; check-in and revise release it — and a lock left behind by a dead agent is taken over on age (`ops.claim_checkout`) |
| **Variant condition / 150% BOM** | An occurrence that exists in the structure but is excluded from the configurations that do not have it | `bom.suppressed` — the line stays, rollups/plans/the viewer skip it. **Not** deletion; see the five verbs below |
| **Revise** | New item revision seeded from the old: datasets carried forward, BVR copied | `POST /api/items/{n}/revise` — new DRAFT revision, files + BOM carried forward |
| **Release status / baseline** | Freezing: statuses stamp a revision; baselines capture a precise structure | lifecycle states on revisions; a release = flipping bindings precise + `RELEASED` |

## The design consequence that matters

**You do not rev the whole tree because a screw changed.** Teamcenter's answer
to structure churn is imprecise occurrences + revision rules: parents reference
the *item*, and the viewer resolves "which revision" at read time. Only when
you *release* an assembly do you freeze occurrences to precise revisions.

plm therefore supports both motions:

1. **Imprecise by default while developing.** Leave `child_version` empty; the
   BOM and the 3D viewer resolve to latest automatically. A child rev-up is
   visible up the whole tree instantly, no writes needed.
2. **Explicit propagation for precise trees.** The lantern vault binds precise
   versions (its CSVs carry `child_version`). For that world,
   `POST /api/items/{n}/propagate` walks where-used: every parent whose latest
   revision binds an older revision of `n` gets **revised up** (new DRAFT, BOM
   carried forward with that occurrence rebound), recursively to the top. Each
   created revision is recorded and returned — the "push the revision up the
   tree" button, as an auditable act instead of a side effect.

## Editing a DRAFT's product structure — the five verbs (added 2026-08-13)

A DRAFT is a **working revision**. Iterating on its BOM is ordinary design work
and costs no revise, no freeze and no re-fork. `ops.py` owns all five; the HTTP
API and the MCP are two doors onto the same functions.

| Verb | ops | HTTP | MCP | Says |
|---|---|---|---|---|
| add | `add_bom_line` | `POST /api/items/{n}/bom` | `add_bom_line` | a new occurrence |
| update | `update_bom_line` | `PATCH /api/bom/{id}` (`qty`/`unit`/`find`/`notes`) | `update_bom_line` | the occurrence stays; its **numbers** change |
| replace | `replace_bom_line` | `PATCH /api/bom/{id}` (`new_child`/`child_version`) | `replace_bom_line` | the same **position** now binds a different child and/or revision |
| remove | `remove_bom_line` | `DELETE /api/bom/{id}` | `remove_bom_line` | **this was never part of it** |
| suppress | `suppress_bom_line` | `PATCH /api/bom/{id}` (`suppressed`) | `suppress_bom_line` | it **is** part of the product and **absent in this configuration** |

**Suppression is not removal.** They are different statements about the product
and plm will not guess between them:

- A **suppressed** line is a real occurrence that *this* configuration does not
  include — the mounting boss you print only for the tall variant, the bracket a
  fork inherited and does not use. It stays in the record, stays visible
  (flagged), and drops out of rolled quantities, build plans and the 3D view.
  This is the **150% BOM** / Teamcenter variant-condition posture, and it is why
  suppression is the one edit allowed on a **frozen** revision: it records which
  variant you are describing, not a change to the product.
- **Removal** says the occurrence was a mistake and is not part of the product in
  any configuration, so nothing downstream should ever reason about it. DRAFT
  only; the event log keeps what went.

**A find number is a position**, so at most one occurrence holds it on a given
revision (verified across the production registry before enforcing: zero
collisions). Two of the same part is a *quantity*, not two lines. `replace`
therefore preserves `find_number`, `unit` and `notes` — those describe the
assembly, not whichever part currently sits at that position.

**Nothing needs invalidating after a BOM edit**, and that is a property worth
keeping. Rolled quantities (`db.rolled_bom`) and build plans (`_build_plan`) walk
the live rows at read time; thumbnails key on the sha/size/mtime of the STLs that
`thumbs.geometry_bodies` collects *through* the BOM, so a structure change changes
the cache key and re-renders by itself; the FTS index carries no BOM content at
all. There is no cache to bust. (One soft edge: `/api/items/{n}/thumb` is served
with `Cache-Control: max-age=300`, so a browser may show the previous composite
for up to five minutes.)

**Addressing.** `ops.resolve_bom_line` is the one rule, shared by both surfaces:
a `line_id` (every read returns each line's `id`), or the parent plus the child,
or the parent plus a find number. An ambiguous reference is refused with the
candidates listed rather than resolved by guessing.

## Concurrency, atomicity and idempotency of writes (added 2026-08-13)

SQLite has **one writer**. plm's transactions are supposed to be milliseconds
long, so contention should be invisible. The rules that keep that true:

- **No slow work inside a write transaction.** `ops.checkin` and
  `ops.upload_files` commit whatever is pending *before* the vault write, so the
  file writes and the git commit (seconds to tens of seconds on the synced
  volume) happen with no transaction open; the row writes are then one short
  transaction the op owns. `staging.fetch_url` records its `upload.fetched` event
  **out of band** (`db.event_out_of_band`) precisely so `normalize()` cannot open
  the caller's transaction ahead of that vault work, and `staging.consume`'s
  delete of the inbox directory runs *after* the commit.
- **Search indexing is incremental and post-commit.** `search.touch(conn, item)`
  replaces `search.reindex(conn)` on every write path: it commits, then reindexes
  only the items the write touched. The full rebuild remains, as
  `POST /api/admin/reindex`. Measurements are in `plm/search.py`.
- **Waiting is bounded, and counted.** `db.PlmConnection` retries an individual
  statement on `SQLITE_BUSY` with exponential backoff
  (`PLM_SQLITE_BUSY_TIMEOUT_MS`, `PLM_SQLITE_WRITE_RETRIES`) — safe because a BUSY
  statement did not run. When the budget is exhausted the error says how long it
  waited and for what. `GET /api/stats` → `sqlite` carries the counters.
- **A check-in is atomic across BOTH stores.** If the database half fails after
  the vault half succeeded, `vault.rollback_checkin` removes the version folder
  plm wrote (restoring its previous content when it had any) and commits the
  undo, and a `checkin.rolled_back` event is recorded out of band. Nothing
  partial survives: no revision row, no orphan `files` rows, no stranded
  checkout, and the staged uploads are **not** consumed, so the same call can be
  retried with the same handles.
- **Idempotency where identity is minted, uniqueness everywhere else.** A
  repeated check-in is a 409 because versions are immutable; a repeated create
  with an explicit number is a 409 because numbers are unique; state and
  attribute writes are last-writer-wins on a value. The only non-idempotent
  writes were the two that *allocate* a number — `create_item` and `fork` on the
  auto path — so those carry a replay record (`plm/idem.py`, `idempotency_key`,
  plus a short auto-fingerprint window for clients that send no key).
  `add_bom_line` with an auto find number refuses a child already on the BOM
  rather than creating a duplicate occurrence.
- **A checkout is recoverable.** `checkouts.released_at IS NULL` strands an item
  when the agent holding it dies. A lock older than
  `PLM_CHECKOUT_STALE_SECONDS` (6 h) is **taken over automatically** by the next
  writer, with a `checkout.takeover` event; breaking another actor's **live** lock
  needs `force`, a reason, and a human identity. `GET /api/checkouts` and MCP
  `list_checkouts` list what is held, with ages.

## Identity: plm owns the numbers (added 2026-08-11)

**A part number is an identifier, not a classification.** The only structure it
carries is `<PREFIX>-<CLASS>-<SEQ>`, where CLASS is a key of
`config.KIND_BY_CLASS` (PRT/ASM/PUR/HDW/FW/DOC/CPN/AST) and therefore says exactly
what `kind` already says. This is enforced server-side in one place —
`ops.validate_number`, called by `create_item`, by the BOM-child
auto-registration path in `checkin` (the other door into the number space) and by
`renumber_item`:

- an explicit number must match the shape AND its class must agree with the
  `kind` being registered;
- anything else is a 400 that teaches the rule ("plm owns identity — omit
  `number` and let plm assign it, or use an approved class");
- the intended path is to omit `number` entirely and pass `prefix` + `kind`, and
  let `next_number()` assign the next free one.

**Why**: a smart number (`GL-OWNED-001`, `GL-FUTURE-002`, `GL-ELEC-007`) is a
second, unqueryable source of truth for a fact that already has a home. It
cannot be filtered on, cannot be corrected without a migration, and is wrong the
first time reality moves — a "FUTURE" part gets bought, an "OWNED" one is sold.
Those facts belong in `kind`, `project`, `name`, **attributes**, `sources` and
context, where they can be queried and changed.

**Numbers are still immutable in the sense that matters** — but they are
repairable. `ops.renumber_item` (POST `/api/items/{n}/renumber`, MCP
`renumber_item`) assigns the correct number, and:

| Concern | How it survives |
|---|---|
| Old links, bookmarks, agent memory, printed labels | `items.former_numbers` (comma list); **every lookup matches it** through `db.item_by_number`, so the old number resolves forever |
| BOM lines, relations, files, events, approvals, context | all reference the item's integer **id**, so structure is untouched by construction |
| The vault folder (its name contains the number) | renamed with a git commit in the data repo (`vault.move_folder`), and every `files.path` under it repointed in the same transaction |
| "Why is this called something else now?" | an append-only context entry on the item, plus an `item.renumber` event |

Prod migration of 2026-08-11: 35 Codex-invented numbers (GL-ELEC-*, GL-MECH-*,
GL-OWNED-*, GL-FUTURE-*, GL-REF-*) were renumbered into GL-PUR-*, GL-HDW-* and
GL-PRT-*; names and descriptions untouched.

### IDs are first class

plm owns the ids as well as the numbers, so both are keys, and every payload
that returns an item or a revision carries its integer `id`. Reads accept
either: `/api/items/95` == `/api/items/GL-PRT-1001` (a bare integer is never
ambiguous, because a number always carries its class token), `/api/revisions/{id}`
is the revision-addressed read, and `?revision_id=` addresses a revision on the
BOM/plan endpoints. Number+version stays fully supported — it is what humans
(and printed drawings) use.

## Attributes: the facts people build in CAD (added 2026-08-11)

`attributes(revision_id, name, value, unit, source, updated_at)` +
`plm/attributes.py`. **Revision-level**, because geometry changes change every
one of them; they carry forward on `revise`/`fork` exactly like files do, and
they are indexed into FTS search.

- **`WELL_KNOWN` is a registry like `relations.REGISTRY`, with one deliberate
  difference: it is a gloss, not a gate.** It gives labels, default units and
  value kinds for `mass_g`, `material`, `bbox_x_mm/y/z`, `color`,
  `print_time_min` — so the common facts have one spelling — but ANY name may be
  written. A slicer that learns a new property must not need a code change.
- **One home per fact.** `material` and `mass_g` remain COLUMNS on `revisions`
  (the build-plan rollup reads them in SQL). Reads merge the columns into the
  same dict (flagged `column: true`); writes to those two names are routed to
  the columns. There is never a row and a column disagreeing.
- **A third case, and it is named: `ELSEWHERE`** (2026-08-13).
  `process` is a `revisions` column that is merged into this READ but **refused
  on write here**, with a refusal that names the verb that owns it
  (`set_process`). The reason is the distinction the whole module rests on:
  everything in `COLUMN_BACKED` is a MEASUREMENT, and you may legitimately weigh
  a RELEASED part — so those writes carry no lifecycle guard. `process` is
  DESIGN INTENT, so it is DRAFT-only. One fact, one home, one guard, and the read
  surface stays uniform.
- **Stock spec attributes**: `stock_form`, `stock_profile`,
  `stock_section`, `stock_length`, `kerf`, `remnant_min` on a `purchased` item's
  revision. Still a GLOSS — a typo lands, and `fab.cut_list` refuses it by name
  where it matters. **No unit in any name and no default unit**, per the rule
  above: `{"value": 120, "unit": "in"}`.
- **The instrument records them**: `PATCH /api/revisions/{id}/attributes` (or
  `/api/items/{n}/attributes?version=`, or MCP `set_attributes`) with
  `source='orca 2.4.2, 0.20mm'`. plm never computes them; `set_material` is now
  simply the two-column special case of the same surface. **The seam for a
  future slicer/CAD integration is exactly this endpoint** — it needs no plm
  change beyond posting.

## Context is GENERIC: comments attach to any object (added 2026-08-12)

`context(id, item_id, revision_id, body, actor, created_at, target_type,
target_id)` + `plm/context.py`. Context began as the "why" on an item or a
revision. The maintainer's reading is the one that generalizes it: **"it's basically
comments"** — and comments are a property of *things people discuss*, not of
parts. A typed link someone questions, an approval someone conditions, later a
change order: each deserves the same append-only, searchable log.

So `context` got the treatment `relations` got: **one table, one registry, no
per-target code** — `context.TARGETS`, keyed by target type, each entry holding a
label, a hint, and a `resolve()` that answers *does this exist* and *what does it
hang off*. Today: `item`, `revision` (the classic pair), `relation`, `approval`.
Extending it is one dict entry.

**Two storage forms, one meaning.**

| form | `target_type` | meaning |
|---|---|---|
| classic | `''` | `item_id` is the item; `revision_id` NULL = item-level, set = revision-level. **Every pre-existing row, and every new comment on a part.** |
| generic | `'relation'`, `'approval'`, … | `target_id` names the row being discussed; `item_id`/`revision_id` are that target's **anchor** |

Target types `item`/`revision` are written in the CLASSIC form on purpose: there
is one representation of "a comment on this item", not two.

**Why the migration is two `ALTER TABLE ADD COLUMN`s and no table rebuild.**
`context.item_id` is `NOT NULL`, and SQLite cannot relax that in place — the
textbook move would be a new-table-copy. It is not needed, because every target
in the registry *belongs to* an item: the relation's FROM item, the approval's
revision and therefore its item. Each resolver returns that anchor and it is kept
on the row. That is the least invasive correct migration, and the anchor pays for
itself twice over:

- **search** (`plm/search.py`) indexes generic comments through the anchor with no
  per-type code, so "why is this a bench test?" is found by searching the item it
  concerns;
- **events** stay `context` events with the same `item_id`/`revision_id` they
  always had (generic writes add `target_type`/`target_id` to the event `data`).

The day a target type has NO owning item (a comment on a project, on a user) is
the day `item_id` must become nullable — and that is the day for the table
rebuild, not before.

**Reads.** Classic reads filter `target_type=''`, so generic comments never leak
into the item page's Context card; they render where they were attached — a
comment count plus an expandable drawer on each Related-items row, and inline on
the approval queue. `GET /api/context?target_type=&target_id=` +
`POST /api/context` are the generic surface; `GET|POST /api/items/{n}/context`
keep their exact original shape and are now thin wrappers over the same write
path, so validation cannot drift between surfaces. `GET /api/context/targets`
(MCP `context_targets`) publishes the registry; MCP `add_context` takes optional
`target_type`/`target_id` and is unchanged by default.

## Kind is a fact; the number and folder are projections of it (added 2026-08-12)

`ops.reclassify_item` (HTTP `PATCH /api/items/{n}/kind`, MCP `reclassify_item`).
Changing `items.kind` is not a one-column update, because two other things are
derived from it: the **class token** in the number (`config.KIND_BY_CLASS`) and
the **vault subfolder** (`vault.item_folder` — `parts/`, `assemblies/`,
`coupons/`). Leave them behind and the number lies to every human and every tool.

So the op does the whole change or refuses it: kind first (+ an `item.kind`
event), *then* `renumber_item` — which reads the item's now-current kind, assigns
the next free number in the right class, moves the vault folder with a git commit
and repoints `files.path`, while `former_numbers` keeps the old number resolving
forever — and finally a context entry recording the judgement. Referential
integrity is free: BOM lines, relations, files, revisions, events, approvals and
context all key on the integer id.

Guards: an item used in a BOM cannot become a `coupon` or an `asset` (coupons
relate, assets are mounted to — neither assembles), and the refusal names the
parent revisions so you know what to unpick; `renumber=False` records the kind
alone and is refused when that would leave the class token contradicting the fact.

## Assets: what you attach to, which you neither build nor buy (added 2026-08-13)

*. Full argument in `docs/PLM_RELATIONS.md`; this is the data-model shape.*

Teamcenter has two ideas plm needed one of. **As-maintained / physical parts**
model real serialized units in the field, and **reference/context structures**
(datum geometry a design is positioned against without consuming it) supply
constraints without appearing in the EBOM. Workshop scale collapses them into one
kind:

| Concept | plm |
|---|---|
| The thing your part attaches to and you neither build nor buy | `items.kind = 'asset'`, class **AST** |
| Its measured configuration at a point in time | a **revision** of that asset — files (scan, parametric envelope, viz mesh) + attributes |
| The physical interface | relation `mounts_to` (item-level) |
| The as-built state (*which* revision is on it) | relation `installed_on` (revision-level; `from_version` is the point) |
| Reference geometry constraining a design | relation `integration_envelope_for` (both ends pinned) |
| The specific unit vs the catalogue model | relation `instance_of` |
| Measured dimensions | revision **attributes**, unit-bearing (see below) |
| Where it lives, who owns it, whether we still have it | **context** — item-level facts that change without the geometry changing |

**A revision of an asset is a MEASUREMENT, not a design change.** That is the one
place the analogy with parts bends, and it bends in a useful direction: re-tape
the camper, check in a new revision, and every `integration_envelope_for` link
still names the revision it was judged against. Attributes carry forward on
`revise` exactly like files, so a re-measure starts from what you thought you
knew.

**It is not structure, and three guards keep it that way** — the BOM bar on both
write paths (`ops.NOT_IN_BOM`), the `reclassify_item` bar above, and the product
bar (`ops.set_product`, shared by HTTP and MCP). Everything downstream —
`rolled_bom`, the material rollup, `_build_plan`'s buy list, `/api/products`,
`_top_items` — is then correct *by construction* rather than by a filter someone
has to remember. `config.NON_STRUCTURAL_KINDS` is the single list; `_build_plan`'s
kind predicates are allow-lists on purpose.

**Type vs instance is a relation, not a kind.** Both are assets; `instance_of`
carries the difference. Fit work points at the instance, because the measured
rail spacing is the datum and the brochure is not. Creating a type item when there
is one unit is inventing a row.

**Envelope attributes carry their unit as DATA.** `overall_length/width/height`,
`mount_spacing`, `mount_hole_pitch`, `mount_hole_dia`, `mount_hole_count`,
`load_rating`, `serial_number` have no unit in the name and no default unit —
unlike `bbox_x_mm`, whose `mm` is safe because a slicer is always metric. A
vehicle measured with a tape in California is in inches. Pass
`{"value": 81, "unit": "in"}`.

## Files are revision content

Per Teamcenter's dataset model, files belong to revisions, never to items:

- Check-in attaches files to the new revision (vault write + git commit).
- Revise carries the previous revision's file rows forward (same immutable
  vault paths) until new content replaces them — matching how datasets ride
  along on Revise.
- Thumbnails are *derived* content: rendered from a revision's geometry into a
  cache (off the synced volume), keyed by source file identity — never
  committed, always regenerable.

## Presence is data, not a guess (added 2026-08-12)

**Every read that a UI row is built from carries `thumb`: the thumbnail URL, or
`null` when that revision has no geometry at all.** One field name, one meaning,
everywhere — `/api/items/{n}` (per revision, per BOM line, per where-used
parent, per relation), `/api/items/{n}/bom` (tree, `rolled=1`, and the tree's
lazy child loads), `/api/items/{n}/plan`, `/api/revisions/{id}`,
`/api/products` (card + `top_items`), `/api/activity`, `/api/approvals`,
`/api/search`, and MCP `get_item` / `get_bom` / `list_related`.

`null` means *do not ask*. A client that guesses instead — "this row might have
a picture, let's try" — turns the registry's own knowledge into a per-row 404:
one open assembly with 23 purchased/hardware children was 23 doomed requests per
load, and the client's rendering-not-ready retry re-fired the ones with a
geometry-bearing kind three more times each.

The presence check is `plm/thumbs.py:has_geometry(conn, rev_id, kind, memo=)` —
the boolean twin of `geometry_bodies`: DB only (no vault reads, no OpenSCAD),
**short-circuiting** at the first body found, **kind-aware** (only
part/assembly/coupon can inherit geometry through a BOM, so a screw or a
document never walks a structure — its own files are still checked), and
**memoized per payload** via `thumbs.Presence(conn)`, so a child on three BOM
lines is answered once. Cost on GL-ASM-0000 (3 revisions × 25 lines): 76 indexed
SQLite reads, ≈1 ms.

Because presence is now data, `thumb: null` is *also* the answer to "is there
anything to view in 3D": the item page's View 3D button and its click-through
image are disabled from the same field rather than discovering emptiness inside
the viewer. A **404 on a `thumb` URL that IS present** keeps its old, narrower
meaning: the PNG is not rendered *yet* (the request that asks for it is often
the one that triggers the OpenSCAD run) — which is what the client's backoff
retry is for.

## Fabrication: routings, raw stock and the cut list (added 2026-08-13)

> **The point, in one sentence:** plm turns fabricated-part structure into
> **actionable cut/drill/notch/weld work**, and does *not* classify every
> non-printed child as something to buy.

Before this, `CCR-ASM-0002` — a camper rack made of eleven repeatable pieces of
1×2×0.120 tube and Sch 40 pipe — reported **11 parts to print and 0 to buy**, and
every one of the eleven said "no STL". The manufacturing facts existed, as prose,
in free-text attributes: `fabrication_operations = "CUT square; NOTCH 3 receiver
saddles at Y -9/0/+9 in; WELD in rear rack"`, `make_buy_disposition = "MAKE"`,
`cut_length_in = "86"`. A sentence cannot be ordered, queried, nested or costed;
`cut_length_in` puts the unit in the *name*, which `plm/attributes.py` explicitly
forbids; and the cut list itself was a **hand-maintained `CUT_LIST.md`** with a
hand-computed nest. Code: `plm/fab.py`.

### `process` — the make/buy/print word, and why it is that word

A `revisions` column, values `'' | print | make`, **validated** because it
dispatches `_build_plan`. Merged into the attribute read surface so a caller sees
it beside `material`, but **refused on write there** (`attributes.ELSEWHERE`),
pointing at `set_process` — because an attribute is a MEASUREMENT (you may weigh
a released part) while `process` is DESIGN INTENT, so it is DRAFT-only like the
five BOM verbs.

- **`process`, not `disposition`.** `docs/PLM_CHANGES.md` already uses
  `disposition` for the ECN affected-item concept (`pending|revise|use_as_is|
  no_impact`). One word, one meaning.
- **⭐ There is deliberately no `process='buy'`.** "Buy it" is already answered by
  `kind` (`purchased`/`hardware`). A second home for one fact is how a registry
  rots. So the *build plan* gains a MAKE bucket while the *field* only has to
  separate print from make.
- **`''` means "not stated", and for a part that reads as PRINT** — exactly what
  the plan did before the column existed. So the migration is one
  `ADD COLUMN … DEFAULT ''` and **zero rows changed**: every lantern part keeps
  printing, verified byte-identical.

### MAKE in `_build_plan`: a second allow-listed axis, never an inversion

`_build_plan`'s docstring warns that its kind predicates are allow-lists and that
this is load-bearing. kept that property by adding a second **allow-listed**
axis rather than inverting anything:

| kind | process | bucket |
|---|---|---|
| `part` | `make` | **MAKE** — a fabricated piece |
| `part` | `''` or `print` | PRINT (unchanged default) |
| `assembly` | `make` | **MAKE** — a **weldment**, with its own joining/finishing routing |
| `purchased`, `hardware` | (n/a) | BUY (unchanged) |
| anything else | anything | neither (unchanged) |

Nothing reaches a shop bucket except by matching a rule that names it, so a kind
*or a process* that must never reach the shop cannot reach it by being forgotten.
The plan carries `cutlist` whenever `make` is non-empty; `/api/products` passes
`cutlist=False`, because a dashboard **counts** and must not pay for a bin-pack.

### The routing: one table, on the REVISION

```sql
operations(id, revision_id, seq, op, params JSON, work_center, notes, updated_at)
```

- **REVISION-level**, for the reason attributes are: geometry changes change the
  work. Shorten a rail and the CUT length moves; relocate a receiver and the
  NOTCH datums move. Carried forward on `revise()`/`fork()` by `fab.copy`, right
  beside `attributes.copy`.
- **ONE TABLE, not a routing/operation/resource trio.** An operation is "a verb,
  a place in the order, and some numbers", and the numbers differ per verb —
  which is a JSON `params` column with a registry gloss, the shape `attributes`
  and `relations` already took.
- **Deliberately NOT built:** a work-centre master, standard times, setup times,
  a resource/capacity model. There is one chop saw, one drill press, one grinder
  and one MIG welder, so `work_center` is a text hint; and a fabricated standard
  time on a controlled record is worse than no time at all. When a second machine
  appears, `work_center` earns a table and this surface does not change — that is
  the seam.
- **DRAFT-only.** A routing is manufacturing definition, and a released routing is
  what somebody approved. `replace=True` (the default) swaps the whole sequence,
  which makes an agent regenerating from CAD idempotent.
- **Exactly one stock-consuming operation per revision** (`CUT`, or `CUT_PROFILE`
  on the sheet lane). A cut part is ONE piece of stock; two pieces is two parts,
  and that is what the weldment's BOM is for. Without this the nest would silently
  double-count a stick.
- **⭐ A WELD BELONGS TO THE WELDMENT.** `WELD`/`FINISH` go on the *assembly's*
  revision. "WELD in rear rack" written on the rail is a piece claiming to know
  about an assembly. **Weldment membership is the BOM and needs no relation** —
  what makes the joint a weld rather than a bolt is the operation.

**The vocabulary is a REGISTRY DICT** (`fab.OPERATIONS`), for the reason
`relations.REGISTRY` is one: adding a verb is a dict entry, not a feature and not
a migration. `CUT · DRILL · NOTCH · COPE · BEND · DEBURR · WELD · FINISH`, plus
`CUT_PROFILE` and `SCRIBE` **already registered for a CNC-plasma lane**,
so that lane ships a consumer rather than a schema change.

**Gate on the verb, gloss on the params.** Unlike attributes (pure gloss) and like
relations (pure gate), this registry is both, on purpose: a typo'd verb (`NOCH`)
is work no shop surface will ever route, so the name is validated; a param nobody
has thought of yet is still a fact somebody measured, so params are open and
merely reported as `extra`.

### Raw stock is a `purchased` item

Not a new kind and not a new number class — see `docs/PLM_RELATIONS.md`
§`cut_from` for the full argument. What makes a purchased item *stock* is ordinary
attributes on its revision: `stock_form` (`linear` | `sheet`), **`stock_profile`**
(the nesting group key, so it needs one spelling), `stock_section` (the human
spec), `stock_length` (the purchasable stick), `kerf`, `remnant_min`.

Those stay a **gloss**, so a typo lands — and the **nest** refuses it by name, at
the point where it matters (`stock_form='lineal'` → "known forms: linear, sheet").
Permissive store, strict computation.

### Units: his stock is imperial and it stays imperial

86-inch rails, 1-inch Sch 40, 0.125-inch kerf; the plasma table talks mm.
`plm/attributes.py`'s rule applies without exception — **carry the unit, never
default it** — and here it is a *gate*, not a gloss, because plm owns the
conversion table:

- Every dimension is `{"value": 86, "unit": "in"}`. A bare `86` is refused: *"a
  bare 86 does not say what it is measured in, and a cut list may not guess (86
  what?)"*.
- A unit plm cannot convert is refused **at the write**, not three days later on a
  cut list. Lengths: `mm cm m in ft yd`. Angle params take `deg` and nothing else
  (an angle is never converted).
- **Conversion happens in exactly one function** (`fab.convert`), only when a nest
  group mixes units, and always **into the stock's own unit** — the stick is what
  you buy, and the purchase order is the thing that has to be right. Proven live:
  a `914.4 mm` cut nested onto a 120-inch stick as `36.0 in`, with the cut row
  still reporting the length the engineer authored.

### The cut list and the 1-D nest

`fab.cut_list(rev_id)` walks `db.rolled_bom`, and for every `process='make'` line
emits the finished length with its unit, the quantity **already multiplied through
the weldments**, the end treatments, the kerf **and where the kerf value came
from** (operation → stock → request → plm default), the follow-on operations, and
the weldment paths that got there. Groups by stock profile; nests each group.

**Nothing is raised for one bad part.** A cut list that refuses wholesale because
one of twelve parts lacks a unit is useless: every problem lands in `issues`,
named, **with the call that fixes it**, and the other eleven still nest.

**Algorithm: first-fit-decreasing (FFD), 1-D, deterministic.** Pieces are expanded
one per required quantity, charged `length + allowance + kerf`, sorted by
descending charged length with `(part number, instance index)` as the tie-break,
then placed in the first stick with room. No randomness, no dict-order dependence
— five identical requests hash identically. **Its honest limits are shipped in the
payload** (`plan.algorithm.honest_limits`) rather than buried here:

- **FFD is not optimal** — at most `11/9·OPT + 6/9` bins, and on a bad instance it
  really does buy one stick more than a human with a pencil. Chosen because it is
  deterministic, explicable and never under-buys.
- **Kerf is charged for every piece**, including the last off a stick —
  over-reserving by at most one kerf per stick, deliberately in the conservative
  direction.
- **No drop inventory.** Sticks start full, so the count is "what to buy if you had
  nothing": right for a purchase order, wrong for a Saturday.
- **No cut sequence, orientation, grain or defect avoidance**, and **miters are not
  nested head-to-tail** — under-optimising, never under-buying.

**Waste is kerf + unusable drop; a remnant worth keeping is not waste.** Each group
reports `yield_pct` (how much steel became parts), `waste_pct`, `kerf_total`,
`scrap_total` and `usable_remnant_total` separately, because calling a keepable
84-inch remnant "waste" is how a shop convinces itself it is 30% wasteful when it
is 6% wasteful with a full rack of drops.

**⛔ A 2-D nest never goes in plm.** `stock_form='sheet'` is refused by
`fab.cut_list` with the reason: a 2-D nest is bound to one physical remnant on one
machine at one moment, and plm's revisions are immutable. That nest lives in
the shop-floor service. plm still owns the sheet **count**, which is a purchasing question.

### Readiness for a fabricated part is its ROUTING, not a mesh

`thumbs.has_own_stl` (`path LIKE '%.stl'`) is left **unchanged** — it answers "is
this printable today", and a DXF does not make something printable. The bug was
asking that question about a fabricated part. Fixed at both surfaces: MAKE rows no
longer enter the print list (so the "no STL" column never sees them), and the item
page now reads *"No mesh — and it does not need one. This is a FABRICATED revision:
its definition is the routing below."*

### The surface

| | |
|---|---|
| HTTP | `GET /api/fab/registry` · `GET`/`PUT /api/revisions/{id}/operations` · `PUT /api/items/{n}/operations` · `PATCH /api/items/{n}/process` · `GET /api/items/{n}/cutlist` · `GET /api/items/{n}/cutlist.md` |
| MCP (5 new, 55 total) | `fab_registry` · `set_process` · `set_routing` · `get_routing` · `cut_list` |
| Reads | `GET /api/items/{n}` carries `operations` per revision (like `attributes`); `_build_plan` carries `make` and `cutlist`; routings are indexed for search, so "notch" or "saddle for 1.660 OD" finds the part |
| UI | MAKE table + per-profile stick/yield/waste lines + a cut-list link on the build plan; a read-only **Routing** card on the item page; `n_make` on the dashboard card |

### CCR v2, as shipped

`CCR-ASM-0002` v0.2.0 → **11 cut parts, 30 pieces, 3 profiles, 6 sticks**, 0
issues. The generated nest reproduces the hand-computed one in
`camper-conduit-rack/README.md` **exactly**: 3 × 120 in tube (remnants 15.75 /
15.75 / 83.875), 2 × 120 in 1-inch Sch 40 (6.125 / 16.875), 1 × 48 in 1-1/4-inch
Sch 40 (3.625). The plan went from *11 to print / 0 to buy* to *15 to make
(11 cut parts + 4 weldments) / 0 to print*.

## Structure views and material rollup (added 2026-08-11)

- **The dashboard groups by PROJECT** (`/api/products`, reworked 2026-08-11):
  one card per project, listing its top-level items (assemblies no BOM uses;
  falling back to unused PARTS with revisions for a project too young to have an
  assembly) with a `primary` = the most recently active one. The previous
  one-card-per-top-assembly shape answered a question about structure while
  looking like an answer about projects — three related bench-test assemblies
  read as three products, and a project whose root was still a part was invisible.
- **BOM views** follow Teamcenter's PSE: the item page offers **Tree**
  (expandable nested occurrences, lazily loaded per level), **Flattened**
  (recursive rollup with multiplied quantities), and **Purchasing** (the buy
  cut with sourcing). Same data, three reads.
- **Material master data lives on the revision** (`material`, `mass_g`) —
  geometry changes change mass, so it can't live on the item. The AI-age
  twist on Teamcenter's mass-properties rollup: the **slicer is the
  instrument** — after slicing, the grams are posted back
  (`POST /api/items/{n}/material`, MCP `set_material`), and the build plan
  rolls up Σ(qty × mass) per material with a waste allowance (default 15%,
  tunable per query) for supports/brims/failed prints. Unknown-mass parts are
  called out rather than silently counted as zero.
- **The build plan has THREE buckets, not two** (2026-08-13): `print`,
  **`make`** and `buy`, plus a generated `cutlist` when there is anything to
  make. All branches are allow-lists on `(kind, process)` — see §Fabrication for
  the table, and for why that property is load-bearing.

## Assembly placement (added 2026-08-11)

Per-part print STLs are usually exported print-oriented, so composing them by
shared coordinates stacks parts at the origin. Three tiers, in order:

1. **`viz` derivatives (now).** An assembly revision carries its own
   *assembled* STL, role `viz` — regenerated from the revision's stored
   `source` + `selector` recipe (e.g. `lantern_top_drive.scad ·
   drive_part=assembly`). Viz files may attach to frozen revisions (derived
   geometry is not a design change — Teamcenter's JT-on-release). The
   geometry resolver and thumbnailer prefer `viz` over print artifacts.
   **Check-in convention: every assembly revision should include an assembled
   STL (or expect the backfill to generate one from its recipe).**
2. **Occurrence transforms (next).** The real Teamcenter answer: a 4×4 (or
   pos+rot) transform on the BOM occurrence, applied to each child's mesh in
   the viewer — restores per-part explode/x-ray on assemblies. Needs CAD that
   exports placements: CadQuery assemblies and STEP both can; OpenSCAD needs
   the scad to expose per-part placement. Schema seat is reserved
   (`bom.transform`, JSON) — populate when the CAD side can emit it.
3. **In-place part exports (convention).** Parts exported in design
   coordinates compose correctly with zero extra data — worth doing for new
   multi-part designs; incompatible with print orientation, so usually as an
   extra viz export, not the print artifact.

## Sources

- [PLM Teamcenter — Database Design — BOM (Structures)](https://medium.com/@mjvibhute/plm-teamcenter-database-design-bom-structures-3105932a4599)
- [Teamcenter Open Gate: Precise vs Imprecise](https://teamcenter-open-gate.blogspot.com/2016/12/precise-vs-imprecise.html)
- [Revision Rules in Teamcenter (TCPLMBASICS)](https://tcplmbasics.com/2018/03/21/revision-rules-in-teamcenter/)
- [Revision Rules (NX/Teamcenter help)](http://www2.me.rochester.edu/courses/ME204/nx_help/en_US/tdocExt/content/s/revision_rules.xml)
- [Attach Dataset to Item Revision (Mendix/Teamcenter docs)](https://docs.mendix.com/appstore/industry/teamcenter-extension/integrations/attach-dataset-to-item-revision/)

### Viewer component labels (2026-10-19)
The 3D body list uses the explicit comparison label when present, otherwise the
resolved item name (then group name), falling back to the mesh filename only for
unnamed geometry. File names such as `posed.stl` identify derivatives, not parts.
Tooltips retain item number and source mesh path; visibility/solo still selects
the individual body. This reuses geometry_bodies metadata, without a new label store.
