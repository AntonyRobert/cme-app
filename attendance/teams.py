"""
Reading a Microsoft Teams attendance export.

The file is not a CSV, whatever its extension says. What it actually is,
from a real export (attendance/tests/fixtures/teams-export-2026-09-10.csv):

- UTF-16 LE with a BOM, CRLF line endings, TAB-separated.
- Four sections, each headed "N. Title" and separated by blank lines:
    1. Summary            key<TAB>value lines (title, start, end, ...)
    2. Participants       one row per person, 15 columns
    3. In-Meeting Activities  one row per JOIN, 6 columns
    4. Meeting Engagement one row per reaction/unmute, 3 columns
- No meeting ID anywhere. An upload is matched to an event on title + date.
- Dates like "9/10/26, 8:57:52 AM": day/month order is ambiguous and the
  file carries nothing that settles it. The parser never guesses: the
  caller supplies the event's known date and the parser accepts whichever
  reading matches it, or fails.
- Names contain commas ("Camille Thibault, Dr") and suffixes like
  "(External)" or "(CUSM)". Kept verbatim; never split on commas.
- Durations are human strings with varying parts: "3h 1m 41s", "3h 26s",
  "36m 6s".
- Section 4 has quoted fields with doubled quotes inside them (Sent reaction
  followed by the reaction name in doubled quotes).
- The Role column is a Teams meeting permission. Everyone is given
  "Presenter" so they can share a screen. It means nothing for credit.

Section 3 is the observation we keep: one AttendanceRecord per row.
Section 2's In-Meeting Duration is already the sum of that person's
Section 3 rows; it is used only as a checksum.

Parsing is pure: bytes in, ParsedExport out, nothing written. Importing
is attendance.services.import_export.
"""
import csv
import datetime
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from django.conf import settings

PARSER_VERSION = "teams-2026.1"

SUMMARY = "Summary"
PARTICIPANTS = "Participants"
ACTIVITIES = "In-Meeting Activities"
ENGAGEMENT = "Meeting Engagement"

SECTION_HEADING = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")
DURATION = re.compile(r"^\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*$")
TIMESTAMP = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*$"
)
NAME_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")

# The orders a "a/b/c" date may be in. Never chosen by locale, only by
# agreement with a date we already know.
MONTH_FIRST = "M/D/Y"
DAY_FIRST = "D/M/Y"


class ExportError(Exception):
    """The file can't be used. The message is safe to show to staff."""


@dataclass(frozen=True)
class RawTimestamp:
    """A timestamp as written, before we know which number is the month."""

    text: str
    a: int
    b: int
    year: int
    hour: int
    minute: int
    second: int

    def date_as(self, order):
        month, day = (self.a, self.b) if order == MONTH_FIRST else (self.b, self.a)
        return datetime.date(self.year, month, day)

    def resolve(self, order, zone):
        try:
            day = self.date_as(order)
        except ValueError:
            raise ExportError(f"{self.text!r} is not a valid date when read as {order}.")
        local = datetime.datetime.combine(day, datetime.time(self.hour, self.minute, self.second))
        return local.replace(tzinfo=zone).astimezone(datetime.timezone.utc)


@dataclass(frozen=True)
class ParticipantRow:
    """Section 2: one line per person, with Teams' own total."""

    display_name: str
    email: str
    upn: str
    role: str
    in_meeting_seconds: int


@dataclass(frozen=True)
class ActivityRow:
    """Section 3: one line per join. What becomes an AttendanceRecord."""

    display_name: str
    join: RawTimestamp
    leave: RawTimestamp
    duration_seconds: int
    email: str
    role: str
    line_number: int


@dataclass
class ParsedExport:
    title: str
    start: RawTimestamp
    end: RawTimestamp
    attended_participants: int | None
    participants: list = field(default_factory=list)
    activities: list = field(default_factory=list)
    engagement_rows: int = 0
    warnings: list = field(default_factory=list)


# --- Small pieces -------------------------------------------------------------


def decode(raw):
    """Bytes to text, by BOM. Teams writes UTF-16 LE with a BOM."""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    # No BOM: a UTF-16 file without one has a NUL in every other byte.
    if raw[1:2] == b"\x00":
        return raw.decode("utf-16-le")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ExportError("Not a Teams attendance export: the text encoding is not recognized.")


def parse_duration(text):
    """'3h 1m 41s', '3h 26s', '36m 6s', '45s' to seconds."""
    match = DURATION.match(text or "")
    if not text or not text.strip() or not match or not any(match.groups()):
        raise ExportError(f"{text!r} is not a duration Teams writes.")
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def parse_timestamp(text):
    match = TIMESTAMP.match(text or "")
    if not match:
        raise ExportError(f"{text!r} is not a timestamp Teams writes.")
    a, b, year, hour, minute, second, meridiem = match.groups()
    year = int(year)
    if year < 100:
        year += 2000
    hour = int(hour)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ExportError(f"{text!r} has an impossible hour.")
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    return RawTimestamp(text.strip(), int(a), int(b), year, hour, int(minute), int(second or 0))


def clean_display_name(name):
    """
    For showing a name to staff: Teams' '(External)', '(CUSM)' suffixes
    removed. The stored observation keeps them.
    """
    previous = None
    name = (name or "").strip()
    while previous != name:
        previous, name = name, NAME_SUFFIX.sub("", name).strip()
    return name


# --- Sections -----------------------------------------------------------------


