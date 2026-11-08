import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afd import parse, render, store, summarize
from afd.__main__ import save_all
from afd.config import Paths

FIXTURE = (Path(__file__).parent / "fixtures" / "iem_sample.txt").read_text()


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path)


def test_split_finds_both_products_and_strips_sequence_numbers():
    products = parse.split_products(FIXTURE)
    assert len(products) == 2
    assert all(p.startswith("FXUS66 KSEW") for p in products)
    assert not products[0].rstrip().endswith("512")


@pytest.mark.parametrize("line, expected", [
    ("245 PM PDT Sat Oct 31 2026", datetime(2026, 10, 31, 21, 45, tzinfo=timezone.utc)),
    ("300 AM PST Mon Nov 2 2026", datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)),
    ("1200 AM PST Tue Nov 3 2026", datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)),
    ("1200 PM PST Tue Nov 3 2026", datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)),
    ("1015 PM PST Thu Dec 31 2026", datetime(2027, 1, 1, 6, 15, tzinfo=timezone.utc)),
])
def test_issuance_time(line, expected):
    assert parse.issuance_utc(f"AFDSEW\n\n{line}\n") == expected


def test_sections_and_priority():
    secs = parse.sections(parse.split_products(FIXTURE)[0])
    names = [n for n, _ in secs]
    assert names[:3] == ["SYNOPSIS", "SHORT TERM /TONIGHT THROUGH MONDAY/", "LONG TERM /TUESDAY THROUGH SATURDAY/"]
    assert "AVIATION" in names and parse.is_lower_priority("AVIATION")
    body = summarize.discussion_body(parse.split_products(FIXTURE)[0])
    assert body.index("[Lower priority") < body.index(".AVIATION") < body.index(".MARINE")


def test_save_deduplicates_across_sources(paths):
    assert len(save_all(paths, [FIXTURE])) == 2
    # The same product again, as the NWS API would return it (different whitespace, no control chars).
    again = parse.split_products(FIXTURE)[1].replace("\n\n", "\n\n\n")
    assert save_all(paths, [again]) == []
    ids = [s.id for s in store.list_all(paths)]
    assert ids == ["AFDSEW_202610312145", "AFDSEW_202611021100"]
    assert (paths.archive / "2026" / "11" / "AFDSEW_202611021100.txt").exists()


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, system, user, max_tokens):
        self.calls += 1
        if user.startswith("Explain this discussion"):
            return "```json\n" + json.dumps({
                "headline": "Dry weekend, then a wet and windy week.",
                "summary": "First paragraph.\n\nSecond paragraph.",
                "timeline": [{"when": "Tonight", "what": "Fog."}],
                "confidence": "Rain amounts are uncertain.",
                "hazards": ["Wind advisory on the coast"],
                "glossary": [{"term": "Atmospheric river", "definition": "A long band of moist air."}],
            }) + "\n```"
        if "issued on" in user:
            return json.dumps({"headline": "Day headline.", "summary": "Day summary.", "changes": ""})
        return json.dumps({"headline": "Period headline.", "summary": "One.\n\nTwo.",
                           "turning_points": [{"date": "Mon Nov 2", "what": "Flood risk added."}]})


def test_summaries_are_cached(paths):
    save_all(paths, [FIXTURE])
    now = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
    llm = FakeLLM()
    assert summarize.run(paths, llm, now=now, log=lambda *_: None) == []
    # 2 discussions + 2 days + week + month
    assert llm.calls == 6
    assert (paths.data / "week.json").exists() and (paths.data / "month.json").exists()
    summarize.run(paths, llm, now=now, log=lambda *_: None)
    assert llm.calls == 6, "a second run with nothing new should make no requests"


def test_render_without_and_with_summaries(paths):
    save_all(paths, [FIXTURE])
    status = {"last_check": "2026-11-02T23:30:00+00:00", "ok": True}
    render.build(paths, status)
    html = (paths.docs / "index.html").read_text()
    assert "has not been generated yet" in html
    assert 'href="archive/2026/11/AFDSEW_202611021100.txt"' in html
    assert "Checked Mon Nov 2, 3:30 PM PST" in html

    summarize.run(paths, FakeLLM(), now=datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc), log=lambda *_: None)
    render.build(paths, status)
    html = (paths.docs / "index.html").read_text()
    assert "Dry weekend, then a wet and windy week." in html
    assert "Wind advisory on the coast" in html
    assert (paths.docs / "archive.html").exists() and (paths.docs / "style.css").exists()


def test_failed_check_is_shown(paths):
    render.build(paths, {"last_check": "2026-11-02T23:30:00+00:00", "ok": False,
                         "message": "could not reach the NWS API or the IEM archive (ConnectionError)."})
    assert "failed: could not reach" in (paths.docs / "index.html").read_text()
