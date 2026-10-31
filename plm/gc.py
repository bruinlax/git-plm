"""The janitor loop, in one place.

plm now has two kinds of bytes that expire by themselves — disposable 3D
snippets (plm/snippets.py) and staged uploads waiting to be checked in
(plm/staging.py) — and both want the same thread: sweep at startup so a restart
is also a sweep, then sweep on an interval, and never die on an exception
because a janitor that dies is worse than no janitor.

That loop was written once for snippets; this module is it, extracted, so the
next expiring thing costs one line instead of a second copy of the idiom.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

EVERY = float(os.environ.get("PLM_GC_SECONDS", 6 * 3600))


def start(name: str, sweep: Callable[[], dict], every: float = EVERY) -> None:
    """Run `sweep` now and every `every` seconds, in a daemon thread."""
    def loop() -> None:
        while True:
            try:
                res = sweep()
                if res.get("deleted") or res.get("errors"):
                    print(f"[{name}] gc: {res}", flush=True)
            except Exception as exc:  # noqa: BLE001 — a janitor never dies
                print(f"[{name}] gc failed: {exc}", flush=True)
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name=f"{name}-janitor").start()


def _sweep_idempotency() -> dict:
    """Expired replay records (plm/idem.py). A retry window, not history — the
    audit trail is the events table, which is never pruned."""
    from . import db, idem
    with db.connect() as conn:
        return {"deleted": idem.sweep(conn)}


def start_all() -> None:
    """Every self-expiring thing plm owns. Called once, from API startup.

    All three sweeps always run: snippets and staged uploads live on plm's own
    disk (CACHE_DIR), and each instance sweeps only its own.
    """
    from . import snippets, staging
    start("snippets", snippets.janitor_once, snippets.JANITOR_EVERY)
    start("uploads", staging.janitor_once)
    start("idempotency", _sweep_idempotency)
