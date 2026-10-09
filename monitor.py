#!/usr/bin/env python3
"""
Netflix weekly Top 10 monitor -> ntfy push notifications.

Normal mode : on Tuesdays between 10:30 and 16:00 Eastern Time, check Netflix's
              official TSV about once a minute. When a new week appears, send
              one notification per category (4 total), then stop.
--test      : fetch once, send the latest week right now (ignores the schedule).
--dry-run   : print messages instead of sending them (nothing is saved).
--file X    : read a local TSV file instead of downloading (for testing).
"""
import argparse
import csv
import io
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DATA_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv"
PAGE_URL = "https://www.netflix.com/tudum/top10"
ET = ZoneInfo("America/New_York")
WINDOW_START = dtime(10, 30)
WINDOW_END = dtime(16, 0)
POLL_SECONDS = 60
MAX_EARLY_WAIT = 45 * 60          # if started >45 min before 10:30, just exit
CATEGORIES = ["Films (English)", "Films (Non-English)",
              "TV (English)", "TV (Non-English)"]
STATE_FILE = Path(__file__).with_name("state.json")
USER_AGENT = "netflix-top10-personal-monitor/1.0 (personal use, 1 request/min)"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


# ---------------------------------------------------------------- data ----
@dataclass
class Row:
    week: date
    category: str
    rank: int
    show: str
    season: str
    views: int
    cum_weeks: int

    @property
    def display(self):
        return self.season if self.season not in ("", "N/A") else self.show

    @property
    def key(self):
        return (self.category, self.show, self.season)


class FetchError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class DataError(Exception):
    pass


REQUIRED_COLUMNS = {"week", "category", "weekly_rank", "show_title",
                    "season_title", "weekly_views", "cumulative_weeks_in_top_10"}


def parse_tsv(text):
    reader = csv.DictReader(io.StringIO(text), delimiter="\t",
                            quoting=csv.QUOTE_NONE)
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise DataError(f"Netflix changed the file; missing columns: {sorted(missing)}")
    rows = []
    for r in reader:
        try:
            rows.append(Row(
                week=date.fromisoformat(r["week"]),
                category=r["category"],
                rank=int(r["weekly_rank"]),
                show=r["show_title"],
                season=r["season_title"],
                views=int(r["weekly_views"]),
                cum_weeks=int(r["cumulative_weeks_in_top_10"]),
            ))
        except (ValueError, TypeError, KeyError):
            continue  # skip malformed or cut-off lines
    if len(rows) < 100:
        raise DataError(f"File looks incomplete ({len(rows)} rows)")
    return rows


