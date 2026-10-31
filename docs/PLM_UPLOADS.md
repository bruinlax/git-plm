# Large files in: links and handles, not base64

**STATUS: live.** Two ways to get a big file into plm without base64:

* **`url`** — the main road. An agent stages a file ONCE with the **filedrop
  MCP** and hands the link to plm (and to comms, the shop-floor service and whatever comes
  next). plm streams it down, verifies the sha256 it was promised, and never
  deletes the source object — it is not plm's.
* **`handle`** — plm's own `upload()` inbox, a directory on plm's disk
  (`CACHE_DIR/inbox/<token>/<name>`). The client PUTs the raw bytes to plm
  itself — `PUT /api/uploads/<token>`, with its bearer token like every
  `/api/` write — and checks in with the handle `staging/<token>/<name>`
  (`filedrop_key` is the legacy alias of the field). plm minted it, so plm
  deletes it once the revision owns a copy.

`file_link` answers `/api/blob?id=` for every row, private-network-only.

## Transport vs storage (the rule this document exists to obey)

Four transports, one destination, and the choice of transport says NOTHING about
where bytes land:

| Transport | When |
|---|---|
| `text` / `content_b64` | small documents and genuinely small binaries |
| **`url`** | **anything big — the filedrop MCP's `get_url`, or any network-reachable https URL** |
| `handle` (legacy alias `filedrop_key`) | plm's own `upload()` handle |
| raw `PUT /api/items/{n}/files/{name}` | scripts and `curl -T` |

Where the bytes end up is `plm/storage.py::choose_backend`'s decision alone — vault
for text/source/viz, offload for bulk binaries. Every transport above feeds the
same `ops.checkin` / `ops.upload_files`.

## The url form

```
agent -> filedrop MCP: upload('bracket.stl')  -> {put_url, get_url, handle}
agent -> curl -f -T bracket.stl '<put_url>'      bytes straight to filedrop
agent -> filedrop MCP: stat(handle)           -> {sha256, size} as STORED
agent -> plm: checkin(files=[{name, url: <get_url>, sha256, size, role}])
plm   -> streams the link down (sha verified while streaming) -> vault or offload
```

Accepted by `checkin`, `put_file`, `put_viz` and `share_3d` (MCP), and by
`POST /api/items/{n}/checkin`, `/files`, `/viz` and `/api/snippets` (HTTP).

| Rule | Why |
|---|---|
| **`sha256` is REQUIRED** with `url` (a 400 refuses without it) | plm holds these bytes for a moment and writes an immutable revision; a link is somebody else's promise, so it comes with the hash it promises. Verified while streaming; a mismatch is a 400 printing both values and nothing is written |
| **https only** | the presigned filedrop GET is the normal case; plaintext fetches are refused |
| **capped at `PLM_RAW_UPLOAD_MAX`** (192 MiB) during the download | a hostile or mistaken link cannot fill the disk |
| **plm never deletes the source object** | it may not own it. A filedrop MCP inbox object belongs to the agent that staged it and may have been handed to more than one service. plm deletes only its own temp copy (`plm/staging.py::release_local`) and its own `upload()` stages. Cleanup of the link's object is the file bus's job: `delete_on_download`, the TTL, or `drop()` |
| the fetched copy lands in `<db dir>/urlcache` and is unlinked as soon as the revision owns its bytes; the janitor sweeps strays older than 2 h | a failed write must not leak disk |

## The problem

An agent with a 40 MB STL had one way in: base64 it into a `checkin()` tool
call. That is ~54 MB of context for one file, it fails well before real CAD
sizes, and it makes plm a byte-proxy for data it has no reason to touch.

## The shape: plm mints, the client PUTs to plm, plm gets a handle

```
agent ->  upload(name, size, sha256)                  MCP tool / POST /api/uploads
      <-  {put_url, put_path, handle, expires_at, instructions}
agent ->  curl -f -T model.stl -H "Authorization: Bearer $PLM_TOKEN" "<put_url>"
                                                      PUT /api/uploads/<token>
plm   ->  hash while streaming -> declared numbers must match -> row records them
agent ->  checkin(files=[{name, role, handle, sha256, size}])
plm   ->  compare with the row -> adopt into the revision -> delete the inbox dir
```

The PUT is on the machine plane like every `/api/` write: a bearer token under
`PLM_AUTH_MODE=required`, nothing extra under `trusted-network`. The identity
middleware does not read the body (`identity.RAW_BODY_PUTS`): it is file bytes,
never a JSON object with an `actor` in it. The PUT route never blocks the
event loop: opening, writing (about a megabyte per hop) and the row update run
in the threadpool.

