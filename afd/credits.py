"""Credits for notable forecasting, kept in an append-only ledger: docs/data/credits.json.

Three kinds, positive only, each backed by the forecaster's own words:
  early        first to flag something that later happened
  uncertainty  flagged doubt that proved justified
  clear        explained the reasoning especially well

Each discussion summary records up to three forward-looking "calls" with a verified quote and
the signer (summarize.attribute_calls). Once a week has HINDSIGHT_DAYS of later discussions
after it, a stronger model judges that week once: the calls against what later discussions
said happened, and the week's forecast-desk writing for clarity. Weeks are judged once and
never again, so credits do not shift as the 30-day window moves.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from . import parse
from .config import Paths
from .store import list_all
from .summarize import LLM, _squash, ask_json, attribute_calls, day_label, local_day, local_label

CATEGORIES = {"early": "Called it early", "uncertainty": "Honest about uncertainty",
              "clear": "Clear explanation"}
HINDSIGHT_DAYS = 3      # a week is judged once this many later days have been discussed
PER_CATEGORY = 2        # at most this many credits per category per week
LATER_DAYS = 7          # how far past the week the "what happened" evidence reaches
STYLE_WORDS = 700       # per discussion, in the clarity request

HINDSIGHT_TASK = """Below are forward-looking calls from Seattle forecast discussions issued {span}, \
oldest first, each with an ID. After them are explanations of the discussions that came later, \
which describe how things turned out.

Award at most {per} credits in each category. None is fine; most weeks should have few. Credit \
only what the later discussions confirm.
"early": the earliest call that clearly anticipated something that then happened, made before \
later discussions treated it as likely. Prefer calls made 3 or more days ahead.
"uncertainty": a call that flagged doubt (stance possible, doubtful, or uncertain, or questioning \
a model) where the doubt proved justified: the favored scenario changed or did not happen.

Return JSON: {{"credits": [{{"category": "early" or "uncertainty", "call_id": "...", \
"why": "one sentence citing what the later discussions said happened"}}]}}

CALLS
{calls}

LATER DISCUSSIONS
{later}"""

STYLE_TASK = """Below are the short- and long-term forecast sections of the Seattle forecast \
discussions issued {span}, as the signing forecaster wrote them (text carried over from earlier \
discussions is removed). Each starts with its discussion ID.

Pick at most {per} that best explain WHY: the pattern behind the weather, why models disagree, \
what would change the forecast. Judge reasoning and clarity, not length or confidence. None is \
fine if nothing stands out.

Return JSON: {{"credits": [{{"discussion": "...", "quote": "their exact words, copied character \
for character, at most 40 words", "why": "one sentence on what makes it clear"}}]}}

{sections}"""


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _credit_id(category: str, discussion: str, quote: str) -> str:
    return f"{category}:{discussion}:{hashlib.sha256(_squash(quote).encode()).hexdigest()[:8]}"


def _forecast_text(text: str) -> str:
    """The short- and long-term sections, without text carried over from earlier discussions."""
    parts = []
    for name, body in parse.sections(text):
        key = name.split(" /")[0].strip()
        if parse.DESKS.get(key) in ("short_term", "long_term"):
            parts.append(parse.PREVIOUS_RE.split(body)[0].strip())
    return " ".join(" ".join(parts).split()[:STYLE_WORDS])


def judge_week(paths: Paths, llm: LLM, start: date, end: date, today: date) -> list[dict]:
    items = [s for s in list_all(paths) if start <= local_day(s.issued) <= end]
    span = f"{day_label(start)} to {day_label(end)}, {end.year}"
    found: list[dict] = []

    # Hindsight: calls made this week, against the explanations of what came after.
    calls = {}
    for s in items:
        summary = _read(paths.discussion_summaries / f"{s.id}.json") or {}
        for i, c in enumerate(summary.get("calls") or []):
            calls[f"{s.id}#{i}"] = (s, c)
    later = []
    d = start + timedelta(days=1)
    while d <= min(today, end + timedelta(days=LATER_DAYS)):
        if day := _read(paths.day_summaries / f"{d.isoformat()}.json"):
            later.append(f"[{day_label(d)}] {day.get('headline', '')}\n{day.get('summary', '')}")
        d += timedelta(days=1)
    if calls and later:
        listed = "\n\n".join(
            f"ID {cid} | {local_label(s.issued)} | forecaster {c['signer']} | about {c.get('about', '')} "
            f"| stance {c.get('stance', '')}\n{c.get('claim', '')}\nTheir words: \"{c.get('quote', '')}\""
            for cid, (s, c) in calls.items())
        data = ask_json(llm, HINDSIGHT_TASK.format(span=span, per=PER_CATEGORY, calls=listed,
                                                   later="\n\n".join(later)), 1500)
        counts = {"early": 0, "uncertainty": 0}
        for cr in data.get("credits") or []:
            cat, cid = cr.get("category"), cr.get("call_id")
            if cat not in counts or cid not in calls or counts[cat] >= PER_CATEGORY:
                continue
            counts[cat] += 1
            s, c = calls[cid]
            found.append({"category": cat, "signer": c["signer"], "discussion": s.id,
                          "issued": s.issued.isoformat(), "quote": c["quote"],
                          "claim": c.get("claim", ""), "why": cr.get("why", "")})

    # Clarity: the week's forecast-desk writing.
    texts = {s.id: s for s in items}
    sections = "\n\n".join(f"=== {s.id} ({local_label(s.issued)})\n{body}"
                           for s in items if (body := _forecast_text(s.text())))
    if sections:
        data = ask_json(llm, STYLE_TASK.format(span=span, per=PER_CATEGORY, sections=sections), 1500)
        n = 0
        for cr in data.get("credits") or []:
            s = texts.get(cr.get("discussion"))
            if not s or n >= PER_CATEGORY:
                continue
            # The quote must really be in what that forecaster wrote; this also finds the signer.
            if not (attributed := attribute_calls(s.text(), [{"quote": str(cr.get("quote", ""))}])):
                continue
            n += 1
            found.append({"category": "clear", "signer": attributed[0]["signer"], "discussion": s.id,
                          "issued": s.issued.isoformat(), "quote": cr["quote"], "why": cr.get("why", "")})

    for f in found:
        f.update(id=_credit_id(f["category"], f["discussion"], f["quote"]),
                 week=start.isoformat(), awarded=today.isoformat())
    return found


def run(paths: Paths, llm: LLM, today: date, log=print) -> list[str]:
    """Judge every week that is ready and not yet judged. Returns errors."""
    path = paths.data / "credits.json"
    ledger = _read(path) or {"judged_through": None, "credits": []}
    if ledger["judged_through"] is None:
        # Credits start with the first discussion summarized with calls; no backfill.
        days = [local_day(s.issued) for s in list_all(paths)
                if "calls" in (_read(paths.discussion_summaries / f"{s.id}.json") or {})]
        if not days:
            return []
        ledger["judged_through"] = (min(days) - timedelta(days=1)).isoformat()
    errors = []
    while True:
        start = date.fromisoformat(ledger["judged_through"]) + timedelta(days=1)
        end = start + timedelta(days=6)
        if today < end + timedelta(days=HINDSIGHT_DAYS):
            break
        try:
            new = judge_week(paths, llm, start, end, today)
        except Exception as e:
            errors.append(f"credits for week of {start}: {e}")
            break
        known = {c["id"] for c in ledger["credits"]}
        ledger["credits"] += [c for c in new if c["id"] not in known]
        ledger["judged_through"] = end.isoformat()
        log(f"judged credits for the week of {start}: {len(new)} awarded")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return errors
