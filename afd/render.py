"""Build the static site in docs/ from the archive and the cached summaries."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import MONTH_DAYS, NWS_PRODUCT_PAGE, REPO_URL, Paths
from .store import list_all
from .summarize import day_label, local_day, local_label

TEMPLATES = Path(__file__).parent / "templates"


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _paragraphs(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


def build(paths: Paths, status: dict) -> None:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["paragraphs"] = _paragraphs

    items = list_all(paths)
    discussions = []
    for s in items:
        summary = _read(paths.discussion_summaries / f"{s.id}.json")
        discussions.append({
            "id": s.id, "issued": s.issued, "label": local_label(s.issued),
            "time": local_label(s.issued).split(", ", 1)[1], "url": s.rel_url(paths),
            "day": local_day(s.issued), "summary": summary,
        })

    by_day: dict[date, list[dict]] = defaultdict(list)
    for d in discussions:
        by_day[d["day"]].append(d)

    latest = discussions[-1] if discussions else None
    recent_days = []
    if latest:
        cutoff = latest["day"] - timedelta(days=MONTH_DAYS - 1)
        for d in sorted(by_day, reverse=True):
            if d < cutoff:
                break
            recent_days.append({
                "date": d, "label": day_label(d),
                "summary": _read(paths.day_summaries / f"{d.isoformat()}.json"),
                "discussions": by_day[d],
            })

    months: dict[str, list[dict]] = defaultdict(list)
    for d in sorted(by_day, reverse=True):
        months[f"{d:%B %Y}"].append({"label": day_label(d), "discussions": by_day[d]})

    ctx = {
        "status": status,
        "checked": local_label(datetime.fromisoformat(status["last_check"])) if status.get("last_check") else None,
        "latest": latest,
        "week": _read(paths.data / "week.json"),
        "month": _read(paths.data / "month.json"),
        "days": recent_days,
        "months": months,
        "count": len(discussions),
        "repo_url": REPO_URL,
        "nws_url": NWS_PRODUCT_PAGE,
    }
    paths.docs.mkdir(parents=True, exist_ok=True)
    (paths.docs / "index.html").write_text(env.get_template("index.html.j2").render(**ctx))
    (paths.docs / "archive.html").write_text(env.get_template("archive.html.j2").render(**ctx))
    shutil.copyfile(TEMPLATES / "style.css", paths.docs / "style.css")
    (paths.docs / ".nojekyll").touch()  # serve files as-is; skip GitHub's Jekyll build
