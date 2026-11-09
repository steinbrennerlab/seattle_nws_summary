"""Settings shared by every step. Change OFFICE to point the project at another NWS office."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

OFFICE = "SEW"                      # NWS Seattle
PIL = f"AFD{OFFICE}"                # product identifier: Area Forecast Discussion, Seattle
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

REPO_URL = "https://github.com/steinbrennerlab/seattle_nws_summary"
SITE_URL = "https://steinbrennerlab.github.io/seattle_nws_summary/"
NWS_PRODUCT_PAGE = (
    "https://forecast.weather.gov/product.php?site=NWS&issuedby=SEW&product=AFD"
    "&format=CI&version=1&glossary=1&highlight=off"
)
# api.weather.gov rejects requests without a User-Agent that identifies the caller.
USER_AGENT = f"seattle_nws_summary ({REPO_URL})"

# Each discussion and each day gets a short, structured explanation: the most numerous calls
# with the most input, so they use the cheaper model. The week and month take more judgment.
DISCUSSION_MODEL = os.environ.get("AFD_DISCUSSION_MODEL", "claude-haiku-5-5")
MODEL = os.environ.get("AFD_MODEL", "claude-sonnet-5-5")

WEEK_DAYS = 7
MONTH_DAYS = 30


@dataclass(frozen=True)
class Period:
    name: str
    days: int            # how far back it looks
    refresh_days: int    # rewritten at most this often, even when a new day arrives
    paragraphs: str
    max_points: int      # turning points listed
    max_tokens: int


# One new day barely changes a 7- or 30-day story, so these are not rewritten every day.
PERIODS = (
    Period("week", WEEK_DAYS, refresh_days=2, paragraphs="two", max_points=4, max_tokens=1500),
    Period("month", MONTH_DAYS, refresh_days=7, paragraphs="three", max_points=6, max_tokens=2000),
)
# Discussions older than this are archived but never sent for summarizing, which caps cost
# if the API key is missing for a long stretch and then added.
SUMMARY_WINDOW_DAYS = 35


@dataclass(frozen=True)
class Paths:
    """Everything the site serves lives under docs/, so archived discussions have public URLs."""

    root: Path

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def archive(self) -> Path:
        return self.docs / "archive"

    @property
    def data(self) -> Path:
        return self.docs / "data"

    @property
    def discussion_summaries(self) -> Path:
        return self.data / "discussions"

    @property
    def day_summaries(self) -> Path:
        return self.data / "days"


DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# Forecasters (see afd/forecasters.py). Anyone who led at least REGULAR_MIN discussions gets a
# profile card. Colors are pinned to IDs so they never shift as people come and go: slots 1-8
# of the validated palette in style.css (--fc-1 ... --fc-8). Others are shown in gray.
REGULAR_MIN = 15
FORECASTER_COLORS = {"Felton": 1, "27": 2, "33": 3, "14": 4, "21": 5, "18": 6, "29": 7, "HPR": 8}