def split_sections(text):
    """{title: [lines]} for every 'N. Title' block."""
    sections = {}
    current = None
    for line in text.splitlines():
        heading = SECTION_HEADING.match(line)
        if heading and "\t" not in line:
            current = heading.group(2)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def rows(lines, width, section):
    """The tab-separated rows of a section after its header, checked for width."""
    body = [line for line in lines if line.strip()]
    if not body:
        raise ExportError(f"Section '{section}' is empty.")
    parsed = list(csv.reader(body, delimiter="\t", quotechar='"'))
    header, data = parsed[0], parsed[1:]
    if len(header) != width:
        raise ExportError(
            f"Section '{section}' has {len(header)} columns; a Teams export has {width}. "
            "Teams may have changed its format."
        )
    for number, row in enumerate(data, start=2):
        if len(row) < width:
            row.extend([""] * (width - len(row)))  # ragged trailing tabs
        elif len(row) > width:
            raise ExportError(f"Section '{section}', row {number}, has too many columns.")
    return header, data


def parse_export(raw):
    """Parse a Teams attendance export. Raises ExportError on anything unusable."""
    text = decode(raw)
    sections = split_sections(text)
    for required in (SUMMARY, PARTICIPANTS, ACTIVITIES):
        if required not in sections:
            raise ExportError(f"Not a Teams attendance export: no '{required}' section.")

    summary = {}
    for line in sections[SUMMARY]:
        if "\t" in line:
            key, value = line.split("\t", 1)
            summary[key.strip()] = value.strip()
    for key in ("Meeting title", "Start time", "End time"):
        if not summary.get(key):
            raise ExportError(f"The summary has no '{key}'.")
    try:
        attended = int(summary.get("Attended participants", ""))
    except ValueError:
        attended = None

    export = ParsedExport(
        title=summary["Meeting title"],
        start=parse_timestamp(summary["Start time"]),
        end=parse_timestamp(summary["End time"]),
        attended_participants=attended,
    )

    _, data = rows(sections[PARTICIPANTS], 15, PARTICIPANTS)
    for row in data:
        export.participants.append(
            ParticipantRow(
                display_name=row[0],
                email=row[4].strip(),
                upn=row[5].strip(),
                role=row[6].strip(),
                in_meeting_seconds=parse_duration(row[3]),
            )
        )

    _, data = rows(sections[ACTIVITIES], 6, ACTIVITIES)
    for number, row in enumerate(data, start=1):
        export.activities.append(
            ActivityRow(
                display_name=row[0],
                join=parse_timestamp(row[1]),
                leave=parse_timestamp(row[2]),
                duration_seconds=parse_duration(row[3]),
                email=row[4].strip(),
                role=row[5].strip(),
                line_number=number,
            )
        )

    if ENGAGEMENT in sections:
        # Not stored. Parsed so a malformed section is noticed, not silently skipped.
        _, data = rows(sections[ENGAGEMENT], 3, ENGAGEMENT)
        export.engagement_rows = len(data)

    export.warnings.extend(checksum_warnings(export))
    return export


def person_key(email, display_name):
    return (email or "").strip().casefold() or clean_display_name(display_name).casefold()


def checksum_warnings(export):
    """
    Section 2 is Teams' own total per person. It should equal the sum of
    that person's Section 3 durations. Disagreement means the parser and
    Teams read the file differently; it is reported, not corrected.
    """
    warnings = []
    if export.attended_participants is not None and export.attended_participants != len(
        export.participants
    ):
        warnings.append(
            f"The summary says {export.attended_participants} participants; "
            f"section 2 lists {len(export.participants)}."
        )
    totals = {}
    counts = {}
    for row in export.activities:
        key = person_key(row.email, row.display_name)
        totals[key] = totals.get(key, 0) + row.duration_seconds
        counts[key] = counts.get(key, 0) + 1
    seen = set()
    for person in export.participants:
        key = person_key(person.email, person.display_name)
        seen.add(key)
        summed = totals.get(key)
        if summed is None:
            warnings.append(f"{person.display_name} is in section 2 but has no rows in section 3.")
            continue
        # Teams rounds each row to the second; allow one second per row.
        if abs(summed - person.in_meeting_seconds) > counts[key]:
            warnings.append(
                f"{person.display_name}: section 2 says {person.in_meeting_seconds} s in the "
                f"meeting, section 3 adds up to {summed} s."
            )
    for key in sorted(set(totals) - seen):
        warnings.append(f"{key} has rows in section 3 but is not in section 2.")
    return warnings


# --- Reading the dates against a known one ----------------------------------


def date_order_for(export, event_date):
    """
    Which reading of the export's dates gives `event_date`. Fails loudly
    when neither does. Never decided by locale or by what looks likely.
    """
    readings = []
    for order in (MONTH_FIRST, DAY_FIRST):
        try:
            if export.start.date_as(order) == event_date:
                readings.append(order)
        except ValueError:
            continue
    if not readings:
        raise ExportError(
            f"This export starts on {export.start.text!r}, which is not {event_date:%d %B %Y} "
            "whichever way the day and month are read. Is this the right event?"
        )
    return readings[0]  # both readings agree when day == month


def resolve_times(export, order, zone=None):
    """(start, end, [(row, join_utc, leave_utc)]) with every timestamp read in `order`."""
    zone = zone or ZoneInfo(settings.TIME_ZONE)
    start = export.start.resolve(order, zone)
    end = export.end.resolve(order, zone)
    resolved = []
    for row in export.activities:
        join, leave = row.join.resolve(order, zone), row.leave.resolve(order, zone)
        if leave < join:
            raise ExportError(f"{row.display_name}: leaves before joining in section 3.")
        resolved.append((row, join, leave))
    return start, end, resolved


def normalize_title(title):
    return " ".join((title or "").split()).casefold()
