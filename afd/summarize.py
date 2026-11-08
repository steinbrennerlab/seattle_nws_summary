"""Hierarchical summaries, cached as JSON under docs/data/.

  each discussion  -> docs/data/discussions/<id>.json   (written once, never redone)
  each Pacific day -> docs/data/days/YYYY-MM-DD.json     (redone when that day gains a discussion)
  last 7 days      -> docs/data/week.json                (redone when any of its days change)
  last 30 days     -> docs/data/month.json               (same)

Week and month summaries are built from day summaries, and day summaries from discussion
summaries, so no single request ever has to hold a month of raw text.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from . import parse
from .config import LOCAL_TZ, MODEL, MONTH_DAYS, SUMMARY_WINDOW_DAYS, WEEK_DAYS, Paths
from .store import Stored, list_all

SYSTEM = """You explain Area Forecast Discussions from the National Weather Service office in Seattle, \
which covers western Washington, for a public web page.

Readers are college-educated weather enthusiasts who are not meteorologists. Write plainly and directly. \
When the discussion uses a technical term (for example: upper-level ridge, shortwave trough, \
atmospheric river, onshore flow, Puget Sound convergence zone, QPF, ensemble spread, snow level), \
either replace it with plain words or keep it and explain it, and list it in the glossary.

Stay faithful to the source. Do not add temperatures, amounts, times, or places the forecasters did \
not give. Keep their stated uncertainty: if they say a detail is unlikely or uncertain, say so. \
Use the day names and Pacific local times the discussion uses.

Focus on weather that people in the Seattle area and western Washington notice: lowlands, mountains, \
and passes. Aviation and marine details matter less to this audience. Mention them only when they \
describe something the general public would notice, such as strong winds or dense fog.

Respond with one JSON object and nothing else: no Markdown fences, no text before or after it."""

DISCUSSION_TASK = """Explain this discussion, issued {issued}.

Return JSON with exactly these keys:
"headline": one sentence, at most 20 words, stating the main forecast point.
"summary": two or three short paragraphs separated by a blank line, explaining what forecasters \
expect and why, in plain language.
"timeline": a list of 3 to 6 objects {{"when": "...", "what": "..."}} covering the forecast period \
in order, using the discussion's own time ranges ("Tonight", "Monday", "Late next week").
"confidence": one or two sentences on where forecasters are confident or uncertain, and why.
"hazards": a list of active or expected watches, warnings, and advisories in plain words; [] if none.
"glossary": a list of 3 to 8 objects {{"term": "...", "definition": "..."}} for terms from this \
discussion a reader may not know. Definitions are one or two sentences.

{body}"""

DAY_TASK = """Below are explanations of every forecast discussion issued on {day}, in order.

Return JSON with exactly these keys:
"headline": one sentence, at most 20 words, stating what forecasters expected as of the end of the day.
"summary": one paragraph describing the forecast as it stood at the end of the day.
"changes": one or two sentences on how the forecast changed between this day's discussions. \
Use "" if there was only one discussion or nothing meaningful changed.

{items}"""

PERIOD_TASK = """Below are day-by-day summaries of the Seattle forecast discussions for {span}.

These describe what forecasters expected each day, not what weather actually happened. \
Write about the forecasting: what forecasters expected at the start, what they changed their \
minds about and when, which forecasts held steady, and what they expect now.

Return JSON with exactly these keys:
"headline": one sentence, at most 20 words.
"summary": {paragraphs} short paragraphs separated by a blank line.
"turning_points": a list of 2 to {max_points} objects {{"date": "...", "what": "..."}} marking the days \
the forecast changed most, in order. Use dates as written below.

