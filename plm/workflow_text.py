"""Standing instructions that contradict the project's write loop (stage 19).

Handoff finding 5 (Codex, CHG-379): the migrated `INSTRUCTIONS.md` of every
camper-ipad-dock item said "Before changes, get_item + what_changed and
checkout; iterate on this DRAFT. After frozen release, revise first." — the
MCP write loop. The project is `write`: the loop there is branch → patch bump
→ commit → push into a CHANGE → a human approves. The agent had to guess which
instruction won. Three readers use this module, one rule:

  * the `instructions-current` CHECK (plm/checks.py) — advisory, on every
    commit of a `write` project: the lines, and the replacement paragraph;
  * project READINESS (plm/readiness.py) — how many items ship stale text;
  * the migration CLEANUP rule `instructions` (plm/migrate_cleanup.py) —
    rewrites the GENERATED block (the migration skill's two lines) on an open
    DRAFT through `ops.upload_files`, the one write path.

Only the legacy WRITE-LOOP verbs are flagged — `checkout(`, `revise(`,
`put_file`, `checkin(`, "iterate on this DRAFT", "revise first", "PLM checkout".
"Do not revise KML-FW-4001 until …" is a design rule, not a loop, and is left
alone; so is every `git checkout`.
"""
from __future__ import annotations

import re

#: file names that carry standing instructions (get_item's `instructions`)
NAMES = ("INSTRUCTIONS.md", "instructions.md", "work_instructions.md")

#: (pattern, why) — each a legacy MCP write-loop instruction
_LEGACY = [
    (re.compile(r"(?<!git )\bcheckout\s*\(", re.I), "checkout() — the advisory lock of the MCP loop"),
    (re.compile(r"\brevise\s*\(", re.I), "revise() — a new DRAFT through the MCP"),
    (re.compile(r"\bcheckin\s*\(", re.I), "checkin() — the deprecated MCP check-in"),
    (re.compile(r"\bput_file\b", re.I), "put_file — iterating on a DRAFT through the MCP"),
    (re.compile(r"\biterate on (?:this|the) draft\b", re.I), "iterate on the same DRAFT — a pushed draft is never rewritten here"),
    (re.compile(r"\brevise first\b", re.I), "revise first — a git-first change is a branch + version bump"),
    (re.compile(r"\bplm checkout\b", re.I), "PLM checkout — there is no lock in the git loop"),
    (re.compile(r"(?<!git )\b(?:and|then|,|;)\s*checkout\b(?!\s*(?:-|v\d|<|origin|[0-9a-f]{7}))", re.I),
     "checkout as a step — the MCP lock, not `git checkout`"),
]

#: the paragraph that replaces them — what GIT-FIRST.md says, in five lines
REPLACEMENT = (
    "Git-first write project: follow the workspace GIT-FIRST.md. A design "
    "change is clone into work/<NUMBER> -> branch -> bump the patch version "
    "in plm.toml -> commit -> push with -o pr=<n> -> submit the "
    "pull request -> a human approves. Never rewrite a pushed draft, never merge or "
    "release as an agent, and do not use the old checkout / revise / put_file "
    "loop.")

#: the block the migration skill generated (two lines, as Codex wrote them on
#: 2026-10-05 for camper-ipad-dock) — what the cleanup rule rewrites verbatim
GENERATED = re.compile(
    r"^Before changes, get_item \+ what_changed and checkout; iterate on this "
    r"DRAFT\.\s*\n?After frozen release, revise first\.", re.M)


#: a line that FORBIDS the old loop ("do not use the old checkout / revise /
#: put_file loop" — REPLACEMENT itself) is the current instruction, not a stale one
_PROHIBITS = re.compile(r"\b(?:do not|don't|never|not)\b[^.]*\b(?:checkout|revise|"
                        r"put_file|checkin)\b", re.I)


def stale_lines(text: str) -> list[dict]:
    """[{line, text, why}] — every line that tells an agent to use the MCP
    write loop. Empty when the text is current."""
    out = []
    for i, line in enumerate((text or "").splitlines(), 1):
        if _PROHIBITS.search(line):
            continue
        whys = [why for rx, why in _LEGACY if rx.search(line)]
        if whys:
            out.append({"line": i, "text": line.strip()[:200],
                        "why": "; ".join(dict.fromkeys(whys))})
    return out


def rewrite(text: str) -> tuple[str, bool]:
    """(new text, changed) — the GENERATED block replaced by REPLACEMENT, the
    rest of the file kept byte for byte. Hand-written stale lines are not
    rewritten (a person wrote them; the check says so and a person fixes
    them)."""
    if not GENERATED.search(text or ""):
        return text, False
    new = GENERATED.sub(lambda _m: REPLACEMENT, text, count=1)
    # the generated block ended mid-sentence; its tail ("Preserve source …")
    # stays a sentence of its own
    return new, new != text


def verdict(text: str, *, mode: str) -> dict | None:
    """The check's answer for one INSTRUCTIONS.md, or None when current (or
    the project is not `write` — the MCP loop IS the loop there)."""
    if mode != "write":
        return None
    lines = stale_lines(text)
    if not lines:
        return None
    new, gen = rewrite(text)
    return {"lines": lines, "replacement": REPLACEMENT, "generated": gen,
            "rewritten": new if gen else ""}
