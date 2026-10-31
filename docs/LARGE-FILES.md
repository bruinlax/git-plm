# Large files — Git LFS in front of plm's object store

*Stage 17 (2026-10-05). docs/PLM_GIT_FIRST.md §6 / Phase 4, built.*

The POC's most surprising punt was "clone a part, get no mesh": migration left
every big mesh as a `[[files.external]]` row pointing at filedrop, and a push
over the size cap was refused with "LFS is Phase 4". Git LFS is the standard
answer, and plm only has to speak its batch API. That is what this is:

```
git push (git-lfs)                                       plm-git :8092
  pre-push: POST /git/N.git/info/lfs/objects/batch ──>  plm-git/plm_git/lfs.py
            PUT  /git/N.git/info/lfs/objects/<oid> ──>  hashed while it streams
                                                        -> plm/lfs.py store
  then the refs ──> pre-receive (plm/gitvalidate.py) asks the store:
                    every pointer's object present, at its size? policy kept?
                ──> post-receive -> plm indexes: files row backend `lfs`
git clone (git-lfs): smudge -> GET /git/N.git/info/lfs/objects/<oid>
GIT_LFS_SKIP_SMUDGE=1 git clone: text only, meshes stay 130-byte pointers
```

## Decisions, and why

**0. The LFS store is plm's ONE object store.** New non-vault bytes go here
(`PLM_BLOBSTORE_DEFAULT=lfs`, `storage.put*` → a `backend='lfs'` row; an arrival
whose sha256 the upload already measured is adopted by hard link, not copied
or re-hashed — `lfs.adopt`). `split` reads an `lfs` row from this store by its
oid. A row on a retired backend (`storage.RETIRED_BACKENDS`: `filedrop`,
`local`) is re-pointed to its object here, at the tree path the split gave it
(`migrate_cleanup --apply --only filedrop-repoint`); `resolve()` refuses one
that is left with that command, and a pushed `[[files.external]]` may declare
only `vault`.

**1. The bytes live in plm's own content-addressed store.** The stock compose
mounts `./data/lfs` at `/var/plm/lfs` and sets `PLM_LFS_ROOT` to that path.
Back up this directory together with the repositories and database (DEPLOY.md).
The layout is
`objects/<aa>/<bb>/<sha256>` — git-lfs's own local layout. An object is written
to `tmp/`, hashed while it streams, and renamed into place only if it hashes to
the promised oid at the promised size; the store cannot hold a lie, so the
receive hook only has to ask "is it there, at that size?". Same bytes in two
items = one object (dedupe is free).

* *Not the filedrop inbox* — its objects expire (TTL ≤ 7 days,
  `delete_on_download`); a release tag pointing at an expired object rots.
* *Not an external object store* — the rule is that design data is
  SERVICE data, protected by the service's own sidecar. Local disk + the
  sidecar's `DATA_TREES` is exactly that rule (decision 6).

**2. The batch API is served by the git service, beside smart HTTP.**
`plm-git/plm_git/lfs.py`: `POST <repo>/info/lfs/objects/batch` (download +
upload, `basic` transfer, `hash_algo: sha256`), `GET`/`PUT
objects/<oid>`, `POST verify`, `POST locks/verify` (answers "no locks").
git-lfs derives the endpoint from the clone URL — no client configuration.
The hrefs point back at the host the client used (`Host:` header), so a
token never travels to a second host.

* **Same door as a push.** Reads (batch download, GET) are anonymous, like
  clone. An `upload` batch, a PUT and `verify` run `push.identify` — the
  bearer (or Basic password) token must map to a plm actor. The upload href
  carries the caller's own `Authorization` header back to it; nothing is minted
  that could outlive the token.
* Uploads are capped (`PLM_LFS_MAX_OBJECT_BYTES`, 2 GiB) and refused with 422
  when the bytes do not hash to the URL's oid.
* Keep-alive: every route consumes or closes its request body (found by
  demo-lfs: an unread `locks/verify` body turned git-lfs's next request into
  method `{"ref":…}POST`).

**3. Policy — which files are LFS.** `plm/lfs.py::wants_lfs`:

