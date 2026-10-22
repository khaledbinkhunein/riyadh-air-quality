"""Daily air-quality pipeline for Riyadh and four other Gulf cities.

Fetches hourly PM10, PM2.5 and dust from the Open-Meteo Air Quality API
(CAMS global model, ~45 km grid), aggregates to Riyadh-time calendar days,
and keeps two files up to date:

  data/daily.csv    one row per city per complete day
  data/summary.csv  one row of headline numbers for the dashboard cards

Usage:
  python src/air_quality.py              # normal run: re-fetch the last 7 days
  python src/air_quality.py --backfill   # rebuild everything from FIRST_DAY

The run fails (exit code 1) if a check fails, so GitHub Actions emails the owner.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import statistics
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DAILY_CSV = ROOT / "data" / "daily.csv"
SUMMARY_CSV = ROOT / "data" / "summary.csv"

API_URL = os.environ.get("AQ_API_URL") or "https://air-quality-api.open-meteo.com/v1/air-quality"
TZ = ZoneInfo("Asia/Riyadh")

CITIES = {
    "Riyadh": (24.71, 46.68),
    "Jeddah": (21.49, 39.19),
    "Dammam": (26.43, 50.10),
    "Dubai": (25.20, 55.27),
    "Doha": (25.29, 51.53),
}

# The API's first non-null hour is 2022-08-04 03:00 Riyadh time (00:00 UTC),
# so the first complete Riyadh day is 5 Aug 2022.
FIRST_DAY = dt.date(2022, 8, 5)
REFETCH_DAYS = 7      # recent values can be revised, so re-download and overwrite these
MAX_STALENESS = 3     # newest complete day must be at most this many days old

WHO_PM10 = 45.0       # WHO 2021 24-hour guideline, µg/m³
WHO_PM25 = 15.0
DUST_DAY_PM10 = 150.0  # WHO interim target 1 for 24-hour PM10 (the least strict WHO level)

# The API stores PM10 in a format that tops out at 3,276.7 µg/m³. Above that it
# returns null; in this data that only happens during extreme dust (dust > 5,000).
# Those hours are kept at the cap, so daily means are a lower bound on those days.
PM10_CAP = 3276.7
CAP_MIN_DUST = 3000.0

# A known model outage (13-16 Dec 2022) sets every city to ~0 with dust exactly 0.
# Hours with PM10 below 5 µg/m³ and zero dust never occur otherwise in this region.
SUSPECT_PM10 = 5.0

# CAMS upgraded its global model (Cy49r1) on 12 Nov 2024. In this data Riyadh's and
# Jeddah's PM10 jump from 14 Nov 2024 while Dammam, Dubai and Doha don't, so values
# before and after are not compared as a trend. Every row is labelled with its period.
MODEL_UPGRADE = dt.date(2024, 11, 12)
PERIOD_BEFORE = "Before CAMS upgrade (to 11 Nov 2024)"
PERIOD_AFTER = "After CAMS upgrade (from 12 Nov 2024)"


def model_period(day: str) -> str:
    return PERIOD_AFTER if day >= MODEL_UPGRADE.isoformat() else PERIOD_BEFORE


def period_start(period: str) -> dt.date:
    return MODEL_UPGRADE if period == PERIOD_AFTER else FIRST_DAY


# Daily verdict: the latest day is compared with earlier days of the same calendar
# month in the same model period (never across the upgrade, never with suspect days).
MIN_COMPARISON_DAYS = 20
NOT_ENOUGH = "not enough history yet"


FIELDS = [
    "date", "city", "year", "month",
    "pm10_mean", "pm10_max", "pm25_mean", "dust_max",
    "above_who_pm10", "above_who_pm25", "dust_day",
    "pm10_capped_hours", "suspect", "model_period",
]


class CheckFailed(Exception):
    pass


# ---------------------------------------------------------------- fetching
def fetch_hourly(city: str, start: dt.date, end: dt.date, session=None, retries: int = 3) -> dict:
    """Hourly pm10, pm2_5 and dust for one city, Riyadh local time."""
    lat, lon = CITIES[city]
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "pm10,pm2_5,dust",
        "timezone": "Asia/Riyadh",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    }
    session = session or requests.Session()
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(API_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise RuntimeError(data.get("reason", "API error"))
            return data["hourly"]
        except Exception as e:  # noqa: BLE001 - retried, then re-raised
            last_error = e
            if attempt < retries:
                time.sleep(5 * attempt)
    raise RuntimeError(f"{city}: fetch {start}..{end} failed after {retries} tries: {last_error}")


# ------------------------------------------------------------- aggregation
def aggregate(city: str, hourly: dict, last_day: dt.date) -> tuple[list[dict], list[str]]:
    """Turn hourly values into one row per complete Riyadh day up to last_day.

    Returns (rows, problems). A day is dropped (and reported as a problem) if it
    doesn't have 24 hours or has a gap that isn't explained by the PM10 cap.
    """
    days: dict[str, dict[str, list]] = {}
    for i, t in enumerate(hourly["time"]):
        d = t[:10]
        if dt.date.fromisoformat(d) > last_day:
            continue
        rec = days.setdefault(d, {"pm10": [], "pm2_5": [], "dust": [], "capped": 0, "suspect": 0, "bad": 0})
        pm10, pm25, dust = hourly["pm10"][i], hourly["pm2_5"][i], hourly["dust"][i]
        if pm25 is None or dust is None:
            rec["bad"] += 1
            continue
        if pm10 is None:
            if dust >= CAP_MIN_DUST:
                pm10 = PM10_CAP
                rec["capped"] += 1
            else:
                rec["bad"] += 1
                continue
        if pm10 < SUSPECT_PM10 and dust == 0:
            rec["suspect"] += 1
        rec["pm10"].append(pm10)
        rec["pm2_5"].append(pm25)
        rec["dust"].append(dust)

    rows, problems = [], []
    for d, rec in sorted(days.items()):
        n = len(rec["pm10"])
        if n != 24 or rec["bad"]:
            problems.append(f"{city} {d}: {n} usable hours, {rec['bad']} unexplained gaps")
            continue
        pm10_mean = sum(rec["pm10"]) / n
        pm25_mean = sum(rec["pm2_5"]) / n
        suspect = rec["suspect"] > 0
        day = dt.date.fromisoformat(d)

        def flag(cond: bool) -> str:
            # Suspect days are left blank so they never count as clean (or dirty) days.
            return "" if suspect else str(int(cond))

        rows.append({
            "date": d, "city": city, "year": day.year, "month": day.month,
            "pm10_mean": round(pm10_mean, 1),
            "pm10_max": round(max(rec["pm10"]), 1),
            "pm25_mean": round(pm25_mean, 1),
            "dust_max": round(max(rec["dust"]), 1),
            "above_who_pm10": flag(pm10_mean > WHO_PM10),
            "above_who_pm25": flag(pm25_mean > WHO_PM25),
            "dust_day": flag(pm10_mean > DUST_DAY_PM10),
            "pm10_capped_hours": rec["capped"],
            "suspect": int(suspect),
        })
    return rows, problems


# ------------------------------------------------------------------ files
def read_daily(path: Path = DAILY_CSV) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    """Overwrite existing city-days with freshly fetched ones; keep the rest.

    If a re-fetched day came back unusable, the previously saved value stays.
    """
    by_key = {(r["date"], r["city"]): r for r in existing}
    for r in new:
        by_key[(r["date"], r["city"])] = {k: str(v) for k, v in r.items()}
    return sorted(by_key.values(), key=lambda r: (r["date"], list(CITIES).index(r["city"])))


def verdict_for(pct_rank: float, comparison_days: int) -> str:
    """Map a percentile rank (share of comparison days that were cleaner) to words."""
    if comparison_days < MIN_COMPARISON_DAYS:
        return NOT_ENOUGH
    if pct_rank < 0.33:
        return "cleaner than usual"
    if pct_rank < 0.67:
        return "about usual"
    if pct_rank <= 0.90:
        return "dustier than usual"
    return "much dustier than usual"


def compare_with_usual(rows: list[dict], city: str = "Riyadh") -> dict:
    """Compare the city's latest day with earlier days of the same month and model period."""
    own = [r for r in rows if r["city"] == city]
    latest = max(own, key=lambda r: r["date"])
    period = model_period(latest["date"])
    month = latest["date"][5:7]
    pool = [float(r["pm10_mean"]) for r in own
            if r["date"] < latest["date"]
            and r["date"][5:7] == month
            and model_period(r["date"]) == period
            and str(r["suspect"]) != "1"]
    value = float(latest["pm10_mean"])
    day = dt.date.fromisoformat(latest["date"])
    start = period_start(period)
    month_name = day.strftime("%B")
    head = f"{day.day} {day.strftime('%b')}"
    since = start.strftime("%b %Y")
    n = len(pool)
    typical = statistics.median(pool) if pool else None
    pct = sum(v < value for v in pool) / n if n else None
    if str(latest["suspect"]) == "1":
        verdict = "no verdict (model outage)"
        text = f"{head}: no verdict. The model data for this day looks faulty."
    else:
        verdict = verdict_for(pct if pct is not None else 0.0, n)
        if verdict == NOT_ENOUGH:
            text = (f"{head}: {verdict}. Only {n} earlier {month_name} days since {since} to compare with. "
                    f"This day read {value:,.0f} µg/m³.")
        else:
            text = (f"{head}: {verdict}. Riyadh's air was dustier than {pct:.0%} of {month_name} days "
                    f"since {since}. A typical {month_name} day reads about {round(typical, -1):,.0f} µg/m³; "
                    f"this day read {value:,.0f}.")
    return {
        "typical_pm10": round(typical, 1) if typical is not None else "",
        "comparison_days": n,
        "pct_rank": round(pct, 3) if pct is not None else "",
        "verdict": verdict,
        "perspective_text": text,
    }


