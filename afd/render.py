"""Build the static site in docs/ from the archive and the cached summaries."""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import MONTH_DAYS, NWS_PRODUCT_PAGE, REPO_URL, SUMMARY_WINDOW_DAYS, Paths
from .store import list_all
from .summarize import day_label, local_day, local_label

TEMPLATES = Path(__file__).parent / "templates"


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _paragraphs(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


# The page's weather "mood": from a headline (plus hazards), the most notable kind of weather
# forecast. First match wins, so the order is by how much it matters to a reader.
SKY_KINDS = [
    ("storm", "Stormy", r"high wind|windstorm|damaging|thunder|gale|storm warning|power outage"),
    ("snow", "Lowland snow", r"lowland snow|snow (?:in|to|for|across) the lowlands|snow to sea level|winter storm"),
    ("wind", "Windy", r"wind advisory|gust|windy|breezy|strong wind"),
    ("rain", "Rain", r"rain|shower|atmospheric river|\bwet\b|drizzle"),
    ("fog", "Fog and low clouds", r"\bfog|low cloud|marine (?:layer|push)|stratus"),
    ("sun", "Dry and bright", r"\bdry\b|sun|clear|warm|high pressure|ridge"),
]
SKY_LABELS = {k: label for k, label, _ in SKY_KINDS} | {"cloud": "Clouds, little else"}
RAINY = {"storm", "wind", "rain", "snow"}


def sky_kind(*texts: str | None) -> str:
    text = " ".join(t or "" for t in texts).lower()
    for kind, _, pattern in SKY_KINDS:
        if re.search(pattern, text):
            return kind
    return "cloud"


# Hazards in the summaries are free text ("No watches...", "Possible high surf...", "Small Craft
# Advisory for the coastal waters..."). For the day tiles, keep only the names of products in
# effect for land, such as "Wind Advisory" or "High Wind Warning".
PRODUCT_NAME = re.compile(r"\b((?:[A-Z][a-z]+(?:[ -]| and ))*?(?:[A-Z][a-z]+ )(?:Warning|Watch|Advisory|Statement))\b")
MARINE = re.compile(r"small craft|gale|hazardous seas|hurricane force|^storm (?:warning|watch)|brisk wind|special marine", re.I)
NOT_ISSUED = re.compile(r"^\s*no\b|not yet|not an official|possible|potential|may need|could|monitor", re.I)


def land_products(hazards: list[str]) -> list[str]:
    names = []
    for text in hazards:
        if NOT_ISSUED.search(text):
            continue
        for name in PRODUCT_NAME.findall(text):
            if not MARINE.search(name):
                names.append(name)
    return list(dict.fromkeys(names))


def build(paths: Paths, status: dict) -> None:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["paragraphs"] = _paragraphs
    env.filters["daylabel"] = lambda iso: day_label(date.fromisoformat(iso))

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
    strip = []
    sky = "cloud"
    if latest:
        s = latest["summary"] or {}
        sky = sky_kind(s.get("headline"), " ".join(s.get("hazards") or []))
        cutoff = latest["day"] - timedelta(days=MONTH_DAYS - 1)
        for d in sorted(by_day, reverse=True):
            if d < cutoff:
                break
            summary = _read(paths.day_summaries / f"{d.isoformat()}.json")
            first = next((x["summary"] for x in reversed(by_day[d]) if x["summary"]), None)
            headline = (summary or first or {}).get("headline")
            kind = sky_kind(headline) if headline else None
            # Land watches, warnings, and advisories named in any of the day's discussions.
            hazards = land_products([h for x in by_day[d] for h in (x["summary"] or {}).get("hazards") or []])
            recent_days.append({
                "hazards": hazards,
                "date": d, "label": day_label(d), "anchor": f"d-{d.isoformat()}",
                "summary": summary, "discussions": by_day[d],
                "kind": kind, "kind_label": SKY_LABELS.get(kind), "headline": headline,
            })
        # One tile per calendar day, oldest first; a day with no discussion is an empty tile.
        known = {x["date"]: x for x in recent_days}
        strip = [known.get(cutoff + timedelta(days=i)) or {"date": cutoff + timedelta(days=i),
                 "label": day_label(cutoff + timedelta(days=i)), "kind": None, "hazards": []}
                 for i in range(MONTH_DAYS)]
    rated = [x for x in strip if x["kind"]]
    tally = {"rainy": sum(x["kind"] in RAINY for x in rated), "of": len(rated)}
    used = [k for k in [*[k for k, _, _ in SKY_KINDS], "cloud"] if any(x["kind"] == k for x in rated)]

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
        "sky": sky,
        "sky_label": SKY_LABELS[sky],
        "strip": strip,
        "tally": tally,
        "legend": [(k, SKY_LABELS[k]) for k in used],
        "months": months,
        "count": len(discussions),
        "window_days": SUMMARY_WINDOW_DAYS,
        "repo_url": REPO_URL,
        "nws_url": NWS_PRODUCT_PAGE,
    }
    paths.docs.mkdir(parents=True, exist_ok=True)
    (paths.docs / "index.html").write_text(env.get_template("index.html.j2").render(**ctx), encoding="utf-8")
    (paths.docs / "archive.html").write_text(env.get_template("archive.html.j2").render(**ctx), encoding="utf-8")
    shutil.copyfile(TEMPLATES / "style.css", paths.docs / "style.css")
    (paths.docs / ".nojekyll").touch()  # serve files as-is; skip GitHub's Jekyll build