{items}"""


class LLM(Protocol):
    def complete(self, system: str, user: str, max_tokens: int) -> str: ...


class AnthropicLLM:
    def __init__(self, model: str = MODEL):
        import anthropic  # imported here so the archive and page still build without it
        self.client = anthropic.Anthropic()
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        msg = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "max_tokens":
            raise CutOff(text)
        return text


class CutOff(Exception):
    """The reply hit max_tokens, so its JSON is incomplete."""


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ask_json(llm: LLM, user: str, max_tokens: int = 2000) -> dict:
    """Ask for JSON and parse it, tolerating fences or stray text around the object.

    Retries once: with twice the token budget if the reply was cut off, otherwise with a
    reminder to reply with only the JSON object.
    """
    prompt, raw = user, ""
    for attempt in range(2):
        try:
            raw = llm.complete(SYSTEM, prompt, max_tokens)
        except CutOff as e:
            raw = str(e)
            max_tokens *= 2
            continue
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        prompt = user + "\n\nYour previous reply was not a single valid JSON object. Reply with only the JSON object."
    snippet = " ".join(raw.split())
    raise ValueError(f"model did not return valid JSON (reply began: {snippet[:150]!r}, "
                     f"ended: {snippet[-150:]!r})")


# ---------- helpers ----------

def local_day(dt: datetime) -> date:
    return dt.astimezone(LOCAL_TZ).date()


def local_label(dt: datetime) -> str:
    t = dt.astimezone(LOCAL_TZ)
    return f"{t:%a %b} {t.day}, {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'} {t:%Z}"


def day_label(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _signature(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def discussion_body(text: str) -> str:
    """General-audience sections first; aviation and marine moved to the end and labeled."""
    secs = parse.sections(text)
    if not secs:
        return text
    main = [f".{n}...\n{b}" for n, b in secs if not parse.is_lower_priority(n)]
    low = [f".{n}...\n{b}" for n, b in secs if parse.is_lower_priority(n)]
    body = "\n\n".join(main)
    if low:
        body += "\n\n[Lower priority for this audience]\n\n" + "\n\n".join(low)
    return body


# ---------- the three levels ----------

def summarize_discussion(llm: LLM, item: Stored) -> dict:
    data = ask_json(llm, DISCUSSION_TASK.format(issued=local_label(item.issued),
                                                body=discussion_body(item.text())), 4000)
    return {"id": item.id, "issued": item.issued.isoformat(), **data}


def summarize_day(llm: LLM, d: date, discs: list[dict]) -> dict:
    items = "\n\n".join(
        f"[{local_label(datetime.fromisoformat(x['issued']))}]\n"
        f"Headline: {x.get('headline', '')}\n{x.get('summary', '')}\n"
        f"Confidence: {x.get('confidence', '')}\nHazards: {', '.join(x.get('hazards') or []) or 'none'}"
        for x in discs)
    data = ask_json(llm, DAY_TASK.format(day=day_label(d), items=items), 1200)
    return {"date": d.isoformat(), **data}


def summarize_period(llm: LLM, days: list[dict], *, paragraphs: str, max_points: int) -> dict:
    first, last = date.fromisoformat(days[0]["date"]), date.fromisoformat(days[-1]["date"])
    span = f"{day_label(first)} to {day_label(last)}, {last.year}"
    items = "\n\n".join(
        f"[{day_label(date.fromisoformat(x['date']))}]\n{x.get('headline', '')}\n{x.get('summary', '')}"
        + (f"\nChanges during the day: {x['changes']}" if x.get("changes") else "")
        for x in days)
    data = ask_json(llm, PERIOD_TASK.format(span=span, paragraphs=paragraphs,
                                            max_points=max_points, items=items), 2500)
    return {"start": first.isoformat(), "end": last.isoformat(), **data}


@dataclass
class Progress:
    errors: list[str] = field(default_factory=list)
    made: int = 0          # discussion summaries written in this run
    done: int = 0          # discussions in the window that have a summary
    total: int = 0         # discussions in the window
    deferred: int = 0      # left for a later batch because of the limit
    failing: int = 0       # failed recently; held back until RETRY_FAILED_AFTER has passed

    @property
    def finished(self) -> bool:
        return self.deferred == 0


# A discussion that fails is not retried until this much time has passed, so one bad
# discussion cannot use up every batch, and the next daily run tries it again.
RETRY_FAILED_AFTER = timedelta(hours=12)


def run(paths: Paths, llm: LLM, now: datetime | None = None, log=print,
        limit: int | None = None) -> Progress:
    """Fill in missing or outdated summaries.

    With `limit`, at most that many discussions are attempted (successes and failures both
    count); the rest are deferred to the next call. Days with deferred discussions, and the week
    and month, wait until nothing is deferred, so they are not written and then rewritten batch
    after batch. Failures are recorded in docs/data/failures.json with the error.
    """
    now = now or datetime.now(timezone.utc)
    today = local_day(now)
    p = Progress()
    errors = p.errors
    window_start = now - timedelta(days=SUMMARY_WINDOW_DAYS)
    failures_path = paths.data / "failures.json"
    failures: dict[str, dict] = _read(failures_path) or {}
    attempted = 0

    # 1. Discussions
    by_day: dict[date, list[dict]] = defaultdict(list)
    waiting: set[date] = set()
    for item in list_all(paths):
        if item.issued < window_start:
            continue
        p.total += 1
        out = paths.discussion_summaries / f"{item.id}.json"
        summary = _read(out)
        if summary is None:
            failed = failures.get(item.id)
            if failed and now - datetime.fromisoformat(failed["at"]) < RETRY_FAILED_AFTER:
                p.failing += 1
                continue
            if limit is not None and attempted >= limit:
                p.deferred += 1
                waiting.add(local_day(item.issued))
                continue
            attempted += 1
            try:
                summary = summarize_discussion(llm, item)
                _write(out, summary)
                failures.pop(item.id, None)
                p.made += 1
                log(f"summarized {item.id} ({attempted}{f'/{limit}' if limit else ''} this batch)")
            except Exception as e:  # keep going; this one is retried after RETRY_FAILED_AFTER
                errors.append(f"{item.id}: {e}")
                failures[item.id] = {"at": now.isoformat(timespec="seconds"),
                                     "error": f"{type(e).__name__}: {e}"[:500]}
                p.failing += 1
                continue
        p.done += 1
        by_day[local_day(item.issued)].append(summary)

    if failures or failures_path.exists():
        _write(failures_path, failures)

    # 2. Days
    day_summaries: dict[date, dict] = {}
    for d, discs in sorted(by_day.items()):
        if d in waiting:
            continue
        sig = _signature([x["id"] for x in discs])
        out = paths.day_summaries / f"{d.isoformat()}.json"
        cached = _read(out)
        if cached and cached.get("signature") == sig:
            day_summaries[d] = cached
            continue
        try:
            summary = {**summarize_day(llm, d, discs), "signature": sig,
                       "discussions": [x["id"] for x in discs]}
            _write(out, summary)
            day_summaries[d] = summary
            log(f"summarized day {d}")
        except Exception as e:
            errors.append(f"day {d}: {e}")
            if cached:
                day_summaries[d] = cached

    # 3. Week and month
    if not p.finished:
        log(f"{p.deferred} discussions left for the next batch; week and month wait until then")
        return p
    for name, n_days, paragraphs, max_points in (("week", WEEK_DAYS, "two or three", 5),
                                                  ("month", MONTH_DAYS, "three or four", 8)):
        first = today - timedelta(days=n_days - 1)
        days = [day_summaries[d] for d in sorted(day_summaries) if first <= d <= today]
        if not days:
            continue
        sig = _signature([f"{x['date']}:{x['signature']}" for x in days])
        out = paths.data / f"{name}.json"
        cached = _read(out)
        if cached and cached.get("signature") == sig:
            continue
        try:
            _write(out, {**summarize_period(llm, days, paragraphs=paragraphs, max_points=max_points),
                         "signature": sig})
            log(f"summarized {name}")
        except Exception as e:
            errors.append(f"{name}: {e}")
    return p
