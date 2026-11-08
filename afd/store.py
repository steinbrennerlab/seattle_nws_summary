"""The archive is one plain-text file per discussion: docs/archive/YYYY/MM/AFDSEW_YYYYMMDDHHMM.txt (UTC).

The file names are the index. Nothing else needs to stay in sync with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Paths
from .parse import fingerprint, id_to_utc, issuance_utc, product_id


@dataclass(frozen=True)
class Stored:
    id: str
    issued: datetime          # UTC
    path: Path

    def text(self) -> str:
        return self.path.read_text()

    def rel_url(self, paths: Paths) -> str:
        """Path relative to docs/, which is also its URL on the Pages site."""
        return self.path.relative_to(paths.docs).as_posix()


def archive_path(paths: Paths, pid: str) -> Path:
    issued = id_to_utc(pid.split("_v")[0])
    return paths.archive / f"{issued:%Y}" / f"{issued:%m}" / f"{pid}.txt"


def list_all(paths: Paths) -> list[Stored]:
    items = []
    for p in paths.archive.glob("*/*/*.txt"):
        pid = p.stem
        base = pid.split("_v")[0]
        items.append(Stored(pid, id_to_utc(base), p))
    return sorted(items, key=lambda s: (s.issued, s.id))


def known_fingerprints(paths: Paths) -> set[str]:
    return {fingerprint(s.text()) for s in list_all(paths)}


def save(paths: Paths, text: str, seen: set[str] | None = None) -> str | None:
    """Save one product. Returns its id, or None if an identical product is already archived.

    Pass `seen` (from known_fingerprints) when saving many products, to avoid rehashing the archive.
    """
    if seen is None:
        seen = known_fingerprints(paths)
    fp = fingerprint(text)
    if fp in seen:
        return None
    pid = product_id(issuance_utc(text))
    # A correction can carry the same issuance minute as the original; keep both.
    candidate, n = pid, 2
    while archive_path(paths, candidate).exists():
        candidate, n = f"{pid}_v{n}", n + 1
    path = archive_path(paths, candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    seen.add(fp)
    return candidate
