"""The UI's URL table (web/routes.js) — run its node self-check, and hold the
server's copy (plm/webroutes.py) to the same answers.

node is optional on a host; without it these skip and say so. On the host it
lives under ~/.nvm, which a non-login shell may not have on PATH.
"""
import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from plm import webroutes

ROOT = Path(__file__).resolve().parents[1]


def _node():
    n = shutil.which("node")
    if n:
        return n
    hits = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    return hits[-1] if hits else None


NODE = _node()
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed on this host")


@needs_node
def test_routes_js_self_check():
    r = subprocess.run([NODE, str(ROOT / "web" / "routes.test.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
    assert "checks ok" in r.stdout


PATHS = ["/", "/CID-ASM-0001", "/CID-ASM-0001/compare/a...b", "/LFD-PRT-PRT-0001",
         "/pulls/380", "/pulls/380/files", "/pulls", "/CID-ASM-0001/pulls",
         "/changes/CHG-380", "/change-requests/CHG-1", "/CID-ASM-0001/pull/3/checks", "/release-sets/1", "/registry", "/approvals", "/search",
         "/projects/x", "/snip/t", "/item/CID-ASM-0001", "/registr", "/api/stats",
         "/auth/login", "/mcp", "/git/X.git", "/healthz", "/metrics", "/app.js",
         "/cid-asm-0001", "/CID-ASM", "/x"]
SPLITS = [["draft/v0.4.1/cad", ["main", "draft/v0.4.1"]],
          ["v0.1.0/cad/x.scad", ["v0.1.0"]],
          ["feature/x/y/z", ["feature/x/y", "feature/x"]],
          ["draft%2Fv0.4.1/a%20b", ["draft/v0.4.1"]],
          ["draft/v9/cad", []], ["abc123/x", ["main"]], ["", ["main"]]]


@needs_node
def test_server_and_ui_agree():
    script = (
        "const R=require(process.argv[1]);const d=JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify({ui:d.paths.map(p=>R.isAppPath(p)),"
        "split:d.splits.map(([r,n])=>{const o=R.splitRef(r,n);return [o.ref,o.path]})}))")
    r = subprocess.run([NODE, "-e", script, str(ROOT / "web" / "routes.js"),
                        json.dumps({"paths": PATHS, "splits": SPLITS})],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    assert js["ui"] == [webroutes.is_ui_path(p) for p in PATHS]
    assert js["split"] == [list(webroutes.split_ref(a, b)) for a, b in SPLITS]
    # and the link builders spell things the same way
    script2 = ("const R=require(process.argv[1]);console.log(JSON.stringify(["
               "R.P.tree('CID-ASM-0001','draft/v0.4.1','cad/a b (1).scad'),"
               "R.P.compare('CID-ASM-0001','v0.1.0','draft/v0.2.0'),"
               "R.P.pull('CID-ASM-0001',17),R.P.release('CID-ASM-0001',4,'v0.2.0'),"
               "R.P.change(380),R.P.releaseSet(12),R.P.project('inky frame'),R.P.snip('t_1'),"
               "R.P.change(380,'files'),R.P.pull('CID-ASM-0001',17,'checks'),R.CHANGE_TABS]))")
    r = subprocess.run([NODE, "-e", script2, str(ROOT / "web" / "routes.js")],
                       capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout) == [
        webroutes.tree("CID-ASM-0001", "draft/v0.4.1", "cad/a b (1).scad"),
        webroutes.compare("CID-ASM-0001", "v0.1.0", "draft/v0.2.0"),
        webroutes.pull("CID-ASM-0001", 17), webroutes.release("CID-ASM-0001", 4, "v0.2.0"),
        webroutes.change(380), webroutes.release_set(12), webroutes.project("inky frame"),
        webroutes.snip("t_1"), webroutes.change(380, "files"),
        webroutes.pull("CID-ASM-0001", 17, "checks"), list(webroutes.CHANGE_TABS)]
