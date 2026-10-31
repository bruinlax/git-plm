#!/usr/bin/env bash
# plm-git demo — the whole point, in one script.
#
#   ./demo/demo.sh [BASE_URL]
#
# Proves five things, in order:
#   1. `git clone` of a part number works, over vanilla smart HTTP.
#   2. The clone carries the part's REAL plm history (check-in messages).
#   3. The extracted content is byte-identical to the vault (same tree object).
#   4. A stale cache is detected and regenerated on the next clone/fetch.
#   5. An unknown part number is a clean 404, not a hang or a 500.
#
# Read-only against the vault and against plm. Creates and deletes clones under
# a scratch dir; makes no commit anywhere except inside the generated repos.
set -uo pipefail

BASE="${1:-http://127.0.0.1:8099}"
VAULT="${PLMGIT_VAULT:-/vault}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/plm-git-demo.XXXXXX")"
PART=KML-PRT-1009       # 16 commits, and its folder was renamed twice in history
PART2=KML-PRT-1012      # a second part, changed most recently
trap 'rm -rf "$WORK"' EXIT

h() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() { printf '\033[2m$ %s\033[0m\n' "$*"; eval "$@"; }

h "0. the service"
run "curl -sS $BASE/healthz"

h "1. git clone a part  (no plm client, no plugin — just git)"
run "git clone $BASE/git/$PART.git $WORK/$PART"

h "2. its REAL plm history came with it"
run "git -C $WORK/$PART log --oneline"
echo
echo "-- one commit in full: note the trailers back to the monorepo --"
run "git -C $WORK/$PART log -1 --format='%H%n%an <%ae>  %ad%n%n%B'"

h "3. the files are the part, rooted at the repo root"
run "git -C $WORK/$PART ls-tree --name-only HEAD"
run "git -C $WORK/$PART ls-tree -r --name-only HEAD | sed -n '1,8p'"
echo "   ($(git -C "$WORK/$PART" ls-tree -r --name-only HEAD | wc -l) files, \
$(du -sh "$WORK/$PART" | cut -f1) on disk)"

h "3b. byte-identical to the vault: same git tree object"
FOLDER=$(curl -sS "$BASE/api/parts/$PART" | python3 -c 'import json,sys;print(json.load(sys.stdin)["folder"])')
echo "   plm says folder = $FOLDER"
A=$(git -C "$WORK/$PART" rev-parse 'HEAD^{tree}')
B=$(git -C "$VAULT" rev-parse "HEAD:$FOLDER")
echo "   extracted root tree : $A"
echo "   vault folder tree   : $B"
[ "$A" = "$B" ] && echo "   IDENTICAL — the extraction reuses the vault's own objects" \
                || echo "   MISMATCH (the vault may have moved since the last refresh)"

h "3c. the git ecosystem, for free"
run "git -C $WORK/$PART log --oneline -- versions/v0.7.1 versions/v0.7.0"
run "git -C $WORK/$PART diff --stat HEAD~3 HEAD"

