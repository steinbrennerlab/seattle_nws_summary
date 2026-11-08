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

MODEL = os.environ.get("AFD_MODEL", "claude-sonnet-5-5")

WEEK_DAYS = 7
MONTH_DAYS = 30
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
