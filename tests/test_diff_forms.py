"""The diff FORMS registry (PLM_REVIEW_MODEL §2.9): web/diffwidgets.js's node
self-check, the registry's forms held equal to roles.MEDIA_KINDS `diff`, and the
compare facts the forms read (plm.toml's flat field delta, same-content).
"""
import json
import subprocess
from pathlib import Path

from plm import gitrepo, roles

from test_web_routes import NODE, needs_node

ROOT = Path(__file__).resolve().parents[1]


@needs_node
def test_diffwidgets_self_check():
    r = subprocess.run([NODE, str(ROOT / "web" / "diffwidgets.test.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
    assert "checks ok" in r.stdout


@needs_node
def test_registry_forms_equal_media_kinds():
    """ONE list of forms per kind: the client registry implements them,
    roles.MEDIA_KINDS `diff` names them for agents — they must agree."""
    r = subprocess.run([NODE, str(ROOT / "web" / "diffwidgets.test.js"), "--kinds"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    assert set(js) == set(roles.MEDIA_KINDS), set(js) ^ set(roles.MEDIA_KINDS)
    for kind, spec in roles.MEDIA_KINDS.items():
        assert js[kind] == list(spec["diff"]), kind


# -- the compare facts the diff forms read (§2.9) ------------------------------

def test_toml_field_delta():
    a = {"version": "0.2.0", "bom": [{"path": "parts/X", "qty": 2}],
         "files": {"doc": ["a.md"]}, "attributes": {"mass_g": 3}}
    b = {"version": "0.2.1", "bom": [{"path": "parts/X", "qty": 4}],
         "files": {"doc": ["a.md", "b.md"]}, "summary": "s"}
    got = {d["key"]: (d["op"], d["from"], d["to"]) for d in gitrepo.toml_field_delta(a, b)}
    assert got == {"version": ("changed", "0.2.0", "0.2.1"),
                   "bom[0].qty": ("changed", 2, 4),
                   "files.doc": ("changed", ["a.md"], ["a.md", "b.md"]),
                   "attributes.mass_g": ("removed", 3, None),
                   "summary": ("added", None, "s")}


def test_same_content():
    sc = gitrepo._same_content
    x = {"blob": "1" * 40, "size": 131, "lfs": {"oid": "f" * 64, "size": 900}}
    assert sc(None, x, x) is True
    assert sc(None, x, {**x, "blob": "2" * 40}) is True            # same oid
    assert sc(None, x, {"blob": "3" * 40, "size": 131,
                        "lfs": {"oid": "e" * 64, "size": 900}}) is False
    assert sc(None, {"blob": "4" * 40, "size": 10}, {"blob": "5" * 40, "size": 11}) is False
    assert sc(None, {"blob": "4" * 40, "size": 899}, x) is False   # sizes differ
    assert sc(None, None, x) is None
