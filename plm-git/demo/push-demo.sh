#!/usr/bin/env bash
# plm-git PUSH demo — "git push" == "propose a revision", end to end.
#
#   PLM_TOKEN=<a token from PLMGIT_PUSH_TOKENS_FILE> ./demo/push-demo.sh [BASE_URL] [NUMBER]
#
# Runs against the SANDBOX instance (plm-git-sandbox, :8199 -> plm-sandbox :8093)
# and REFUSES TO RUN against anything else, because it creates real DRAFT
# revisions and real approval requests in whatever plm it is pointed at.
#
# Shows, in order:
#   1. clone a part, create the next version folder, commit
#   2. `git push origin main`            -> the teaching refusal
#   3. `git push HEAD:refs/for/main`     -> plm revises, uploads, submits
#   4. the DRAFT in plm's API, its file rows and their storage backends
#   5. the approval sitting in the queue, and the provenance context entry
#   6. `git fetch` + `git diff` against the advertised DRAFT ref (the PR view)
#   7. the refusal catalogue: duplicate version, malformed tree, path escape,
#      no credential, and a bot trying to merge
#
# It does NOT approve anything. Approval is a human decision in plm, and that
# asymmetry is the point.
#
# Re-running it: the proposal from the previous run has to be DECIDED first
# (approved, rejected or withdrawn in plm), because plm allows one open DRAFT per
# item — the script will hit exactly that refusal otherwise, and say so.
set -uo pipefail

BASE="${1:-http://127.0.0.1:8199}"
PART="${2:-KML-PRT-1013}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/plm-git-push-demo.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

h() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() { printf '\033[2m$ %s\033[0m\n' "$*"; eval "$@"; }
py() { python3 -c "$1"; }

# ---- guard rails ----------------------------------------------------------
STATUS="$(curl -sS "$BASE/healthz")" || { echo "no plm-git at $BASE" >&2; exit 1; }
SANDBOX=$(printf '%s' "$STATUS" | py 'import json,sys;print(json.load(sys.stdin)["push"]["target_is_sandbox"])')
WRITES=$(printf '%s' "$STATUS" | py 'import json,sys;print(json.load(sys.stdin)["push"]["writes_to"])')
ENABLED=$(printf '%s' "$STATUS" | py 'import json,sys;print(json.load(sys.stdin)["push"]["enabled"])')
if [ "$ENABLED" != "True" ]; then
  printf '%s\n' "$STATUS" | py 'import json,sys;print("push is off here:",json.load(sys.stdin)["push"]["refused_because"])'
  exit 1
fi
if [ "$SANDBOX" != "True" ]; then
  cat >&2 <<EOF
REFUSING TO RUN: $BASE writes to $WRITES, which is not a sandbox.
This script creates DRAFT revisions and approval requests. Point it at
plm-git-sandbox (:8199) instead.
EOF
  exit 1
fi
: "${PLM_TOKEN:?set PLM_TOKEN to a token from PLMGIT_PUSH_TOKENS_FILE}"
GIT_PUSH=(git -c "http.extraHeader=Authorization: Bearer $PLM_TOKEN")

h "0. the service, and where its proposals go"
run "curl -sS $BASE/healthz | python3 -m json.tool"

# ---- 1. clone and prepare the proposal ------------------------------------
h "1. clone the part and create the NEXT version folder"
run "git clone -q $BASE/git/$PART.git $WORK/$PART"
cd "$WORK/$PART"
LATEST=$(curl -sS "${WRITES/plm-sandbox:8097/127.0.0.1:8093}/api/items/$PART" 2>/dev/null \
         | py 'import json,sys;print(json.load(sys.stdin)["latest_version"])' 2>/dev/null) || LATEST=""
if [ -z "$LATEST" ]; then
  LATEST=$(git ls-tree -d --name-only HEAD versions/ >/dev/null 2>&1; \
           git ls-tree -r --name-only HEAD | sed -n 's|^versions/v\([0-9.]*\)/.*|\1|p' | sort -V | tail -1)