| file | LFS? | why |
|---|---|---|
| meshes (`.stl .3mf .obj .ply .off .amf .step .stp .f3d .glb`, either case) | **always, any size** | `.gitattributes` cannot say "above 1 MiB": a pattern is a pattern. A mesh that is a pointer in one commit and a blob in the next makes `git status` lie in an LFS clone (the clean filter would turn the blob into a pointer). So the brief's "mesh kinds above the threshold" became "meshes, period" — the only rule a `.gitattributes` can keep true |
| sources, manifests, docs (`plm.toml`, `README.md`, `.scad .py .md .toml .json .csv .svg .dxf …`) | **never** | what `git diff` and review are for; a pointer to a source is unreadable without the store. A pointer in one of these paths is REFUSED |
| anything else (images, firmware `.elf`, PDFs, archives) | above `PLM_LFS_THRESHOLD_BYTES` (default **1 MiB**) | small renders stay plain so README images and diffs keep working; big binaries do not ride every clone forever. Each such file gets its OWN `.gitattributes` line (a `*.png` pattern would catch the small ones too) |

plm writes the `.gitattributes` (`lfs.gitattributes()` — one home):
`create_item`'s template, every commit `split --lfs` makes, and plm's own
commits (`gitwrite._lfs_changes`: a file the policy names is stored and
committed as its pointer, and missing lines are appended, never rewritten).
Meshes keep `merge=binary` (§5.3) instead of git-lfs's default `merge=lfs`.

**4. The push door** (`gitvalidate._check_lfs`, in quarantine):

* REFUSED — a pointer whose object is not in the store, or at another size:
  `git lfs push --object-id origin <oid>…` to paste.
* REFUSED — a NEW plain blob the policy says is LFS above the threshold (a 5 MB
  mesh from a clone without git-lfs). The one fix, in the clone:
  `git lfs install --local && git lfs track '*.stl'`, `git rm --cached` +
  `git add` the file, `git add .gitattributes && git commit --amend --no-edit`.
  This replaces "LFS is Phase 4".
