"""Two sources.

api.weather.gov: the official NWS API. Used for the daily update. It only keeps recent products.
Iowa Environmental Mesonet (IEM): a long-running archive of NWS text products. Used for the
initial backfill, and as a fallback if the NWS API is down.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import requests

from .config import OFFICE, PIL, USER_AGENT

NWS_LIST = f"https://api.weather.gov/products/types/AFD/locations/{OFFICE}"
NWS_PRODUCT = "https://api.weather.gov/products/{id}"
IEM_RETRIEVE = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(s: requests.Session, url: str, *, params=None, attempts: int = 4) -> requests.Response:
    for i in range(attempts):
        try:
            r = s.get(url, params=params, timeout=60)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r
            err: Exception = requests.HTTPError(f"{r.status_code} from {r.url}")
        except (requests.ConnectionError, requests.Timeout) as e:
            err = e
        if i < attempts - 1:
            time.sleep(5 * 2**i)
    raise err


def nws_recent(s: requests.Session, since: datetime | None) -> list[str]:
    """Texts of discussions issued after `since` (UTC), oldest first."""
    graph = _get(s, NWS_LIST).json().get("@graph", [])
    texts = []
    for item in sorted(graph, key=lambda g: g["issuanceTime"]):
        issued = datetime.fromisoformat(item["issuanceTime"])
        if since is None or issued > since:
            texts.append(_get(s, NWS_PRODUCT.format(id=item["id"])).json()["productText"])
    return texts


def iem_range(s: requests.Session, start: date, end: date, chunk_days: int = 7) -> list[str]:
    """Raw IEM text for [start, end], requested a week at a time to keep each response small.

    Each response holds many products concatenated; parse.split_products separates them.
    """
    out = []
    day = start
    while day <= end:
        stop = min(day + timedelta(days=chunk_days), end + timedelta(days=1))
        r = _get(s, IEM_RETRIEVE, params={
            "pil": PIL, "fmt": "text", "order": "asc", "limit": 9999,
            "sdate": day.isoformat(), "edate": stop.isoformat(),
        })
        out.append(r.text)
        day = stop
        time.sleep(1)  # be polite to a university-run service
    return out
