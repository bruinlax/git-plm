"""Test environment for plm: an OIDC-configured instance on a throwaway DB.

plm reads its configuration from the environment at IMPORT time (plm/config.py,
plm/identity.py), so this sets it before anything under `plm` is imported. The
client secret is a fixed test string; nothing here talks to a real provider —
the token and userinfo calls are faked per test.

Run (venv OFF the synced volume):
    export UV_PROJECT_ENVIRONMENT=~/.cache/git-plm-venv
    uv run --with pytest pytest tests -q
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="plm-tests-"))
for k, v in {
    "HOME": str(_TMP),
    "PLM_DB": str(_TMP / "plm.sqlite3"),
    "PLM_DATA_ROOT": str(_TMP / "vault"),
    "PLM_REPOS": str(_TMP / "repos"),
    "PLM_CACHE_DIR": str(_TMP / "cache"),
    "PLM_PUBLIC_URL": "http://testserver",
    "PLM_ADMIN": "geoff",
    "PLM_ACTIONS_DRY_RUN": "1",
    "PLM_GIT_MODE": "off",
    "PLM_AUTH_MODE": "trusted-network",
    "OIDC_ISSUER": "http://idp.invalid",
    "OIDC_CLIENT_ID": "plm-test",
    "OIDC_CLIENT_SECRET": "test-secret-not-real",
    "OIDC_REDIRECT_URI": "http://testserver/auth/callback",
    "OIDC_ACCESS_GROUP": "plm-users",
}.items():
    os.environ[k] = v
(_TMP / "vault").mkdir(exist_ok=True)