* REFUSED — a pointer standing in for a source / manifest / doc.
* LINT — a new SMALL mesh that `.gitattributes` tracks arrived as a plain blob
  (the pusher has no git-lfs): accepted, with "install git-lfs, `git add
  --renormalize .`".
* GRANDFATHERED — a blob already in a parent commit's tree is never refused:
  the live POC's generated repos carry plain meshes, and a push that does not
  touch them must not be blamed for them.
* `PLMGIT_PUSH_MAX_FILE_BYTES` (192 MiB) still caps a plain blob; its refusal
  now names the LFS fix.

**5. Everything that reads a file resolves pointers.** One chokepoint each:

| reader | how |
|---|---|
| indexer (`gitindex._index_files`) | a pointer becomes a `files` row `backend='lfs'`, `key`=`sha256`=oid, the object's size, media sniffed from the object |
| `storage.resolve` / `local_copy` | backend `lfs` = the object file (`local_copy` symlinks it into `objcache` with the extension, so OpenSCAD's `import()` and the thumbnailer get a suffix); a `git` row that is a pointer serves its object |
| `/api/repo/{n}/blob`, `/preview`, `/tree`, compare sizes (`gitrepo.blob_info`/`blob`/`blob_stream`/`tree`/`_tree_sizes`) | the pointer is read as the object; tree rows carry `lfs: {oid, present}` (the UI shows an `lfs` chip); a missing object is a 404 naming the `git lfs push` line |
| `/api/blob?id=` (UI viewer, diff, explode, MCP) | through `storage` above — the web client changed by one chip |
| checks (`Ctx.blob`; the render's extracted tree) | `geometry-facts`, `render-matches-source` and every mesh reader measure the object; `git archive` writes pointers, so the render tree is smudged (`lfs.smudge_dir`) before OpenSCAD runs |
| release evidence (`release.evidence_document`) | an LFS file records the OBJECT's sha256 and size plus `lfs: true` — "the bytes that were reviewed are the bytes that get printed". Absent on every other file, so evidence frozen before LFS hashes the same |
| `get_item` (MCP) | `git.lfs = {needs_git_lfs, files, bytes, say}` at `default_ref` |

**6. Backup: the store is backed up as files, beside the repos as git.**
The following describes an optional backup integration; the stock compose
does not include a backup sidecar. Follow DEPLOY.md for standalone backups. The
sidecar's `DATA_TREES` gains `data/lfs` (content-addressed, immutable — a
file-level copy is exact, which is the case `DATA_TREES` exists for). The
`GIT_TREES` bundles carry the pointers; the store carries the bytes; the drill
proves the join: `lfs-fsck` (`python -m plm.lfs --fsck`: every restored object
hashes to its name) and `lfs-pointers-resolve` (`python -m plm.gitkeep --lfs`:
every pointer blob in every restored repo — reachable or not — names a restored
object at its size). Bundles that embedded LFS objects would have duplicated
every mesh into every repo that ever referenced it; keeping them as files is
what dedupe across items is worth.

**7. Migration: `split --lfs` is a recorded layout, like `--normalize-names`.**
The layout marker (`.plm-split.json`) gains `lfs` and `lfs_threshold` (the
threshold decides which non-mesh files are pointers, so it is part of every
SHA). A root recorded before stage 17 has no `lfs` key and is NOT LFS — which
is what keeps the live POC's repos, and `gitsync`'s re-splits of them, byte for
byte as they were. Switching a root is `--lfs --force-layout --prune --all`.
With `--lfs`:

* every in-tree file the policy names becomes a pointer (bytes into the store);
  an `lfs` row is read from the store by its oid;
* every commit carries the `.gitattributes` template.

## Git LFS clients

The server implements the LFS batch API itself. Clients need git-lfs to clone
mesh bytes and upload new meshes. The repo-root Dockerfile includes a pinned
git-lfs binary; see [DEPLOY.md](../DEPLOY.md#3-clone-it-add-geometry-push-a-proposal)
for the container commands. Run `git lfs install --local` inside each client
clone before adding mesh files.

Some agent instructions refer to an optional workspace wrapper at
`tools/git/git.sh`. That wrapper is not distributed in this repository; use
the documented container commands or your own git-lfs client instead.

A clone made with `GIT_LFS_SKIP_SMUDGE=1` contains small text pointers in place
of meshes. Source diffs still work. Use `git lfs pull` with a configured client
to download the mesh bytes.

Do not enable a system-wide smudge filter in the server container: its bare
repositories have no download remote, and plm resolves pointers itself when
rendering checks.

## Older plain meshes in a live repo stay plain — decided, not a bug (stage 18)

A repo that held plain meshes before LFS (a live repo plm or a person wrote
before stage 17, e.g. CID-ASM-0001's two `.fbx` scans and two 2 MB PNGs from
2026-10-05) keeps them as plain blobs when it later receives an LFS file:
`gitwrite` and a client's `git lfs track` APPEND `.gitattributes` lines and
never rewrite history, and the push door grandfathers a blob already in a
parent tree. **That is the intended state**: rewriting a pushed history to
convert them would change every SHA a parent, a claim or a release pins. The
cost is small and stated: in an LFS clone, a grandfathered file the new
`.gitattributes` matches shows as modified once it is touched (`git lfs fsck
--pointers` lists them). To convert one deliberately, forward-only (a NEW
commit, nothing rewritten), in a git-lfs-enabled clone: `git lfs track
'<file>'` if no `.gitattributes` line matches it yet, then `git rm
--cached <file> && git add <file> .gitattributes && git
commit -m "Store <file> in LFS"`. Generated
repos do not have the case once a root is split with `--lfs` (§7; the live POC
since stage 18).

## Not built (stated, not hidden)

* **LFS locks.** `locks/verify` answers "no locks" (so git-lfs's pre-push does
  not warn on every push); creating a lock is refused with a pointer to plm's
  checkout. §6 / Phase 4's "the LFS lock API replaces `checkouts`" is the next
  step; it needs a DB table (the git service does not open the DB — the lock
  routes would forward to plm, like `/api/git/index`).
* **Garbage collection of the store.** Nothing deletes an object. An unused
  object costs disk only; a delete needs the same "nothing pins it" scan as
  keep-refs (§ stage 11b) across every repo, reachable or not.
* **`catalog.git`** carries TOML only; no LFS there.
* **Submodule pointers:** `git clone --recurse-submodules` with git-lfs smudges
  each child from its own `../<CHILD>.git/info/lfs` (relative URLs resolve);
  verified on a rehearsal clone of an assembly, not on every nesting.
