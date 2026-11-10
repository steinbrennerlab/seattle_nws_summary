"""Who wrote what: profiles of the forecasters who sign the discussions.

Each desk's section ends with the forecaster's office ID (see parse.desk_signatures). The office
does not publish which person uses which number, and this module does not try to find out; a
few forecasters sign with a surname, and those are shown as they signed.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from . import parse
from .config import FORECASTER_COLORS, LOCAL_TZ, REGULAR_MIN
from .store import Stored

DESK_LABELS = {"forecast": "Forecast", "aviation": "Aviation", "marine": "Marine"}
# The page's own words for each desk, as section names in a discussion.
SOURCE_LABELS = {"short_term": "Short term", "long_term": "Long term",
                 "aviation": "Aviation", "marine": "Marine"}

# Impressions of each regular's writing, from reading their sections in Oct-Nov 2026. About the
# writing only. Forecasters without a note still get a card with the measured details.
NOTES = {
    "Felton": "Clipped, telegraphic sentences (\"High temperatures near normal, upper 50s to mid 60s.\") "
              "and climate context: \"the coldest morning since May.\" Watches convergence zones closely.",
    "Mazurkiewicz": "Friendly and practical: \"we'll,\" \"temps,\" and advice to protect sensitive "
                    "plants when a freeze is coming.",
    "27": "Precise with some flair (\"eerily quiet,\" \"a recipe for a pretty quiet period\"). "
          "Cites ensemble 500-millibar maps from the American, European, and Canadian models.",
    "33": "Short and conversational: the briefest sentences on the desk, with \"we're\" and "
          "\"there's\" and plain impact words like \"wetting rains.\"",
    "14": "Long and analytical. Talks about ensemble \"clusters\" and model \"discrepancies,\" "
          "and walks through what satellite and radar show.",
    "29": "Puts numbers on uncertainty (\"a 55-70% chance for gusts exceeding 21 kt\") and "
          "reports how ensemble members split.",
    "21": "Mostly the long term. Measured and quantitative (\"a few hundredths of an inch\"), "
          "with an eye on frost and freezes that matter to farms and gardens.",
    "18": "Long and folksy: a front will \"drag its feet,\" then \"a break in the action.\" "
          "Uses the old NWS habit of slash-bracketed asides.",
    "HPR": "Detailed on fog and low clouds: which pockets \"survive the burnoff\" and where. "
           "Open about disagreement among the models.",
    "62": "The longest sections. Starts from what radar and satellite show right now, "
          "and covers marine detail like wave periods.",
    "15": "The longest sentences. Big-picture and careful about how far models agree, "
          "with terms like rain shadowing and atmospheric rivers.",
    "41": "Terse and abbreviated, aviation and marine only, with stock phrases: \"in store,\" "
          "\"slated,\" \"westerly pushes are favored.\"",
    "JD": "Reports what already fell (rainfall totals in inches by area) before looking ahead.",
}


@dataclass
class Profile:
    id: str
    discussions: int = 0                              # discussions with at least one desk they signed
    desks: Counter = field(default_factory=Counter)   # forecast / aviation / marine, once per discussion
    hours: list[int] = field(default_factory=lambda: [0] * 24)  # local issuance hour of those discussions
    partners: Counter = field(default_factory=Counter)
    first: datetime | None = None
    last: datetime | None = None
    words: list[str] = field(default_factory=list)
    slot: int = 0                                     # color slot 1-8; 0 means no color of their own
    regular: bool = False
    credits: list[dict] = field(default_factory=list)  # from credits.json, filled in by render

    def credit_counts(self) -> list[tuple[str, str, int]]:
        """(category, label, count) for each category they have credits in."""
        from .credits import CATEGORIES
        return [(k, v, n) for k, v in CATEGORIES.items()
                if (n := sum(c["category"] == k for c in self.credits))]

    @property
    def note(self) -> str | None:
        return NOTES.get(self.id)

    @property
    def anchor(self) -> str:
        return f"f-{self.id}"

    @property
    def main_desk(self) -> str:
        return self.desks.most_common(1)[0][0] if self.desks else "forecast"

    def desk_rows(self) -> list[tuple[str, int]]:
        return [(DESK_LABELS[d], self.desks[d]) for d in DESK_LABELS]

    def tip(self) -> str:
        """One line for a hover title."""
        desk = DESK_LABELS[self.main_desk].lower()
        return f"Forecaster {self.id}: {self.discussions} discussions, mostly the {desk} desk" + (
            f". {self.note}" if self.note else "")


def _desk(source: str) -> str:
    return "forecast" if source in ("short_term", "long_term") else source


def _authored_text(text: str) -> dict[str, str]:
    """Text each lead signer wrote in this discussion, without carried-over earlier text."""
    out: dict[str, list[str]] = defaultdict(list)
    for name, body in parse.sections(text):
        key = re.split(r"\s*/", name, maxsplit=1)[0].strip()
        if key not in parse.DESKS:
            continue
        sig = parse.section_signature(body)
        if not sig:
            continue
        body = parse.PREVIOUS_RE.split(body)[0]
        out[sig[0]].append(body)
    return {k: " ".join(v) for k, v in out.items()}


def _distinctive(words: dict[str, Counter], n: int = 6) -> dict[str, list[str]]:
    """Words each forecaster uses noticeably more than everyone else (smoothed log-odds)."""
    total = sum(words.values(), Counter())
    out = {}
    for who, mine in words.items():
        size = sum(mine.values())
        rest = total - mine
        rest_size = sum(rest.values()) or 1
        if size < 500:
            out[who] = []
            continue
        scored = sorted(((math.log((mine[w] + 0.5) / size) - math.log((rest[w] + 0.5) / rest_size), w)
                         for w, c in mine.items() if c >= 5 and len(w) > 3), reverse=True)
        out[who] = [w for _, w in scored[:n]]
    return out


def build(items: list[Stored]) -> tuple[dict[str, Profile], dict[str, dict[str, list[str]]]]:
    """Profiles keyed by ID, and each discussion's desk signatures keyed by discussion id."""
    profiles: dict[str, Profile] = {}
    signed: dict[str, dict[str, list[str]]] = {}
    words: dict[str, Counter] = defaultdict(Counter)

    def get(who: str) -> Profile:
        return profiles.setdefault(who, Profile(who))

    for item in items:
        text = item.text()
        desks = parse.desk_signatures(text)
        signed[item.id] = desks
        leads: dict[str, set[str]] = defaultdict(set)
        for source, ids in desks.items():
            leads[ids[0]].add(_desk(source))
        seen_pairs = set()
        for ids in desks.values():
            for other in ids[1:]:
                if other != ids[0] and (ids[0], other) not in seen_pairs:
                    seen_pairs.add((ids[0], other))
                    get(ids[0]).partners[other] += 1
                    get(other).partners[ids[0]] += 1
        hour = item.issued.astimezone(LOCAL_TZ).hour
        for who, which in leads.items():
            p = get(who)
            p.discussions += 1
            p.desks.update(which)
            p.hours[hour] += 1
            p.first = min(p.first or item.issued, item.issued)
            p.last = max(p.last or item.issued, item.issued)
        for who, body in _authored_text(text).items():
            words[who].update(w for w in re.findall(r"[a-z][a-z'-]+[a-z]", body.lower())
                              if w != who.lower())

    for who, w in _distinctive(words).items():
        if who in profiles:
            profiles[who].words = w
    for p in profiles.values():
        p.regular = p.discussions >= REGULAR_MIN
        p.slot = FORECASTER_COLORS.get(p.id, 0)
    return profiles, signed
