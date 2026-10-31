# Deploying plm

How to run plm from a clone of this repo: a single `docker compose up`, then
the options for putting it behind a hostname, turning on sign-in, and backing
it up.

---

## Run it anywhere

Nothing is needed before this: no account, no hostname, no other service, no
file to edit. Docker (with the compose plugin) and git. Ten minutes, most of it
the first image build (Ubuntu + OpenSCAD).

### 1. Clone and start

```sh
git clone https://github.com/bruinlax/git-plm.git && cd git-plm
docker compose up -d --build
```

The first `up` runs a one-shot `init` service (`plm/firstrun.py`, root for
that step only) that creates `./data/` (all of plm's state, gitignored) owned
by the uid the services run as, and two secrets in `./data/secrets/`:

| file | what |
|---|---|
| `push-tokens` | `<token>=<username>` lines — the ONE credential store for the API, the MCP and `git push`. The first line is yours: user `admin`, seeded on first start as a human **approver**. |
| `index-token` | a service-to-service secret (the git door asks plm to index a push). |

It never overwrites an existing secret and never prints one (`docker compose
logs init` says where it is). Add a person or an agent by appending a line
(`<random>=<name>`; tokens are re-read within 30 s) and creating their user
(**More users**, below).

**Optional `.env`** (`cp sample.env .env`; every line is commented and
optional): the admin's name (`PLM_ADMIN`, before the first `up`), ports, a
hostname, auth, and on Linux `PLM_UID`/`PLM_GID` when `id -u` is not 1000, so
the files under `./data` are yours. `PLM_COMMIT=$(git rev-parse --short HEAD)
docker compose up -d --build` labels the image with the commit;
`COMPOSE_PROJECT_NAME` names the stack, e.g. to run two side by side.

Three services come up, all from one image (the repo-root `Dockerfile`):

| service | default address | what |
|---|---|---|
| `plm` | http://localhost:8097 | web UI + HTTP API |
| `plm-mcp` | http://localhost:8098/mcp | MCP server for agents |
| `plm-git` | http://localhost:8092/git/`<N>`.git | clone a part; `git push` = a proposal |

Ports bind to `127.0.0.1` by default. Change them with `PLM_PORT`,
`PLM_MCP_PORT`, `PLM_GIT_PORT` in `.env` (the links plm prints follow), and
`PLM_BIND=0.0.0.0` to expose them — read **Auth modes** first.

```sh
docker compose ps                        # plm turns (healthy) in ~15 s
curl -s http://localhost:8097/api/stats  # {"items":0, ...}
```

Open **http://localhost:8097**.

### 2. The sample project, or your first item

`scripts/load-example.sh` loads `examples/camper-ipad-dock` — a real project
with two releases and an open pull request — into the running instance
(examples/README.md). Approve pull request #380 as the admin to see a merge:
the tag written, `main` moved, the assembly that pins the part listed under
impact. The loader briefly stops running services during import and verification,
then restarts them. Or start from nothing.

Your token, for the commands below (it stays out of your shell history):

```sh
export PLM_TOKEN=$(grep -v '^#' data/secrets/push-tokens | head -1 | cut -d= -f1)
```

plm owns numbering, so an item is created by plm — from the UI, the MCP
(`create_item`), or the API:

```sh
curl -s -X POST http://localhost:8097/api/items \
  -H "Authorization: Bearer $PLM_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Demo bracket","kind":"part","project":"demo","prefix":"DEMO"}'
# -> {"number":"DEMO-PRT-0001", "latest_version":"0.1.0", "latest_state":"DRAFT", ...}
```

Every item IS a git repo, with a `v0.1.0` draft on branch `draft/v0.1.0`.

### 3. Clone it, add geometry, push a proposal

Meshes travel as **Git LFS** objects, so install git-lfs once
(`brew install git-lfs`, `apt install git-lfs`, …). Then:

```sh
git clone http://localhost:8092/git/DEMO-PRT-0001.git && cd DEMO-PRT-0001
git lfs install --local
$EDITOR bracket.scad                      # your OpenSCAD source
openscad -o bracket.stl bracket.scad      # or with the plm image, below
cat >> plm.toml <<'TOML'

[source]
file = "bracket.scad"

[files]
primary = "bracket.stl"
source = "bracket.scad"
artifact = "bracket.stl"
TOML
git add -A && git commit -m "Demo bracket: first geometry"
git -c "http.extraHeader=Authorization: Bearer $PLM_TOKEN" push origin HEAD:refs/for/main
```

The push output is the interface: it says what plm indexed, runs the checks,
opens **pull request #1** and prints its URL. Open it in the UI and approve:
approval merges it, tags `v0.1.0` and moves `main`. (`git push` means
*proposed*; a human approving is the merge.)

No OpenSCAD or git-lfs on the host? The plm image has both. On Linux:

```sh
plmimg() { docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp \
             -v "$PWD":/w -w /w plm-selfhost:latest "$@"; }
plmimg openscad -o bracket.stl bracket.scad
plmimg git lfs install --local
plmimg git -c user.name="You" -c user.email=you@example.com add -A
plmimg git -c user.name="You" -c user.email=you@example.com commit -m "Demo bracket: first geometry"
plmimg git -c "http.extraHeader=Authorization: Bearer $PLM_TOKEN" push origin HEAD:refs/for/main
```

Check what plm recorded:

```sh
curl -s http://localhost:8097/api/items/DEMO-PRT-0001   # revisions[-1].summary = your commit subject
```

### 4. Agents: the MCP server

Any MCP client that speaks streamable HTTP:

```json
{
  "mcpServers": {
    "plm": {
      "type": "http",
      "url": "http://localhost:8098/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

Give each agent its own line in `push-tokens` (and its own user): every write
is recorded as the token's actor. The server's instructions teach the agent
the loop above. `PLM_ISSUES_ADVICE` in `.env` is the one line agents are told
about reporting a bug or a gap (default: "file an issue with the operator").

### 5. Auth modes

`PLM_AUTH_MODE` is one switch:

| mode | meaning | use it when |
|---|---|---|
| `trusted-network` (default) | The network is the boundary. Reads and writes need no login; a bearer token says WHO. ⚠️ Anyone who can reach the ports can write and approve. | Ports on `127.0.0.1`, or a private network/VPN where everybody is trusted. |
| `required` | Every write needs a credential: a bearer token, or an OIDC session for humans in the browser. | Anything other people can reach. |

For browser sign-in under `required`, register plm with any OIDC provider
(Authelia, Keycloak, Authentik, Google, …; the `groups` claim must be in
scope) and set in `.env`:

```sh
PLM_AUTH_MODE=required
OIDC_ISSUER=https://id.example.com
OIDC_CLIENT_ID=plm
OIDC_REDIRECT_URI=https://plm.example.com/auth/callback
OIDC_ACCESS_GROUP=plm-users,admins
OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc-client-secret   # file: data/secrets/oidc-client-secret
PLM_PUBLIC_URL=https://plm.example.com
PLM_GIT_CLONE_BASE=https://plm.example.com/git            # if your proxy serves /git there
```

Behind a reverse proxy, route `/` to :8097, `/mcp` to :8098 and `/git/` to
:8092, and set `PLM_PUBLIC_URL` / `PLM_GIT_CLONE_BASE` to what users type —
every link plm prints is built on them. `GET /auth/whoami` says what plm sees.

### More users

A token names a user plm already knows; it never creates one. The admin is
seeded (`PLM_ADMIN`). For anybody else, add their token line and their user:

```sh
docker compose exec plm python3 - <<'PY'
import time; from plm import db
with db.connect() as c:
    c.execute("INSERT OR IGNORE INTO users (username,kind,roles,display,created_at) "
              "VALUES (?,?,?,?,?)", ("alice", "human", "editor", "Alice", time.time()))
PY
```

`kind` is `human` or `bot` (bots never approve); `roles` is a csv of
`editor` and `approver`. OIDC users are created at their first sign-in.

### 6. Backups (bring your own)

Everything is in `./data`. `data/cache` is derived and can be skipped.

- **Simplest:** `docker compose stop && tar czf plm-$(date +%F).tgz data && docker compose start`.
- **Live:** the DB is SQLite in WAL mode — copy it with
  `sqlite3 data/db/plm.sqlite3 ".backup plm.sqlite3"` (never a plain `cp` of a
  live file), then copy `data/repos`, `data/lfs`, `data/evidence` and
  `data/secrets` as files. `data/lfs` is content-addressed and append-only.

Restore = put `./data` back and `docker compose up -d`. Keep `./data` on a
local disk: SQLite on a network share corrupts.

### 7. Upgrade

```sh
git pull && docker compose up -d --build
```

Schema migrations run on start. Take a backup first.

### Troubleshooting

- `Permission denied` under `./data`: the containers run as `PLM_UID:PLM_GID`
  (1000:1000 unless `.env` says otherwise), and the `init` service hands them
  the folders it creates. `ls -ln data` must show that uid; a folder you made
  yourself as another user is yours to `chown`.
- You cannot read `data/secrets/push-tokens`: it belongs to that uid, mode
  600. Read it through the container (`docker compose exec -T plm cat
  /run/secrets/push-tokens`), or set `PLM_UID`/`PLM_GID` in `.env` before the
  first `up`.
- A push refused with a "mesh as a plain blob" message: git-lfs was not active
  in that clone — `git lfs install --local`, then the fix the message prints.
- Links point at the wrong host: set `PLM_PUBLIC_URL` and `PLM_GIT_CLONE_BASE`.
- `docker compose logs -f plm plm-git plm-mcp`.
