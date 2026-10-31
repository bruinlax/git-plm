# Relations as a first-class surface — coupons are the second application

**STATUS: IMPLEMENTED 2026-08-11.** `plm/relations.py` (the REGISTRY + all
reads/writes), migration in `plm/db.py` (`relations.from_version`, `.actor` —
additive; existing derived_from rows untouched), kind `coupon`/class `CPN` in
`plm/config.py`, BOM guard `ops.COUPON_IN_BOM` on both write paths
(`checkin` pre-flight and `add_bom_line`), HTTP `GET /api/relations/types`,
`GET /api/items/{n}/relations`, `POST /api/relations`,
`DELETE /api/relations/{id}`, `POST /api/items/{n}/coupon`, `related` in
`GET /api/items/{n}`; MCP `relation_types`, `link_items`, `unlink_items`,
`list_related`, `add_coupon` (38 tools), `related` in `get_item`, relation
filters on `find_items`; one generic "Related items" card in `web/app.js`.
Deviations from the brief below: revision pointers are TEXT versions rather
than `from_rev_id`/`to_rev_id` (same idiom as `bom.child_version`, and what the
existing rows already hold), and `fork()` now writes its `derived_from` through
`relations.add` so provenance is evented like every other link. Sandbox demo:
`KML-CPN-0001` verifies `KML-PRT-1011` v0.3.3.

**2026-08-13:** added `cut_from` (part → purchased, item-level) — the
raw stock a fabricated part is cut off. **One dict entry and nothing else**: no
table, no endpoint, no UI card, and it made *weldment → cut part → raw stock*
queryable in both directions for free. See "`cut_from`" at the end of this file
and `docs/PLM_DATA_MODEL.md` §Fabrication. MCP `tools/list` = 55 tools.

**2026-08-13:** added kind `asset` / class **AST** and four relation
types — `mounts_to`, `installed_on`, `integration_envelope_for`, `instance_of`.
See "Assets" below. MCP `tools/list` = 45 tools (`add_asset` is the new one).

**2026-08-12:** added `bench_test_of`, and relations became a **context target**
— every row of `relations.for_item()` carries a `comments` count, and
`add_context(target_type='relation', target_id=…)` attaches the reasoning to the
LINK (docs/PLM_DATA_MODEL.md, "Context is GENERIC"). A link is a thing people
question; the answer now lives on it.

## The registry (plm/relations.py — THE source of truth; this is a gloss)

| type | label / inverse | level | from-kinds | what it is for |
|---|---|---|---|---|
| `derived_from` | derived from / forks | item | any | provenance of a fork (written by `fork()`); a fork diverges and stops tracking its source |
| `coupon_for` | verifies / verified by | **revision** | `coupon` | a test coupon validates ONE revision — fit, tolerance, print settings. A coupon for v0.3.0 says nothing about v0.4.0 |
| `bench_test_of` | bench test of / bench tested by | item | `assembly`, `part` | a rig, fixture or partial reprint made to exercise another item. Item-level: a rig follows the thing it tests across revisions |
| `mounts_to` | mounts to / mounting point for | item | any → **`asset`** | the physical INTERFACE: this design bolts/clamps/hangs on that asset. Item-level, because an interface follows a design across revisions |
| `installed_on` | installed on / has installed | **revision** | any → **`asset`** | the STATE: this article is on that asset *right now*, and `from_version` says which revision is. Two racks `mounts_to` one camper; one is `installed_on` it |
| `integration_envelope_for` | integration envelope for / constrained by envelope | **revision** | **`asset`** → any | reference geometry that constrains a design without entering its structure. Both ends pinned: "does it fit?" is a question about one scan and one design |
| `instance_of` | instance of / instantiated by | item | **`asset`** → any | the specific unit (measured) vs the type/model (published). Point fit work at the instance |
| `cut_from` | cut from / stock for | item | `part` → **`purchased`** | the RAW STOCK a fabricated part comes off. Item-level: which stock a design is cut from follows it across revisions, and the cut LENGTH is a revision fact that lives in the CUT operation |

`from_kinds`/`to_kinds` empty = any kind. Unknown types (rows written by newer
code) degrade to a readable label rather than vanishing: the table is the record,
the dict is the gloss.

### Assets: the thing you attach to, which you neither build nor buy (2026-08-13)

A Codex bot modelling the camper rack hit a category plm did not have, correctly
refused to smuggle a camper into the rack's BOM as a purchased line, and filed
the ticket. The missing category: **a camper, a truck bed, a garage wall, a bike
frame** — reference geometry for fit, never a BOM line, never a purchase.

**Kind `asset`, class `AST`.** A real item in every other respect: revisions,
files (the scan / the parametric envelope / viz STLs), measured dimensions as
revision attributes, context. `AST`, **not `REF`** — the class token is a
projection of `kind`, and `GL-REF-0001` is the number the MCP SOP names as
forbidden precisely because it smuggled a classification into identity while
there was no kind to carry the fact. The fix for a missing concept is a kind and
a relation, never a class token you invented.

**Structurally barred from BOMs, exactly like coupons**, plus one exclusion
coupons do not need — *purchasing*:

| Surface | How an asset is kept out |
|---|---|
| BOM (both write paths) | `ops.NOT_IN_BOM` — `checkin` pre-flight (before any write, so a refused line cannot leave half a check-in) and `add_bom_line`. Judged by the number's class when the child does not exist yet, so `CCR-AST-0001` is refused on first mention |
| `reclassify_item` | refuses to turn a BOM-used item into an asset, and *names the parents*; that op is the third door into the same illegal state |
| `db.rolled_bom`, material rollup, `_build_plan.buy` | free, and deliberately so: assets cannot be in a BOM, so they cannot reach a rollup. `_build_plan`'s kind predicates are **allow-lists** (`part` prints, `purchased`/`hardware` buys) — never turn either into a deny-list |
| `items.product` / the dashboard | `ops.set_product` **refuses** a non-structural kind (one write path shared by HTTP + MCP), and `/api/products` filters `kind NOT IN (config.NON_STRUCTURAL_KINDS)` at read time so a row flagged before the guard still cannot front a card |
| `_top_items` | keeps its `kind=` predicate. LOAD-BEARING: an asset is unused in every BOM *forever*, so a bare "unused items" query selects exactly the wrong rows |
| registry counts / search / activity | **not** excluded — an asset is real work and should be findable. `/api/stats.kinds` gains `asset` |

`config.NON_STRUCTURAL_KINDS = ("coupon", "asset")` is the one list; adding a
future non-structural kind is one entry.

**Type vs instance: one kind, plus `instance_of`.** The model ("Four Wheel Camper
Hawk flatbed") and the specific one in the driveway are different objects, but
they are not different *kinds*: both need identity, revisions, files, attributes
and context, and the only difference is how they relate to the world. That is the
same argument the bench-test section makes — **the role is a relation, not a kind,
and never a number**. One kind also means one token in every exclusion predicate;
two kinds would mean every guard has to remember both, and the whole risk here is
an asset leaking into a plan. So: create the *instance* (its measurements are the
datum), and only create the type when there are two units or the published spec is
worth keeping beside the measured one.

**Three relations, not one word.** `mounts_to` is an interface, `installed_on` is
a state, and `integration_envelope_for` is a constraint. One word for all three
breaks the moment there are two racks and one camper — which is the actual prod
situation. `installed_on` is revision-level *because the from-version is the
point*: it records which revision is bolted on (the as-built fact).
`integration_envelope_for` pins both ends, which is what makes "does the rack
fit?" answerable from data — and what the separately-scoped 3D markup feature
will redline against.

**Measured dimensions are attributes** (`plm/attributes.py` WELL_KNOWN gained
`overall_length/width/height`, `mount_spacing`, `mount_hole_pitch`,
`mount_hole_dia`, `mount_hole_count`, `load_rating`, `serial_number`). Those names
carry **no unit and no default unit**, deliberately unlike `bbox_x_mm`: a slicer
is always in millimetres, but a camper in a Californian driveway is measured in
inches, and a silent `mm` default would make one of them a lie. Pass the unit.
Owner/location are *not* attributes — they are item-level facts that change
without the geometry changing, so they go in context.

**Prod (2026-08-13):** `CCR-AST-0001` "Four Wheel Camper Hawk flatbed on Norweld
tray" — the instance, LiDAR-scanned 2026-08-10, no model year invented because no
document states one. `CCR-ASM-0001` (built, welded Aug 2026) `installed_on` it at
v1.0.1 **and** `mounts_to` it; `CCR-ASM-0002` (v2 concept, DRAFT) `mounts_to` it
and nothing more, because nothing is fabricated; the asset is the
`integration_envelope_for` both. `fwc_hawk.scad` had been living on
`CCR-ASM-0002` v0.1.0 as a `role=doc` file for want of anywhere else — it now
belongs to the asset. Sandbox demo: the `W58-*` items in project `sbx`.

### Bench tests are V&V artifacts, not product structure (2026-08-12)

The maintainer's dashboard complaint made the pattern concrete: the lantern's product card
was fronted by a bench-test rig, because a test build registered as a top-level
`assembly` is indistinguishable from a product by topology alone. Two correct
homes, and `bench_test_of` is the second one:

- **validates ONE revision** → it is a **coupon** (kind `coupon`, class `CPN`,
  `coupon_for`). Structurally barred from BOMs and product surfaces.
- **a rig/fixture that follows an item across revisions** → keep its real kind
  (an assembly of real children IS an assembly) and link `bench_test_of`. The
  role is a relation, not a kind — and never a number.

Neither is ever `items.product`. The bike-projector's four `GL-PRT-90xx` "fit
coupon"/"fit fixture" parts became `GL-CPN-0002…0005` (`ops.reclassify_item`, old
numbers still resolve); `KML-ASM-1900/1901` kept `kind=assembly` and gained
`bench_test_of KML-ASM-0000`.

*Design brief (the maintainer, 2026-08-11). Build after lands (same files).*

## The generic model (this is the point — keep it generic)