fi
NEXT=$(py "a,b,c='$LATEST'.split('.');print(f'{a}.{b}.{int(c)+1}')")
run "ls versions/"
echo "   plm's latest is v$LATEST, so this proposal is v$NEXT"
cp -r "versions/v$LATEST" "versions/v$NEXT"
rm -f "versions/v$NEXT/MANIFEST.sha256"     # plm computes the manifest; it is a receipt
printf '\n- v%s: bearing seat relieved 0.1 mm so the pulley presses on by hand.\n' \
  "$NEXT" >> "versions/v$NEXT/VERSION.md"
git add -A -f
git -c user.name="push demo" -c user.email=demo@example.com commit -q \
  -m "Relieve the pulley bearing seat 0.1 mm for a hand-press fit" \
  -m "The v$LATEST seat needed an arbor press and marked two hubs. 0.1 mm of relief keeps the interference but lets it go on by hand."
run "git log --oneline -1"

# ---- 2. the natural mistake ----------------------------------------------
h "2. push to main the way anyone would — and be taught why not"
run "\"\${GIT_PUSH[@]}\" push origin main 2>&1 | sed 's/[[:space:]]*\$//'"

# ---- 3. the proposal ------------------------------------------------------
h "3. push it as a PROPOSAL"
run "\"\${GIT_PUSH[@]}\" push origin HEAD:refs/for/main 2>&1 | sed 's/[[:space:]]*\$//'"

# ---- 4/5. what plm now holds ---------------------------------------------
# A tiny reader rather than inline python: three levels of shell quoting around
# an f-string is how demo scripts start lying about their own output.
PLM_HTTP="${WRITES/plm-sandbox:8097/127.0.0.1:8093}"
cat > "$WORK/show.py" <<'PYEOF'
import json, sys, urllib.request

what, base, number = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3]


def get(path):
    with urllib.request.urlopen(f"{base}{path}", timeout=20) as r:
        return json.load(r)


if what == "revision":
    d = get(f"/api/items/{number}")
    r = d["revisions"][0]
    print(f"  {d['number']} v{r['version']}  {r['state']}  author={r['author']}")
    print(f"  summary: {r['summary']}")
    print(f"  vault commit: {r['git_commit'][:10]}")
    print("  files (role / storage backend / size / path):")
    for f in d["files"][str(r["id"])]:
        print(f"    {f['role']:<9} {f['backend'] or 'vault':<9} "
              f"{f['size']:>10,} B  {f['path']}")
elif what == "approval":
    for a in get("/api/approvals?pending=true"):
        if a["number"] == number:
            print(f"  approval #{a['id']}  {a['number']} v{a['version']} -> "
                  f"{a['target_state']}  requested_by={a['requested_by']}  "
                  f"decision={a['decision'] or 'PENDING'}")
            print(f"  note: {a['note']}")
elif what == "context":
    print(get(f"/api/items/{number}/context")[0]["body"])
PYEOF

h "4. the DRAFT in plm, and where each file's bytes actually live"
run "python3 $WORK/show.py revision $PLM_HTTP $PART"

h "5. the approval waiting for a human, and the provenance entry"
run "python3 $WORK/show.py approval $PLM_HTTP $PART"
run "python3 $WORK/show.py context $PLM_HTTP $PART"

# ---- 6. the pull-request view -------------------------------------------
h "6. the PR view: fetch the advertised DRAFT ref and diff it"
BASE_SHA=$(git rev-parse origin/main)
run "git fetch -q origin '+refs/plm/*:refs/plm/*' && git for-each-ref --format='%(refname)' refs/plm"
run "git diff -M -C --find-copies-harder --stat $BASE_SHA..refs/plm/drafts/$PART/v$NEXT"
echo "   (the -M -C --find-copies-harder flags matter: a revision is a NEW version"
echo "    folder, so without them git reports adds instead of the change)"