| Thing | Value |
|---|---|
| Handle | `staging/<token>/<name>` |
| `put_url` | `<PLM_PUBLIC_URL>/api/uploads/<token>`; `put_path` is the same without the origin, for a client that reaches plm at another base URL; valid while the staging row lives; **written once** — a second PUT is a 409 (`drop_upload`, then `upload` again) |
| Bytes | `CACHE_DIR/inbox/<token>/<name>`, streamed to disk, capped at `PLM_RAW_UPLOAD_MAX` (a lying `Content-Length` is caught while streaming) |
| Staging TTL | `PLM_STAGING_TTL_SECONDS` (24 h; 7 d cap), swept by `plm/gc.py` |
| Inline cap, MCP | `PLM_MAX_INLINE_BYTES` = 1 MiB |
| Inline cap, HTTP | `PLM_MAX_INLINE_BYTES_HTTP` = 16 MiB |
| Inline cap, text ext | `PLM_MAX_INLINE_TEXT_BYTES` = 8 MiB (whichever is larger) |

`upload(url=…)` is the same handle from a link: plm fetches it straight into
the inbox (sha256 verified while streaming) and returns the handle.

## Inline is for small text, and only that

`text` and `content_b64` over the cap are refused with a 413 that spells out the
three steps above. The MCP cap is the strict one (a tool call is a context
window); HTTP is looser so the web UI and existing multi-MB viz posts keep
working, and text extensions get their own generous cap because an inline
`INSTRUCTIONS.md` is exactly what inline is *for*.

## Verification: a claim is not a fact

The bytes are hashed ONCE, by the `Sink` that writes them. When a PUT
finishes, a sha256/size declared at mint must equal what arrived — otherwise
a 400 printing both values, and nothing is kept — and the arrived numbers go
on the `staging` row with `received_at`. At check-in (and in `upload_status`)
the caller's numbers are compared against the row and the file's existence is
checked; nothing is re-read. Nothing uploaded yet is a 409 that says "run the
curl". The store then ADOPTS the file by those numbers (`lfs.adopt`, a hard
link). A handle is only honored while a `staging` row vouches for it —
otherwise "read this handle" would be an arbitrary-read primitive over plm's
inbox.

## Where the bytes end up

Handles go through the **normal** storage split (`storage.choose_backend`), so
nothing about this flow changes where files live:

- a staged `.md`/`.scad` (vault-pinned) is read and committed to the git
  vault (or the tree, in a `write` project), exactly as an inline one would be;
- a staged STL over `PLM_BLOBSTORE_MIN_SIZE` offloads through
  `storage.put_file` — the ONE write path for bytes that arrived by reference,
  shared with the `url` form — into plm's LFS store (`PLM_BLOBSTORE_DEFAULT=lfs`).

The inbox directory is deleted only after the revision owns its copy. A failed
check-in leaves the stage alone so it can be retried, and the janitor collects
whatever is never claimed.

## What a write RETIRES: supersession

*2026-08-12. Confirmed on KML-PRT-1012 v0.4.1: thirteen file rows spanning three
version generations on one revision.*

Revise carries every file **row** forward, and that is right — a new DRAFT starts
life as a copy of what it was seeded from (`docs/PLM_DATA_MODEL.md`). The bug was
the other half of the contract. `upload_files` retired a prior row **by
basename**, and this vault's exported filenames carry the version they came from
(`KML-PRT-1012_v0.3.0_bottom-insert-thin-carrier.stl`), so a new generation's STL
never had the same name as the STL it replaced. Nothing was individually wrong;
together the manifest became unreadable, and the viewer could pick a
two-generations-old body.

So every file write now ends by asking one question: **did this revision gain a
file of its own that plainly stands in for something it inherited?**
`ops.supersede_inherited` (called by `checkin` and `upload_files` — one helper,
so this is a property of writing files rather than of one endpoint) is the
answer, and it is deliberately narrow:

| Retired | Because |
|---|---|
| inherited row with the **same role + extension** as an own-folder file | version-stamped names give geometry, renders and sources no other stable identity |
| inherited row whose **filename exists in this revision's own version folder** | `MANIFEST.sha256` / `VERSION.md` — names that never change, refreshed in the folder by `vault.write_checkin` without a row of their own |

Only rows whose bytes provably live in an **older** version folder of the same
item are candidates — a vault path `<folder>/versions/v<ver>/…` or an offloaded
key `<number>/v<ver>/<name>`. Three guarantees follow, and they are the point:

- a row whose generation **cannot be proven** from its path/key is never touched,
  so imported and hand-placed files are safe by construction;
- **same-generation siblings are never touched.** Two STLs on one revision are
  variants — an exploded view beside an assembled one — and both are real;
- **no bytes are deleted.** A vault copy stays in git history, an offloaded
  object keeps its immutable key. A `files` row is a POINTER, and this retires
  pointers.

