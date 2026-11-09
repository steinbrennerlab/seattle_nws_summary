import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afd import parse, render, store, summarize
from afd.__main__ import save_all
from afd.config import Paths

FIXTURE = (Path(__file__).parent / "fixtures" / "iem_sample.txt").read_text(encoding="utf-8")


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
                "timeline": [{"when": "Tonight", "what": "Fog.", "source": "short_term"}],
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
    p = summarize.run(paths, llm, now=now, log=lambda *_: None)
    assert p.errors == [] and p.finished and (p.done, p.total) == (2, 2)
    # 2 discussions + 2 days + week + month
    assert llm.calls == 6
    assert (paths.data / "week.json").exists() and (paths.data / "month.json").exists()
    summarize.run(paths, llm, now=now, log=lambda *_: None)
    assert llm.calls == 6, "a second run with nothing new should make no requests"


def test_batches_defer_the_rest_and_hold_back_week_and_month(paths):
    save_all(paths, [FIXTURE])
    now = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
    llm = FakeLLM()
    p = summarize.run(paths, llm, now=now, log=lambda *_: None, limit=1)
    assert (p.made, p.done, p.total, p.deferred) == (1, 1, 2, 1) and not p.finished
    # 1 discussion + its (complete) day; the other day and the week and month wait
    assert llm.calls == 2
    assert (paths.day_summaries / "2026-10-31.json").exists()
    assert not (paths.day_summaries / "2026-11-02.json").exists()
    assert not (paths.data / "week.json").exists()

    p = summarize.run(paths, llm, now=now, log=lambda *_: None, limit=1)
    assert p.finished and (p.made, p.done) == (1, 2)
    assert llm.calls == 2 + 4  # second discussion + its day + week + month
    assert (paths.data / "week.json").exists()


class FailingLLM(FakeLLM):
    """Fails on the first discussion of the fixture (the Oct 31 one)."""

    def complete(self, system, user, max_tokens):
        if user.startswith("Explain this discussion") and "Oct 31" in user:
            self.calls += 1
            return "not json"
        return super().complete(system, user, max_tokens)


def test_failures_count_toward_the_batch_and_wait_before_retry(paths):
    save_all(paths, [FIXTURE])
    now = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
    llm = FailingLLM()
    p = summarize.run(paths, llm, now=now, log=lambda *_: None, limit=1)
    assert (p.made, p.failing, p.deferred) == (0, 1, 1) and len(p.errors) == 1
    assert "AFDSEW_202610312145" in json.loads((paths.data / "failures.json").read_text(encoding="utf-8"))

    # The next batch skips the recent failure and moves on.
    p = summarize.run(paths, llm, now=now, log=lambda *_: None, limit=1)
    assert (p.made, p.failing, p.deferred, p.errors) == (1, 1, 0, [])

    # Twelve hours later it is tried again; on success it leaves the failure list.
    later = now + summarize.RETRY_FAILED_AFTER
    p = summarize.run(paths, FakeLLM(), now=later, log=lambda *_: None, limit=1)
    assert (p.made, p.failing, p.done) == (1, 0, 2)
    assert json.loads((paths.data / "failures.json").read_text(encoding="utf-8")) == {}


def test_json_with_stray_text_and_a_cut_off_reply():
    class Chatty:
        def complete(self, system, user, max_tokens):
            return 'Here is the explanation:\n{"headline": "Rain."}\nHope this helps.'

    assert summarize.ask_json(Chatty(), "x") == {"headline": "Rain."}

    class CutOffOnce:
        budgets = []

        def complete(self, system, user, max_tokens):
            self.budgets.append(max_tokens)
            if len(self.budgets) == 1:
                raise summarize.CutOff('{"headline": "Ra')
            return '{"headline": "Rain."}'

    llm = CutOffOnce()
    assert summarize.ask_json(llm, "x", 1000) == {"headline": "Rain."}
    assert llm.budgets == [1000, 2000]


def test_period_model_and_refresh_intervals(paths):
    from datetime import timedelta
    save_all(paths, [FIXTURE])
    now = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
    cheap, big = FakeLLM(), FakeLLM()
    summarize.run(paths, cheap, now=now, log=lambda *_: None, period_llm=big)
    assert (cheap.calls, big.calls) == (4, 2)  # 2 discussions + 2 days; week + month
    week = json.loads((paths.data / "week.json").read_text(encoding="utf-8"))
    assert week["written"] == "2026-11-02"

    def new_day_then_run(days_later):
        # A new day arrives: same fixture text, shifted forward, so the week and month are out of date.
        text = parse.split_products(FIXTURE)[1].replace("Nov 2 2026", f"Nov {2 + days_later} 2026") \
                                               .replace("Mon Nov", "XXX Nov")
        save_all(paths, [text])
        before = big.calls
        summarize.run(paths, cheap, now=now + timedelta(days=days_later), log=lambda *_: None,
                      period_llm=big)
        return big.calls - before

    assert new_day_then_run(1) == 0   # week 1 day old, month 1 day old: both wait
    assert new_day_then_run(2) == 1   # week is 2 days old: rewritten; month waits
    assert new_day_then_run(7) == 2   # both due