# ---- 7. the refusals ----------------------------------------------------
h "7a. a proposal built on a stale main — what a race looks like"
# The push in step 3 moved the mirror's main. This working tree still predates
# it, which is exactly the position the LOSER of two concurrent proposals is in.
run "\"\${GIT_PUSH[@]}\" push origin HEAD:refs/for/main 2>&1 | sed -n '1,16p'"

git fetch -q origin
git checkout -q -B fresh origin/main

h "7b. a version plm already has — versions are immutable"
GONE=$(python3 - "$PLM_HTTP" "$PART" <<'PYEOF'
import json, subprocess, sys, urllib.request
base, number = sys.argv[1].rstrip("/"), sys.argv[2]
with urllib.request.urlopen(f"{base}/api/items/{number}", timeout=20) as r:
    have = [x["version"] for x in json.load(r)["revisions"]]
tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", "origin/main"],
                      capture_output=True, text=True).stdout
in_tree = {l.split("/")[1][1:] for l in tree.splitlines()
           if l.startswith("versions/v")}
print(next((v for v in sorted(have) if v not in in_tree), ""))
PYEOF
)
if [ -n "$GONE" ]; then
  cp -r "versions/v$LATEST" "versions/v$GONE" && rm -f "versions/v$GONE/MANIFEST.sha256"
  git add -A -f && git -c user.name=demo -c user.email=demo@example.com commit -q \
    -m "Backfill the v$GONE dataset that never reached the vault"
  run "\"\${GIT_PUSH[@]}\" push origin HEAD:refs/for/main 2>&1 | sed -n '1,16p'"
  git checkout -q -B fresh2 origin/main
else
  echo "   (skipped: every version plm knows about is already in this tree)"
fi

h "7c. a tree that is not a part folder"
git checkout -q -B junk origin/main
mkdir -p notes && echo 'loose notes' > notes/scratch.txt
git add -A -f && git -c user.name=demo -c user.email=demo@example.com commit -q \
  -m "Drop some loose notes into the part repository root"
run "\"\${GIT_PUSH[@]}\" push origin HEAD:refs/for/main 2>&1 | sed -n '1,16p'"

h "7d. a path escape"
git checkout -q -B esc origin/main
NEXT2=$(python3 -c "a,b,c='$NEXT'.split('.');print(f'{a}.{b}.{int(c)+1}')")
cp -r "versions/v$NEXT" "versions/v$NEXT2" 2>/dev/null || cp -r "versions/v$LATEST" "versions/v$NEXT2"
rm -f "versions/v$NEXT2/MANIFEST.sha256"
ln -s ../../../../../etc/passwd "versions/v$NEXT2/leaked.txt"
git add -A -f && git -c user.name=demo -c user.email=demo@example.com commit -q \
  -m "Reference the shared profile instead of copying the file in"
run "\"\${GIT_PUSH[@]}\" push origin HEAD:refs/for/main 2>&1 | sed -n '1,12p'"

h "7e. no credential (reads are anonymous; writes are not)"
run "GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true git push origin HEAD:refs/for/main 2>&1 | tail -3"
echo "   the full message, as curl sees it:"
run "curl -sS '$BASE/git/$PART.git/info/refs?service=git-receive-pack'"

h "7f. a bot trying to merge"
run "\"\${GIT_PUSH[@]}\" push --force origin HEAD:refs/plm/drafts/$PART/v$NEXT 2>&1 | sed -n '1,12p'"

h "done — and nothing was released"
cat <<EOF
The proposal is IN_REVIEW in $PLM_HTTP. A human approves it, and THAT is the
merge:

  curl -X POST $PLM_HTTP/api/approvals/<id>/decide \\
       -H 'Content-Type: application/json' \\
       -d '{"decision":"approved","actor":"geoff","note":"..."}'

A bot calling that gets "agent is not an approver" — 403, from plm, which owns
the gate.
EOF