Every retirement is evented as **`file.superseded`**, carrying what went, which
generation it came from, and what replaced it; check-in and working-upload
responses return the same list as `superseded`.

The rule is coarse in one known way: two documents of the same role and
extension across generations (a `NOTES.md` arriving while an inherited
`INSTRUCTIONS.md` is still current) read as a replacement. That is the trade for
having any rule at all against version-stamped names — and `remove_file` plus a
re-upload is the repair.

### Backfill

`ops.backfill_supersede(conn, apply=False)` applies the same helper to existing
history, so the sweep cannot diverge from the write path. Run it dry first; it
reports per item and per revision. Prod, 2026-08-12: **39 rows retired across 13
revisions of 9 items (611 → 572 rows)**, recorded as one `files.backfill` event.
KML-PRT-1012 v0.4.1 went 13 → 5 (its own STL, viz STL, PNG, source and
INSTRUCTIONS.md), v0.4.0 9 → 4.

## Taking a file off a revision: `remove_file`

The manual counterpart, for what a rule cannot judge (`ops.remove_file` →
`DELETE /api/files/{id}`, MCP `remove_file`). Like supersession it removes a
claim, not content.

- **DRAFT** — any row. A working revision's manifest is still being decided.
- **Frozen** (IN_REVIEW and everything past it) — **only rows carried in from an
  older version folder.** A released record does not lose its own files; the
  refusal says so and names the two ways forward (Revise ↑ and remove it on the
  new DRAFT, or upload the replacement and let supersession retire it).
- A row whose generation cannot be proven counts as the revision's own, so
  ambiguity is refused rather than resolved in favour of deleting something.

Evented as **`file.remove`**, with a written note in the event data saying what
was removed, whether it was inherited, and where the bytes still are.

`GET /api/files/{id}` returns one file's record (never its bytes) — name, role,
size, owning revision, `from_version` / `inherited`, and the backend-aware blob
URL. The web UI's file previews are a routed state, so a reload can rebuild the
header from the id alone.

## Surfaces

| MCP tool | HTTP | |
|---|---|---|
| *(filedrop MCP)* `upload(name, ttl_days, delete_on_download)` | — | the universal path: stage once, hand the link to any service |
| `upload(name, size, sha256)` | `POST /api/uploads` | plm's own mint: a put_url + handle plm will clean up |
| *(curl)* | `PUT /api/uploads/{token}` | the bytes, as the raw body, with your bearer token |
| `upload(url=, sha256=)` | — | a link fetched into the inbox now, as a handle |
| `upload_status(handle)` | `GET /api/uploads/status?handle=` | did it land, and the sha256/size measured when it did |
| `list_uploads()` | `GET /api/uploads` | what is pending |
| `drop_upload(handle)` | `DELETE /api/uploads?handle=` | abandon early |
| — | `POST /api/uploads/gc` | sweep now |
| `checkin(files=[…])` | `POST /api/items/{n}/checkin` | accepts `handle` |
| `put_file(…, handle=)` | `POST /api/items/{n}/files` | accepts `handle` |
| `put_viz(…, handle=)` | `POST /api/items/{n}/viz` | accepts `handle` |
| `file_link(number, name)` | `GET /api/files/{id}/link` | the `/api/blob?id=` URL (private-network-only; `expiring_url` is always null) |
| — | `GET /api/files/{id}` | one file's record (no bytes) |
| `remove_file(number, name \| file_id)` | `DELETE /api/files/{id}` | take a row off a revision's manifest |

Check-in and working-upload responses carry each file's `/api/blob?id=` URL.
plm links are private-network-only; to hand a file to somebody off the private network, stage
it with the filedrop MCP and share its `get_url`.

## Code

| File | Role |
|---|---|
| `plm/staging.py` | mint / open_inbox / claim / consume / GC, `token_dir` (token → directory, shared with snippets), `Sink` (the one hash+cap+write loop for arriving bytes), `fetch_url` (the url form), the inline guard, and the one wire→ops normalizer shared by the API and MCP |
| `plm/api.py` | `PUT /api/uploads/{token}` and `PUT /api/items/{n}/files/{name}` — stream the body into a `Sink` off the event loop |
| `plm/ops.py` | `_split_destinations` / `_fill_vault_bytes` / `_consume_inputs` — handles, links and inline bytes route through the same check-in |
| `plm/ops.py` | `file_generation` / `supersede_inherited` / `remove_file` / `backfill_supersede` — what a write RETIRES, and the manual repair |
| `plm/storage.py` | `put_file` — an arrival with measured numbers is adopted into the LFS store (`lfs.adopt`); without them it is streamed and hashed |
| `plm/snippets.py` | `create_from_url` — share_3d from a link |
| `plm/gc.py` | the janitor loop, now shared by snippets and staged uploads |
| `plm/db.py` | the `staging` table |