def build_summary(rows: list[dict]) -> dict:
    """Headline numbers for the dashboard cards (Riyadh, latest complete day and year to date)."""
    riyadh = [r for r in rows if r["city"] == "Riyadh"]
    latest = max(riyadh, key=lambda r: r["date"])
    year = latest["date"][:4]
    ytd = [r for r in riyadh if r["date"].startswith(year) and r["suspect"] != "1"]
    return {
        "latest_date": latest["date"],
        "riyadh_pm10_mean": latest["pm10_mean"],
        "riyadh_pm25_mean": latest["pm25_mean"],
        "who_pm10_limit": WHO_PM10,
        "who_pm25_limit": WHO_PM25,
        "pm10_times_who": round(float(latest["pm10_mean"]) / WHO_PM10, 1),
        "pm25_times_who": round(float(latest["pm25_mean"]) / WHO_PM25, 1),
        "year": year,
        "days_in_year": len(ytd),
        "days_above_who_pm10_ytd": sum(r["above_who_pm10"] == "1" for r in ytd),
        "days_above_who_pm25_ytd": sum(r["above_who_pm25"] == "1" for r in ytd),
        "dust_days_ytd": sum(r["dust_day"] == "1" for r in ytd),
        **compare_with_usual(rows),
    }


# ----------------------------------------------------------------- checks
def check(rows: list[dict], today: dt.date) -> list[str]:
    """Raise CheckFailed on hard problems; return a list of warnings."""
    errors, warnings = [], []
    newest = max(dt.date.fromisoformat(r["date"]) for r in rows)
    have = {(r["date"], r["city"]) for r in rows}
    if len(have) != len(rows):
        errors.append(f"{len(rows) - len(have)} duplicate city-days")
    missing = []
    d = FIRST_DAY
    while d <= newest:
        for c in CITIES:
            if (d.isoformat(), c) not in have:
                missing.append(f"{c} {d}")
        d += dt.timedelta(days=1)
    if missing:
        errors.append(f"{len(missing)} missing city-days, e.g. {missing[:5]}")
    num = ["pm10_mean", "pm10_max", "pm25_mean", "dust_max"]
    neg = [f"{r['city']} {r['date']}" for r in rows if any(float(r[k]) < 0 for k in num)]
    if neg:
        errors.append(f"negative values on {len(neg)} city-days, e.g. {neg[:5]}")
    if any(not math.isfinite(float(r[k])) for r in rows for k in num):
        errors.append("non-numeric values found")
    age = (today - newest).days
    if age > MAX_STALENESS:
        errors.append(f"data is stale: newest complete day is {newest}, {age} days old")
    for r in rows:
        if float(r["pm25_mean"]) > float(r["pm10_mean"]):
            warnings.append(f"PM2.5 mean above PM10 mean: {r['city']} {r['date']}")
    if errors:
        raise CheckFailed("; ".join(errors))
    return warnings


