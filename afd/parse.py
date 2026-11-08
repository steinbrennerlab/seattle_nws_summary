"""Turn raw NWS text into individual discussions with a UTC issuance time and named sections."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .config import PIL

# WMO header line, e.g. "FXUS66 KSEW 072245" or "FXUS66 KSEW 072245 AAA" for an amendment.
WMO_RE = re.compile(r"(?m)^FXUS\d\d K[A-Z]{3} \d{6}(?: [A-Z]{3})?[ \t]*$")

# Local issuance line, e.g. "245 PM PST Sat Nov 7 2026" or "1015 AM PDT Mon Oct 12 2026".
LOCAL_RE = re.compile(
    r"(?m)^[ \t]*(\d{1,4}) (AM|PM) (PST|PDT|MST|MDT|CST|CDT|EST|EDT|AKST|AKDT|HST|UTC)"
    r" [A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{4})[ \t]*$"
)
UTC_OFFSETS = {
    "PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6, "CDT": -5,
    "EST": -5, "EDT": -4, "AKST": -9, "AKDT": -8, "HST": -10, "UTC": 0,
}
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# Section headers start a line with a period, e.g. ".SYNOPSIS..." or ".SHORT TERM /TONIGHT THROUGH MONDAY/..."
SECTION_RE = re.compile(r"(?m)^\.([A-Z0-9][A-Z0-9 /&,\-]*?)\.\.\.")
LOWER_PRIORITY = ("AVIATION", "MARINE")


def split_products(raw: str) -> list[str]:
    """Split text holding one or many products (IEM returns them concatenated) into AFDs.

    Products are located by their WMO header line rather than by control characters,
    so this works whether or not the source kept the \\x01/\\x03 start/end markers.
    """
    text = raw.replace("\r", "").replace("\x01", "").replace("\x03", "")
    starts = [m.start() for m in WMO_RE.finditer(text)]
    products = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[start:end]
        # Drop the next product's leading sequence number ("000", "512"), if it got attached here.
        chunk = re.sub(r"\n\s*\d{3}\s*$", "\n", chunk.rstrip()).rstrip() + "\n"
        if re.search(rf"(?m)^{PIL}\s*$", chunk):
            products.append(chunk)
    return products


def issuance_utc(text: str) -> datetime:
    """Read the local issuance line and convert it to UTC."""
    m = LOCAL_RE.search(text)
    if not m:
        raise ValueError("no local issuance time line found")
    hhmm, ampm, zone, mon, day, year = m.groups()
    hour, minute = (int(hhmm[:-2]), int(hhmm[-2:])) if len(hhmm) > 2 else (int(hhmm), 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        raise ValueError(f"bad time {hhmm} {ampm}")
    hour = hour % 12 + (12 if ampm == "PM" else 0)
    local = datetime(int(year), MONTHS[mon.title()], int(day), hour, minute)
    return (local - timedelta(hours=UTC_OFFSETS[zone])).replace(tzinfo=timezone.utc)


def product_id(issued: datetime) -> str:
    return f"{PIL}_{issued.astimezone(timezone.utc):%Y%m%d%H%M}"


def id_to_utc(pid: str) -> datetime:
    stamp = pid.split("_")[1]
    return datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def fingerprint(text: str) -> str:
    """Hash of the product with whitespace collapsed, so the same discussion fetched from
    the NWS API and from IEM is recognized as identical."""
    m = WMO_RE.search(text)
    body = text[m.start():] if m else text
    return hashlib.sha256(" ".join(body.split()).encode()).hexdigest()


def sections(text: str) -> list[tuple[str, str]]:
    """Return (name, body) pairs. Bodies end at the next header, '&&', or '$$'."""
    matches = list(SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end]
        body = re.split(r"(?m)^\s*(?:&&|\$\$)\s*$", body)[0].strip()
        out.append((m.group(1).strip(), body))
    return out


def is_lower_priority(section_name: str) -> bool:
    return section_name.upper().startswith(LOWER_PRIORITY)
