#!/usr/bin/env python3
"""Anonymize a Teams attendance export so it can be committed as a test fixture.

Replaces every person's name, email and UPN with a deterministic pseudonym while
preserving the exact structure the parser has to cope with: UTF-16 LE encoding, CRLF
line endings, tab separation, the four sections, varying column counts per section,
honorific and "(External)" decorations, email domain variety, and the capitalization
quirks Teams emits.

Usage:
    python anonymize_teams_export.py INPUT.csv OUTPUT.csv [--map mapping.json]

The mapping is written out only if --map is given. Keep it OUT of the repo: it is the
re-identification key. It exists so you can trace a fixture row back to a real person
while debugging, nothing more.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path


def fold(s: str) -> str:
    """Strip accents. Display names keep theirs; email local parts never have them."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )

# Fictional names. Deliberately plausible for a Montreal teaching hospital so the fixture
# exercises the same accent and hyphenation handling as the real thing.
FIRST_NAMES = [
    "Camille", "Mathieu", "Sophie", "Olivier", "Geneviève", "Thomas", "Noémie",
    "Vincent", "Élise", "Samuel", "Anaïs", "Étienne", "Juliette", "Félix",
    "Rosalie", "Gabriel", "Maude", "Xavier", "Léonie", "Antoine", "Chloé",
    "Nicolas", "Amélie", "Benoît", "Clara", "Raphaël", "Delphine", "Simon",
]

LAST_NAMES = [
    "Beaulieu", "Tremblay", "Gagnon", "Thibault", "Lavoie", "Fortin", "Bergeron",
    "Côté", "Pelletier", "Mercier", "Caron", "Dubois", "Lévesque", "Poulin",
    "Nadeau", "Ouellet", "Desrosiers", "Charbonneau", "Giroux", "Lemieux",
    "Rivard", "Bouchard", "Hébert", "Paquette", "Sauvé", "Marchand", "Dufour",
]

# Trailing honorifics Teams appends after a comma.
HONORIFIC = r"(?:,\s*(?:Dr|Dre|MD|MDCM|RN|PhD|Ph\.D|PharmD)\.?)"
NAME_RE = re.compile(
    rf"^(?P<base>.*?)(?P<honorific>{HONORIFIC})?(?P<parens>(?:\s*\([^)]*\))*)$"
)

# Which column holds the name / email / UPN, per section heading.
SECTION_COLUMNS = {
    "2. Participants": {"name": 0, "email": 4, "upn": 5},
    "3. In-Meeting Activities": {"name": 0, "email": 4},
    "4. Meeting Engagement": {"name": 0},
}


class Anonymizer:
    def __init__(self) -> None:
        self.by_base: dict[str, str] = {}
        self.by_email: dict[str, str] = {}
        self._n = 0

    def _next_identity(self) -> tuple[str, str]:
        i = self._n
        self._n += 1
        first = FIRST_NAMES[i % len(FIRST_NAMES)]
        last = LAST_NAMES[(i * 7 + 3) % len(LAST_NAMES)]
        return first, last

    def base_name(self, base: str) -> str:
        """Map a bare name, preserving a middle initial if there was one."""
        key = base.strip().casefold()
        if key in self.by_base:
            return self.by_base[key]
        first, last = self._next_identity()
        had_middle_initial = bool(re.match(r"^\S+\s+\S\.?\s+\S", base.strip()))
        new = f"{first} {last[0]} {last}" if had_middle_initial else f"{first} {last}"
        self.by_base[key] = new
        return new

    def display_name(self, value: str) -> str:
        """Map a full display name, keeping honorifics and parentheticals intact."""
        value = value.strip()
        if not value:
            return value
        m = NAME_RE.match(value)
        base = m.group("base") or value
        new_base = self.base_name(base)
        return new_base + (m.group("honorific") or "") + (m.group("parens") or "")

    def email(self, value: str, display: str = "") -> str:
        """Map an address, preserving domain, segment count and capitalization."""
        value = value.strip()
        if not value or "@" not in value:
            return value
        key = value.casefold()
        if key in self.by_email:
            return self.by_email[key]

        local, domain = value.rsplit("@", 1)
        m = NAME_RE.match(display.strip()) if display else None
        new_name = self.base_name(m.group("base")) if m else self.base_name(local)
        parts = fold(new_name).replace(".", "").split()
        first = parts[0].casefold()
        last = parts[-1].casefold()

        # Keep the number of dot-separated segments the original had. Longer local parts
        # are real — provincial health addresses often carry a second surname and a
        # role suffix — so fill them with plausible tokens, not placeholders.
        segments = len(local.split("."))
        extras = ["bernier", "med", "ext", "md"]
        if segments <= 1:
            new_local = f"{first}{last}"
        elif segments == 2:
            new_local = f"{first}.{last}"
        else:
            filler = extras[: segments - 2]
            new_local = ".".join([first, *filler, last])

        # Teams sometimes capitalizes the local part. Mirror that.
        if local[:1].isupper():
            new_local = ".".join(p.capitalize() for p in new_local.split("."))

        new = f"{new_local}@{domain}"
        self.by_email[key] = new
        return new

    def mapping(self) -> dict:
        return {"names": self.by_base, "emails": self.by_email}


def anonymize(text: str, anon: Anonymizer) -> str:
    lines = text.split("\r\n")
    section: str | None = None
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped in SECTION_COLUMNS:
            section = stripped
            out.append(line)
            continue
        if stripped.startswith(("1. ", "2. ", "3. ", "4. ", "5. ")):
            section = stripped if stripped in SECTION_COLUMNS else None
            out.append(line)
            continue
        if not stripped or section is None:
            out.append(line)
            continue

        cols = line.split("\t")
        idx = SECTION_COLUMNS[section]

        # Header rows start with the literal column name.
        if cols[0].strip() == "Name":
            out.append(line)
            continue

        name_i = idx["name"]
        original_display = cols[name_i] if name_i < len(cols) else ""
        if name_i < len(cols):
            cols[name_i] = anon.display_name(cols[name_i])

        for field in ("email", "upn"):
            i = idx.get(field)
            if i is not None and i < len(cols):
                cols[i] = anon.email(cols[i], original_display)

        out.append("\t".join(cols))

    return "\r\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--map", help="write the re-identification map here (keep out of git)")
    args = ap.parse_args()

    raw = Path(args.input).read_bytes()
    text = raw.decode("utf-16")

    anon = Anonymizer()
    result = anonymize(text, anon)

    # utf-16 encoding emits the BOM, matching what Teams produces.
    Path(args.output).write_bytes(result.encode("utf-16"))

    if args.map:
        Path(args.map).write_text(
            json.dumps(anon.mapping(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print(f"{len(anon.by_base)} people, {len(anon.by_email)} addresses -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