class Fetcher:
    """Downloads the TSV politely (conditional requests, honest User-Agent)."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.etag = self.last_mod = self.text = None

    def get(self):
        h = {}
        if self.etag and self.text:
            h["If-None-Match"] = self.etag
        if self.last_mod and self.text:
            h["If-Modified-Since"] = self.last_mod
        try:
            r = self.s.get(DATA_URL, headers=h, timeout=30)
        except requests.RequestException as e:
            raise FetchError(f"network error: {e}")
        if r.status_code == 304 and self.text:
            return self.text
        if r.status_code != 200:
            ra = r.headers.get("Retry-After", "")
            raise FetchError(f"HTTP {r.status_code}",
                             int(ra) if ra.isdigit() else None)
        self.etag = r.headers.get("ETag")
        self.last_mod = r.headers.get("Last-Modified")
        self.text = r.content.decode("utf-8-sig")
        return self.text


# ------------------------------------------------------------ analysis ----
def compare(rows, week):
    """Return {category: [dict, ...]} with previous-week comparison."""
    prev_week = week - timedelta(days=7)
    prev = {r.key: r.views for r in rows if r.week == prev_week}
    out = {}
    for cat in CATEGORIES:
        items = sorted((r for r in rows if r.week == week and r.category == cat),
                       key=lambda r: r.rank)
        lst = []
        for r in items:
            p = prev.get(r.key)
            if p:
                pct = (r.views - p) / p * 100
                status = f"{'▲' if pct > 0 else '▼' if pct < 0 else '■'} {pct:+.1f}%"
            else:
                pct = None
                # first week in Top 10 => NEW; otherwise no usable prior number
                status = "NEW" if r.cum_weeks == 1 else "N/A"
            lst.append({"rank": r.rank, "title": r.display, "views": r.views,
                        "prev": p, "pct": pct, "status": status})
        out[cat] = lst
    return out


def week_label(week):
    start = week - timedelta(days=6)
    return f"{start:%b} {start.day} - {week:%b} {week.day}, {week.year}"


def format_message(week, items):
    lines = [f"Week: {week_label(week)}"]
    for it in items:
        prev = f"{it['prev']:,}" if it["prev"] else "none"
        lines.append(f"{it['rank']}. {it['title']}\n"
                     f"   {it['views']:,} views | prev {prev} | {it['status']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- ntfy ----
def send_ntfy(topic, title, body, tags="tv", priority="default"):
    headers = {"Title": title, "Tags": tags, "Priority": priority,
               "Click": PAGE_URL}                      # ASCII only in headers!
    last = None
    for attempt in range(3):
        try:
            r = requests.post(f"{NTFY_SERVER}/{topic}", data=body.encode("utf-8"),
                              headers=headers, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "10")) if
                           r.headers.get("Retry-After", "").isdigit() else 10)
                continue
            r.raise_for_status()
            return
        except requests.RequestException as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"ntfy failed: {last}")


# --------------------------------------------------------------- state ----
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def notify_week(rows, week, topic, state, dry_run=False, test=False):
    """Send one message per category; never re-send a category twice."""
    groups = compare(rows, week)
    prog = state.get("in_progress", {})
    already = prog.get("sent", []) if prog.get("week") == str(week) and not test else []
    for cat in CATEGORIES:
        if cat in already:
            continue
        items = groups[cat]
        if not items:
            continue
        title = f"{'TEST - ' if test else ''}Netflix Top 10 - {cat}"
        body = format_message(week, items)
        if dry_run:
            print(f"\n=== {title} ===\n{body}")
        else:
            send_ntfy(topic, title, body)
            time.sleep(2)                               # be gentle with ntfy
            if not test:
                already.append(cat)
                state["in_progress"] = {"week": str(week), "sent": already}
                save_state(state)
    if not test and not dry_run:
        state["last_notified_week"] = str(week)
        state.pop("in_progress", None)
        save_state(state)


# ------------------------------------------------------------- running ----
def now_et():
    return datetime.now(ET)


def in_window(now):
    return now.weekday() == 1 and WINDOW_START <= now.time() < WINDOW_END


def log(msg):
    print(f"[{now_et():%Y-%m-%d %H:%M:%S} ET] {msg}", flush=True)


def get_rows(fetcher, local_file):
    if local_file:
        return parse_tsv(Path(local_file).read_text(encoding="utf-8-sig"))
    return parse_tsv(fetcher.get())


def run_test(args, topic):
    rows = get_rows(Fetcher(), args.file)
    week = max(r.week for r in rows)
    log(f"TEST: latest week in file is {week}")
    state = load_state()
    notify_week(rows, week, topic, state, dry_run=args.dry_run, test=True)
    if not state.get("last_notified_week") and not args.dry_run:
        state["last_notified_week"] = str(week)        # set baseline
        save_state(state)
        log("Baseline saved (no earlier state existed).")


def run_monitor(topic):
    now = now_et()
    if now.weekday() != 1:
        log("Not Tuesday - nothing to do.")
        return
    start = now.replace(hour=10, minute=30, second=0, microsecond=0)
    wait = (start - now).total_seconds()
    if wait > MAX_EARLY_WAIT:
        log("Too early for the 10:30 AM ET window - exiting.")
        return
    if wait > 0:
        log(f"Waiting {int(wait)}s for the window to open...")
        time.sleep(wait)

    fetcher, state = Fetcher(), load_state()
    failures, alerted = 0, False
    while in_window(now_et()):
        delay = POLL_SECONDS
        try:
            rows = parse_tsv(fetcher.get())
            latest = max(r.week for r in rows)
            last = state.get("last_notified_week")
            if last is None:
                state["last_notified_week"] = str(latest)
                save_state(state)
                log(f"First run: baseline set to week {latest} (no notification).")
            elif str(latest) > last or state.get("in_progress"):
                log(f"New week detected: {latest}")
                notify_week(rows, latest, topic, state)
                log("Notifications sent. Done for this week.")
                return
            else:
                log(f"No new week yet (latest = {latest}).")
            failures, alerted = 0, False
        except (FetchError, DataError, RuntimeError) as e:
            failures += 1
            delay = getattr(e, "retry_after", None) or min(60 * 2 ** failures, 900)
            log(f"Problem #{failures}: {e}. Backing off {delay}s.")
            if failures >= 5 and not alerted:
                try:
                    send_ntfy(topic, "Netflix monitor trouble",
                              f"{failures} failures in a row. Last: {e}",
                              tags="warning", priority="low")
                    alerted = True
                except RuntimeError:
                    pass
        end = now_et().replace(hour=16, minute=0, second=0, microsecond=0)
        left = (end - now_et()).total_seconds()
        time.sleep(max(0, min(delay + random.uniform(0, 5), left)))
    log("Window closed (4:00 PM ET). Exiting.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--file")
    args = ap.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and not args.dry_run:
        sys.exit("ERROR: set the NTFY_TOPIC environment variable (your secret topic name).")

    if args.test or args.dry_run or args.file:
        run_test(args, topic)
    else:
        run_monitor(topic)


if __name__ == "__main__":
    main()
