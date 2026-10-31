# Branching, forking, and suppression

*2026-08-11. Design note, per the maintainer: "consider how to branch/fork parts — and
where suppressed features live."*

## Forking: how the grown-up PLMs branch

Production PLMs almost never branch a single item's revision chain. The tools
that tried (branch revision IDs like `A.1`) are famously avoided in practice,
because two "current" revisions of one part number breaks the one question a
registry must answer: *what is current?* Instead the industry does three
different things for three different needs:

| Need | Industry answer | plm answer |
|---|---|---|
| Short-lived parallel work on one item | Check-out lock + working (DRAFT) revisions | already built |
| A divergent purpose ("like X, but for the van") | **Save As / fork**: a NEW item number, deep-copied from a source revision, with a recorded **derivation link** (Windchill "Save As", Teamcenter derived-from relation) | `fork` operation + `relations` table |
| Many variants of one design | Parameters / variant conditions, not copies | a revision's `source` + `selector` binding — OpenSCAD's `-D` flags ARE the variant mechanism |

So in plm:

- **`POST /api/items/{n}/fork`** creates a new item (new number, new name),
  seeds its v0.1.0 DRAFT from a chosen source revision (files + BOM carried,
  same mechanics as revise), auto-writes a context entry ("Forked from
  KML-PRT-1002 v0.2.0 — <reason>") and records a `derived_from` relation.
- The **`relations` table** is the general mechanism (`derived_from` now;
  `replaces`, `alternate_for` later). Item pages show lineage both ways:
  "derived from X" and "forks: Y, Z".
- **Merging back** is editorial, not mechanical: a revise on either item that
  adopts the other's files, with a context entry and (optionally) a
  `merged_from` relation. CAD forks rarely merge like code; the context trail
  is what matters.
- **Git branches are NOT the fork mechanism.** The vault is one repo; a fork
  is a new folder + provenance in the index. Repo-level branches stay reserved
  for whole-environment experiments — that's what the sandbox instance is.

## Suppression: where it lives

Two genuinely different things hide under "suppressed", and they live in two
different places:

1. **Geometry features** (a boss, a vent pattern, an eyelet toggled off) —
   this is the CAD source's business, and in a parametric world the record is
   the revision's **parameter binding**: `source=lantern_top_drive.scad`,
   `selector=drive_part=top_cover with_eyelet=false`. The revision already
   stores exactly which features were on when its artifacts were generated.
   Nothing new to build; the discipline is "never hand-edit an STL — change
   params and re-check-in."

2. **BOM occurrences** (a line that exists in the master structure but is
   inactive for this build) — the industry's 150% BOM: the parent's structure
   carries everything, suppression/variant conditions filter it. plm adds a
   **`suppressed` flag on the BOM line**:
   - suppressed lines stay visible in the BOM table (greyed, struck) — they
     are part of the design record;
   - they are **excluded** from rolled-up quantities and from the 3D viewer's
     geometry resolution;
   - flipping suppression is a normal write (evented), and because BOM rows
     belong to a parent *revision*, suppression is versioned exactly like
     every other structural fact — an old revision keeps its old suppression.

Variant *conditions* (suppress line X only in the "vented" build) are a later
layer: a `variant` tag on the line plus a chosen variant at view time. The
flag ships now because it is the storage the conditions would filter anyway.

## CAD-agnostic core (the Teamcenter posture)

plm must not be an OpenSCAD tool that grew a database. The neutral-core
pattern the big PLMs use maps directly onto what's already here:

- **Native files are opaque blobs with provenance.** A revision's files can be
  `.scad`, a CadQuery `.py`, a Fusion `.f3d`, SolidWorks — the registry never
  parses them. `revisions.source` + `selector` (how to regenerate) are already
  free-text, CAD-agnostic bindings.
- **Every revision carries derivatives by role**, which is where the pipeline
  gets its CAD independence (Teamcenter: native + JT for viz + STEP for
  exchange):
  - `source` — the native CAD file(s);
  - `artifact` **mesh derivative (STL/3MF) — required for viz**: the viewer,
    thumbnails and assembly composites consume ONLY this role, so any CAD that
    can export a mesh is fully supported today;
  - `exchange` (STEP) — for CAD-to-CAD moves (CadQuery already emits it);
  - `drawing`, `doc` as before.
- **Fusion specifically**: no headless local API, so the Teamcenter-style
  answer applies — the check-in includes the `.f3d` plus exported STL/STEP,
  the same way CAD workstations generate JT on save. An agent-side helper can
  later drive the Fusion cloud API to automate the export.
- **Assembly renders** come from the PLM, not the CAD: the composite
  thumbnailer imports every unsuppressed occurrence's mesh derivative into one
  scene (shared coordinates), so "what does this assembly look like" works
  regardless of which CAD authored each child.
- STEP-only revisions (no mesh) are the one gap: meshing STEP needs OCCT — the
  workspace's CadQuery container already has it, and a `step→stl` derivative
  hook in the check-in path is the planned fix.