@pytest.mark.parametrize("headline, kind", [
    ("Strong cold front brings damaging wind and heavy rain tonight.", "storm"),
    ("Breezy and showery Monday, drier Tuesday.", "wind"),
    ("Steady rain through Thursday, heaviest in the Olympics.", "rain"),
    ("Morning fog and low clouds, then afternoon sun.", "fog"),
    ("Dry and mild under high pressure through the weekend.", "sun"),
    ("Mostly cloudy and seasonable.", "cloud"),
])
def test_sky_kind(headline, kind):
    assert render.sky_kind(headline) == kind


def test_strip_and_scene(paths):
    save_all(paths, [FIXTURE])
    summarize.run(paths, FakeLLM(), now=datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc), log=lambda *_: None)
    render.build(paths, {"last_check": "2026-11-02T23:30:00+00:00", "ok": True})
    html = (paths.docs / "index.html").read_text(encoding="utf-8")
    assert 'data-sky="wind"' in html          # fake headline + "Wind advisory on the coast"
    assert html.count('<li class="k-') >= 30  # one strip tile per day (plus the legend)
    assert 'id="d-2026-11-02"' in html


def test_progress_is_shown_while_incomplete(paths):
    save_all(paths, [FIXTURE])
    render.build(paths, {"last_check": "2026-11-02T23:30:00+00:00", "ok": True,
                         "summaries_done": 1, "summaries_total": 2})
    assert "1 of 2 discussions" in (paths.docs / "index.html").read_text(encoding="utf-8")


def test_render_without_and_with_summaries(paths):
    save_all(paths, [FIXTURE])
    status = {"last_check": "2026-11-02T23:30:00+00:00", "ok": True}
    render.build(paths, status)
    html = (paths.docs / "index.html").read_text(encoding="utf-8")
    assert "has not been generated yet" in html
    assert 'href="archive/2026/11/AFDSEW_202611021100.txt"' in html
    assert "Checked Mon Nov 2, 3:30 PM PST" in html

    summarize.run(paths, FakeLLM(), now=datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc), log=lambda *_: None)
    render.build(paths, status)
    html = (paths.docs / "index.html").read_text(encoding="utf-8")
    assert "Dry weekend, then a wet and windy week." in html
    assert "Wind advisory on the coast" in html
    assert (paths.docs / "archive.html").exists() and (paths.docs / "style.css").exists()


def test_failed_check_is_shown(paths):
    render.build(paths, {"last_check": "2026-11-02T23:30:00+00:00", "ok": False,
                         "message": "could not reach the NWS API or the IEM archive (ConnectionError)."})
    assert "failed: could not reach" in (paths.docs / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("body, expected", [
    ("Rain tonight.\n\n27", ["27"]),
    ("Rain tonight.\n\n33/41", ["33", "41"]),
    ("Rain tonight.\n\nHPR", ["HPR"]),
    ("Rain tonight.\n\n14/Mazurkiewicz", ["14", "Mazurkiewicz"]),
    ("since 2022 and 2020. Felton", ["Felton"]),
    ("peaking this evening. JD", ["JD"]),
    ("No changes needed.  18\n\nFrom Previous Discussion...\nOlder text.", ["18"]),
    ("looms offshore. The\nprevious discussion remains below.", []),
    ("Showers ease by 06Z this evening.", []),
])
def test_section_signature(body, expected):
    assert parse.section_signature(body) == expected


def test_desk_signatures_and_byline():
    text = """.SHORT TERM /TODAY THROUGH MONDAY/...
Sunny.

&&

.LONG TERM /TUESDAY THROUGH FRIDAY/...
Rain returns.

15

&&

.AVIATION...
VFR.

33/41

&&

.MARINE...
Calm.

41

&&
"""
    desks = parse.desk_signatures(text)
    assert desks == {"short_term": ["15"], "long_term": ["15"], "aviation": ["33", "41"], "marine": ["41"]}
    assert render.byline(desks) == [("Forecast", ["15"]), ("Aviation", ["33", "41"]), ("Marine", ["41"])]


def test_forecaster_pills_and_page(paths):
    save_all(paths, [FIXTURE])
    summarize.run(paths, FakeLLM(), now=datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc), log=lambda *_: None)
    render.build(paths, {"last_check": "2026-11-02T23:30:00+00:00", "ok": True})
    assert (paths.docs / "forecasters.html").exists()
    assert 'href="forecasters.html"' in (paths.docs / "index.html").read_text(encoding="utf-8")