The `relations` table already links items (`derived_from`, written by fork).
Promote it from an internal detail to a **first-class, typed, queryable
surface** so every future link (coupons today; fixtures, test reports,
supersedes, mating-parts tomorrow) is one row, not one feature:

- Schema: relations(id, type, from_item_id, from_rev_id NULLABLE,
  to_item_id, to_rev_id NULLABLE, note, created_at, actor). Revision-level
  pointers are nullable — `derived_from` is item-level, `coupon_for` points
  at the SPECIFIC revision it validates (a coupon for v0.3.0 says nothing
  about v0.4.0). Additive migration; existing derived_from rows keep
  working.
- A relation-type REGISTRY in code (dict): type → {label, inverse_label,
  from_kinds?, to_kinds?, rev_level: bool}. e.g. coupon_for → label
  "verifies", inverse "verified by", rev_level True. UI and MCP render from
  the registry — adding a type is one dict entry, zero new endpoints.
- API: GET /api/items/{n}/relations → BOTH directions, typed, with the
  other side's number/name/kind/rev + `thumb` (its render URL, or **null**
  when that revision has no geometry — presence is data, see
  PLM_DATA_MODEL.md; it used to be an unconditional URL, so every link to a
  document or a mesh-less coupon was a guaranteed 404). POST /api/relations
  {type, from, to, from_version?, to_version?, note} + DELETE (with events).
- MCP: get_item returns a `related` block (typed, both directions —
  agents must SEE "this part is verified by CPN-0003, derived from
  PRT-9001" without extra calls); tools link_items(type, ...) /
  unlink_items; find_items can filter by relation (e.g. coupons_of=NUMBER).
- UI: one generic "Related items" card on the item page rendering from the
  registry (grouped by type label, thumbnails, links both directions).

## Coupons (first application of the generic surface)

- New kind `coupon`, class CPN (KIND_BY_CLASS/CLASS_BY_KIND in config) —
  auto-numbers like KML-CPN-0001. Real items: revisions, files, viz,
  context — tracked like anything.
- NEVER in BOMs: excluded structurally (nothing adds them); registry/build
  plans/product cards must not count them (verify rolled_bom, /api/products,
  plan, dashboards ignore coupon-kind items; add a guard: BOM lines whose
  child is a coupon are refused with "coupons relate, they don't assemble").
  This is the Teamcenter posture: coupons are process/verification
  artifacts (V&V trace links), not EBOM occurrences. Distinct from
  SUPPRESSION, which is for real product parts conditionally absent.
- Creation flow: MCP create_item(kind="coupon") + link_items("coupon_for",
  CPN, PRT, to_version=...) — or a convenience add_coupon(name, for_number,
  for_version, ...) that does both. Fit results = context on the coupon
  revision; if it drives a change, the next revision's summary cites it —
  search then answers "why did the bore change" through the coupon trail.
- Part page shows its coupons (with state stamps + thumbs) in the Related
  items card; coupon page shows what it verifies.
- Registry table: coupons get a kindchip and are filterable; default
  registry view may include them (they're real work) but product/build
  surfaces never do.

## `cut_from`: raw stock, and why it is a relation (2026-08-13)

A fabricated part is cut off something you bought. Recording *what* is the
fourth-and-last fact `fab.cut_list` needs (`docs/PLM_DATA_MODEL.md`
§Fabrication), and it is a **relation**, not a column, for three reasons that
between them are the argument for the whole registry:

1. **`relations.for_item` already returns both directions, already labelled.**
   The part page reads "cut from CCR-PUR-0001"; the steel's page reads "stock
   for" and lists all four rails cut off it. The ticket's requirement was that
   *weldment → finished cut part → raw stock* be queryable **both ways** — and
   a registry entry delivered that with no new read path, no new UI card and no
   new MCP tool. A column would have answered one direction and needed all three.
2. **A dict entry is the whole change.** No table, no endpoint, no `if type ==
   "cut_from"` anywhere. That is the property this file exists to protect.
3. **Item-level, on the `mounts_to` / `bench_test_of` precedent.** Which stock a
   rail comes off follows the design across revisions, and `revise()` does *not*
   copy relations — a revision-pinned link would go stale at the next patch bump
   and silently drop the part out of the cut list. What *is* revision-level is
   the **cut length**, and that lives where it belongs: the CUT operation's
   `length` param, with its unit.

**The stock is `purchased`, not a new kind.** It is bought, it has a supplier and
a purchase length, it belongs in the BUY list, and `/api/products` must keep
seeing it. A `stock` kind would have to be added to `_build_plan`'s allow-list to
be bought at all — and the day somebody forgot, the steel would vanish from
purchasing. Same lesson as `AST`: the fix for a missing concept is a kind **or a
relation**, and here a relation was enough.

**Normally not a BOM child — but that is guidance, not a guard.** Purchased items
are legitimate BOM children (screws are), and a purchased length used *whole and
uncut* is a fine BOM line. What is wrong is listing a 10-foot stick as an
occurrence in a weldment that consumes 18 inches of it. The stick is what you
**buy** (and `fab.cut_list` says how many); the piece is what you **build with**,
and the piece is the part.