h "4a. the vault moves forward -> the part repo follows  (real, end to end)"
echo "   Making a junk revision in prod plm just to demo this would be vandalism,"
echo "   so instead we run the extractor against a THROWAWAY cache whose mirror"
echo "   has been rewound to an earlier vault commit. That is the same situation"
echo "   as 'the cache was built, then someone checked in': the mirror catches"
echo "   up on the next fetch and the staleness key moves."
OLDCACHE="$WORK/oldcache"
REALMIRROR="${PLMGIT_CACHE:-~/.cache/plm-git}/vault-mirror.git"
mkdir -p "$OLDCACHE"
git clone --mirror -q "$REALMIRROR" "$OLDCACHE/vault-mirror.git"
git -C "$OLDCACHE/vault-mirror.git" remote set-url origin "$VAULT"
# Rewind to just before the newest vault commit that touched THIS part, so the
# rewound mirror is genuinely missing one of its revisions.
TIP=$(curl -sS "$BASE/api/parts/$PART2" | python3 -c 'import json,sys;print(json.load(sys.stdin)["vault_folder_head"])')
BEHIND=$(git -C "$VAULT" rev-parse "$TIP^")
echo "   $PART2's newest vault commit is ${TIP:0:10}; rewinding the throwaway"
echo "   mirror to its parent ${BEHIND:0:10}"
run "git -C $OLDCACHE/vault-mirror.git update-ref refs/heads/master $BEHIND"
echo "   -- build $PART2 from the rewound mirror (no vault fetch) --"
run "PLMGIT_CACHE=$OLDCACHE python3 -m plm_git.extract $PART2 --no-refresh"
BEFORE_N=$(git -C "$OLDCACHE/repos/$PART2.git" rev-list --count HEAD)
echo "   commits: $BEFORE_N   tip: $(git -C "$OLDCACHE/repos/$PART2.git" log -1 --format='%h %s')"
echo "   -- now let it fetch the vault: mirror catches up, cache is stale, rebuild --"
run "PLMGIT_CACHE=$OLDCACHE python3 -m plm_git.extract $PART2"
AFTER_N=$(git -C "$OLDCACHE/repos/$PART2.git" rev-list --count HEAD)
echo "   commits: $AFTER_N   tip: $(git -C "$OLDCACHE/repos/$PART2.git" log -1 --format='%h %s')"
echo "   -> $BEFORE_N commits became $AFTER_N; nobody ran a build step in between."
echo "   -- and it is idempotent: a third run does nothing --"
run "PLMGIT_CACHE=$OLDCACHE python3 -m plm_git.extract $PART2"

h "4b. the server-side trigger: staleness is checked on ref advertisement"
STAMP="${PLMGIT_CACHE:-~/.cache/plm-git}/repos/$PART2.git/plm-git.json"
run "git clone -q $BASE/git/$PART2.git $WORK/$PART2-before && git -C $WORK/$PART2-before rev-parse --short HEAD"
if [ -w "$STAMP" ]; then
  CUR=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["folder_head"])' "$STAMP")
  PREV=$(git -C "$VAULT" rev-parse "$CUR^")
  echo "   rewinding the cache's staleness key ${CUR:0:10} -> ${PREV:0:10}"
  python3 - "$STAMP" "$PREV" <<'PY'
import json,sys
p,prev=sys.argv[1],sys.argv[2]
d=json.load(open(p)); d["folder_head"]=prev; d["vault_head"]="0"*40
json.dump(d,open(p,"w"),indent=2)
PY
  run "curl -sS $BASE/api/parts/$PART2 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(\"stale:\",d[\"stale\"],\"cached:\",d[\"cached_folder_head\"][:10],\"vault:\",d[\"vault_folder_head\"][:10])'"
  echo "   -- next clone rebuilds it, unprompted (watch the service log) --"
  run "git clone -q $BASE/git/$PART2.git $WORK/$PART2-after && git -C $WORK/$PART2-after rev-parse --short HEAD"
  run "curl -sS $BASE/api/parts/$PART2 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(\"stale now:\",d[\"stale\"],\" rebuilt at\",d[\"stamp\"][\"generated_at\"])'"
else
  echo "   (cache not writable from here — skipping; run on the cache host)"
fi

h "5. a second part, and an unknown number"
run "git clone -q $BASE/git/KML-PRT-1002.git $WORK/KML-PRT-1002 && git -C $WORK/KML-PRT-1002 log --oneline"
run "curl -sS -o /dev/null -w 'GET /api/parts/KML-PRT-9999 -> %{http_code}\n' $BASE/api/parts/KML-PRT-9999"
run "git clone $BASE/git/KML-PRT-9999.git $WORK/nope 2>&1 | tail -3"

h "6. push is refused on THIS instance (PLMGIT_ALLOW_PUSH=0)"
run "GIT_TERMINAL_PROMPT=0 git -C $WORK/$PART push origin HEAD:refs/heads/main 2>&1 | tail -6"
echo "   (the write path IS built — see demo/push-demo.sh, which runs it against"
echo "    the sandbox. It is off here, and pushing to main is refused even there.)"

h "done"
echo "scratch dir $WORK is being removed; the vault and plm were only read."
