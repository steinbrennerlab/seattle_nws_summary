"""Usage:
  python -m afd backfill --days 35   load past discussions from the Iowa Environmental Mesonet archive
  python -m afd update [--batch N]   daily job: fetch new discussions, summarize, rebuild the page
  python -m afd summarize [--batch N]  fill in missing summaries only
  python -m afd render               rebuild the page only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fetch, parse, render, store, summarize
from .config import DEFAULT_ROOT, LOCAL_TZ, Paths


def save_all(paths: Paths, raw_texts: list[str]) -> list[str]:
    seen = store.known_fingerprints(paths)
    saved = []
    for raw in raw_texts:
        for product in parse.split_products(raw):
            try:
                pid = store.save(paths, product, seen)
            except ValueError as e:
                print(f"skipped a product: {e}", file=sys.stderr)
                continue
            if pid:
                saved.append(pid)
    return saved


def fetch_new(paths: Paths) -> list[str]:
    existing = store.list_all(paths)
    since = existing[-1].issued - timedelta(hours=1) if existing else datetime.now(timezone.utc) - timedelta(days=3)
    s = fetch.session()
    try:
        return save_all(paths, fetch.nws_recent(s, since))
    except Exception as e:
        print(f"NWS API failed ({e}); trying the Iowa Environmental Mesonet archive", file=sys.stderr)
        today = datetime.now(LOCAL_TZ).date()
        return save_all(paths, fetch.iem_range(s, today - timedelta(days=3), today))


MORE_TO_DO = 3  # exit code: this batch finished, and more discussions are waiting for the next one


def run_summaries(paths: Paths, status: dict, limit: int | None) -> int:
    """Run one batch, record progress in `status`, and return the exit code."""
    if not summarize.available():
        print("ANTHROPIC_API_KEY is not set; skipping summaries")
        status.update(summaries_enabled=False, summary_errors=0)
        return 0
    p = summarize.run(paths, summarize.AnthropicLLM(), limit=limit)
    for e in p.errors:
        print(f"summary error: {e}", file=sys.stderr)
    status.update(summaries_enabled=True, summary_errors=len(p.errors),
                  summaries_done=p.done, summaries_total=p.total)
    line = (f"Explained {p.made} discussions in this batch; {p.done} of {p.total} done, "
            f"{p.deferred} waiting, {len(p.errors)} errors.")
    print(line)
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):  # shows on the Actions run page
        with open(summary_file, "a") as f:
            f.write(f"- {datetime.now(timezone.utc):%H:%M}Z: {line}\n")
    if not p.finished:
        return MORE_TO_DO
    return 1 if p.errors else 0


def write_status(paths: Paths, status: dict) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    (paths.data / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def read_status(paths: Paths) -> dict:
    p = paths.data / "status.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="afd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: this checkout)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=35)
    batch_help = (f"summarize at most N discussions, then stop (exit code {MORE_TO_DO} if more are waiting); "
                  "0 = no limit")
    sub.add_parser("update").add_argument("--batch", type=int, default=0, metavar="N", help=batch_help)
    sub.add_parser("summarize").add_argument("--batch", type=int, default=0, metavar="N", help=batch_help)
    sub.add_parser("render")
    args = ap.parse_args(argv)
    limit = getattr(args, "batch", 0) or None
    paths = Paths(args.root)

    if args.cmd == "backfill":
        today = datetime.now(LOCAL_TZ).date()
        saved = save_all(paths, fetch.iem_range(fetch.session(), today - timedelta(days=args.days), today))
        print(f"backfill saved {len(saved)} discussions")
        return 0

    if args.cmd == "summarize":
        status = read_status(paths)
        code = run_summaries(paths, status, limit)
        write_status(paths, status)
        render.build(paths, status)
        return code

    if args.cmd == "render":
        render.build(paths, read_status(paths))
        return 0

    # update
    status = {"last_check": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        new = fetch_new(paths)
        print(f"saved {len(new)} new discussions: {', '.join(new) or 'none'}")
        status.update(ok=True, new_discussions=len(new))
    except Exception as e:
        # Record the failure on the page, so readers see it instead of a quietly stale date.
        status.update(ok=False, message=f"could not reach the NWS API or the IEM archive ({type(e).__name__}).")
        write_status(paths, status)
        render.build(paths, status)
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    code = run_summaries(paths, status, limit)
    write_status(paths, status)
    render.build(paths, status)
    return code


if __name__ == "__main__":
    sys.exit(main())
