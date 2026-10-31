"""Item number -> vault folder.

plm owns identity. A number is not a path: the folder is
`projects/<project>/<class-dir>/<NUMBER>_<slug>/` today, but plm renames it when
the slug changes and MOVES it when an item is renumbered (plm/vault.py
move_folder, plm/ops.py renumber_item). So we never build the path from the
number — we ask plm for `items.folder`, and we take `former_numbers` with it
because those old numbers are the anchors for the pre-rename history.

Two resolvers, in order:
  1. GET {PLM_URL}/api/items/{number}   — the supported surface.
  2. plm's SQLite index, opened mode=ro  — for when the API is down.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config


class NotFound(Exception):
    pass


@dataclass
class Item:
    number: str
    name: str
    folder: str
    former_numbers: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def anchors(self) -> list[str]:
        """Every number this item has ever had, newest first. Used to find the
        item's folder in historical commits where it had a different name."""
        seen, out = set(), []
        for n in [self.number, *self.former_numbers]:
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out


def _from_api(number: str) -> Item | None:
    url = f"{config.PLM_URL}/api/items/{urllib.request.quote(number)}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(number) from None
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None  # API down -> try the DB
    if not data.get("folder"):
        raise NotFound(f"{number} has no vault folder (nothing checked in yet)")
    return Item(number=data["number"], name=data.get("name", ""),
                folder=data["folder"].strip("/"),
                former_numbers=list(data.get("former_numbers") or []),
                source="plm-api")


def _from_db(number: str) -> Item:
    if not config.PLM_DB.exists():
        raise NotFound(f"cannot resolve {number}: plm API unreachable and no DB "
                       f"at {config.PLM_DB}")
    uri = f"file:{config.PLM_DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT number,name,folder,former_numbers FROM items WHERE number=?",
            (number,)).fetchone()
        if row is None:  # a number the item USED to have still resolves
            row = conn.execute(
                "SELECT number,name,folder,former_numbers FROM items "
                "WHERE former_numbers!='' AND (','||former_numbers||',') "
                "LIKE ('%,'||?||',%')", (number,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise NotFound(number)
    if not row["folder"]:
        raise NotFound(f"{number} has no vault folder (nothing checked in yet)")
    return Item(number=row["number"], name=row["name"] or "",
                folder=row["folder"].strip("/"),
                former_numbers=[n for n in (row["former_numbers"] or "").split(",") if n],
                source="plm-db-ro")


def resolve(number: str) -> Item:
    item = _from_api(number)
    return item if item is not None else _from_db(number)