# ------------------------------------------------------------------- main
def run(backfill: bool = False, today: dt.date | None = None, fetch=fetch_hourly,
        daily_path: Path = DAILY_CSV, summary_path: Path = SUMMARY_CSV) -> dict:
    today = today or dt.datetime.now(TZ).date()
    yesterday = today - dt.timedelta(days=1)
    existing = [] if backfill else read_daily(daily_path)

    if existing:
        last_saved = max(dt.date.fromisoformat(r["date"]) for r in existing)
        start = min(last_saved + dt.timedelta(days=1), yesterday - dt.timedelta(days=REFETCH_DAYS - 1))
    else:
        start = FIRST_DAY
    start = max(start, FIRST_DAY)

    new_rows, problems = [], []
    for city in CITIES:
        hourly = fetch(city, start, yesterday)
        rows, probs = aggregate(city, hourly, yesterday)
        new_rows += rows
        problems += probs

    rows = merge(existing, new_rows)
    for r in rows:
        r["model_period"] = model_period(r["date"])
    warnings = check(rows, today)          # raises on missing days, negatives, staleness
    write_csv(daily_path, rows, FIELDS)
    summary = build_summary(rows)
    write_csv(summary_path, [summary], list(summary))
    return {"start": start, "end": yesterday, "rows": len(rows), "new_rows": len(new_rows),
            "problems": problems, "warnings": warnings, "summary": summary}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backfill", action="store_true", help=f"rebuild all data from {FIRST_DAY}")
    args = ap.parse_args()
    try:
        out = run(backfill=args.backfill)
    except CheckFailed as e:
        print(f"::error::Data check failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"::error::Run failed: {e}")
        return 1
    for p in out["problems"]:
        print(f"::warning::Day skipped: {p}")
    for w in out["warnings"][:20]:
        print(f"::warning::{w}")
    s = out["summary"]
    print(f"Fetched {out['start']}..{out['end']} ({out['new_rows']} city-days); "
          f"daily.csv now has {out['rows']} rows through {s['latest_date']}.")
    print(f"Riyadh {s['latest_date']}: PM10 {s['riyadh_pm10_mean']} µg/m³ ({s['pm10_times_who']}x WHO), "
          f"PM2.5 {s['riyadh_pm25_mean']} µg/m³ ({s['pm25_times_who']}x WHO).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
