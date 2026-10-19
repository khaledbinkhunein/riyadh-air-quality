"""Tests run against real Open-Meteo responses saved in tests/fixtures/."""
import copy
import datetime as dt
import json
from pathlib import Path

import pytest

import air_quality as aq

FIX = json.loads((Path(__file__).parent / "fixtures" / "open_meteo_samples.json").read_text())


def hourly(name):
    """Rebuild an API-style hourly dict (with timestamps) from a saved sample."""
    s = FIX[name]
    start = dt.datetime.fromisoformat(s["start"])
    n = len(s["pm10"])
    return {
        "time": [(start + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)],
        "pm10": list(s["pm10"]), "pm2_5": list(s["pm2_5"]), "dust": list(s["dust"]),
    }


def by_date(rows):
    return {r["date"]: r for r in rows}


# ------------------------------------------------------------ aggregation
def test_daily_mean_matches_hand_calculation():
    h = hourly("Riyadh_2026-10-10_18")
    rows, problems = aq.aggregate("Riyadh", h, dt.date(2026, 10, 17))
    day = by_date(rows)["2026-10-17"]
    hours = [v for t, v in zip(h["time"], h["pm10"]) if t.startswith("2026-10-17")]
    assert len(hours) == 24
    assert day["pm10_mean"] == round(sum(hours) / 24, 1)
    assert day["pm10_max"] == max(hours)
    assert not problems


def test_known_dust_event_on_17_oct_2026():
    h = hourly("Riyadh_2026-10-10_18")
    morning = {t[11:13]: v for t, v in zip(h["time"], h["pm10"]) if t.startswith("2026-10-17")}
    assert 660 <= morning["06"] <= 720 and 660 <= morning["08"] <= 720
    rows, _ = aq.aggregate("Riyadh", h, dt.date(2026, 10, 17))
    day = by_date(rows)["2026-10-17"]
    assert day["dust_day"] == "1" and day["above_who_pm10"] == "1"


def test_today_is_never_included():
    h = hourly("Riyadh_2026-10-10_18")          # API also returns 24 forecast hours for 18 Oct
    rows, _ = aq.aggregate("Riyadh", h, dt.date(2026, 10, 17))
    assert max(r["date"] for r in rows) == "2026-10-17"
    assert len(rows) == 8


def test_pm10_above_the_api_cap_is_kept_as_a_lower_bound():
    rows, problems = aq.aggregate("Riyadh", hourly("Riyadh_capped"), dt.date(2025, 1, 6))
    day = by_date(rows)["2025-01-06"]
    assert day["pm10_capped_hours"] == 11          # 13:00-23:00 came back null during extreme dust
    assert day["pm10_max"] == aq.PM10_CAP
    assert not problems


def test_unexplained_gap_drops_the_day():
    h = hourly("Jeddah_2d")
    h["pm10"][5] = None                          # low dust, so not the cap: a real gap
    rows, problems = aq.aggregate("Jeddah", h, dt.date(2026, 10, 17))
    assert "2026-10-16" not in by_date(rows) and len(problems) == 1


def test_model_outage_days_are_flagged_not_counted():
    rows, _ = aq.aggregate("Riyadh", hourly("Riyadh_glitch"), dt.date(2022, 12, 16))
    d = by_date(rows)
    assert [d[x]["suspect"] for x in ["2022-12-13", "2022-12-14", "2022-12-15", "2022-12-16"]] == [1, 1, 1, 1]
    assert d["2022-12-14"]["above_who_pm10"] == "" and d["2022-12-14"]["dust_day"] == ""


# --------------------------------------------------------------- full run
@pytest.fixture
def fake_api(monkeypatch):
    """Serve the saved Riyadh sample for every city (enough to exercise the pipeline)."""
    monkeypatch.setattr(aq, "FIRST_DAY", dt.date(2026, 10, 10))
    base = hourly("Riyadh_2026-10-10_18")
    calls = []

    def fetch(city, start, end):
        calls.append((city, start, end))
        keep = [i for i, t in enumerate(base["time"]) if start.isoformat() <= t[:10] <= end.isoformat()]
        return {k: [base[k][i] for i in keep] for k in base}

    fetch.calls = calls
    return fetch


def test_full_run_writes_files_and_passes_checks(tmp_path, fake_api):
    out = aq.run(today=dt.date(2026, 10, 18), fetch=fake_api,
                 daily_path=tmp_path / "daily.csv", summary_path=tmp_path / "summary.csv")
    assert out["rows"] == 8 * 5
    daily = aq.read_daily(tmp_path / "daily.csv")
    assert daily[0]["date"] == "2026-10-10" and daily[-1]["date"] == "2026-10-17"
    s = aq.read_daily(tmp_path / "summary.csv")[0]
    assert s["latest_date"] == "2026-10-17" and s["days_in_year"] == "8"


def test_second_run_only_refetches_last_7_days(tmp_path, fake_api):
    kw = dict(fetch=fake_api, daily_path=tmp_path / "daily.csv", summary_path=tmp_path / "summary.csv")
    aq.run(today=dt.date(2026, 10, 18), **kw)
    fake_api.calls.clear()
    aq.run(today=dt.date(2026, 10, 18), **kw)
    assert {c[1] for c in fake_api.calls} == {dt.date(2026, 10, 11)}   # 11..17 = last 7 days


def test_revised_values_overwrite_old_ones(tmp_path, fake_api):
    path = tmp_path / "daily.csv"
    kw = dict(fetch=fake_api, daily_path=path, summary_path=tmp_path / "summary.csv")
    aq.run(today=dt.date(2026, 10, 18), **kw)
    rows = aq.read_daily(path)
    for r in rows:
        if r["date"] == "2026-10-15":
            r["pm10_mean"] = "1.0"               # pretend an earlier run saved a different value
    aq.write_csv(path, rows, aq.FIELDS)
    aq.run(today=dt.date(2026, 10, 18), **kw)
    assert {r["pm10_mean"] for r in aq.read_daily(path) if r["date"] == "2026-10-15"} != {"1.0"}


# ----------------------------------------------------------------- checks
def good_rows(tmp_path, fake_api):
    aq.run(today=dt.date(2026, 10, 18), fetch=fake_api,
           daily_path=tmp_path / "d.csv", summary_path=tmp_path / "s.csv")
    return aq.read_daily(tmp_path / "d.csv")


def test_check_fails_on_missing_city_day(tmp_path, fake_api):
    rows = [r for r in good_rows(tmp_path, fake_api) if not (r["city"] == "Doha" and r["date"] == "2026-10-12")]
    with pytest.raises(aq.CheckFailed, match="missing city-days"):
        aq.check(rows, dt.date(2026, 10, 18))


def test_check_fails_on_negative_value(tmp_path, fake_api):
    rows = good_rows(tmp_path, fake_api)
    rows[3]["pm25_mean"] = "-1"
    with pytest.raises(aq.CheckFailed, match="negative"):
        aq.check(rows, dt.date(2026, 10, 18))


def test_check_fails_when_data_is_stale(tmp_path, fake_api):
    rows = good_rows(tmp_path, fake_api)          # newest day 17 Oct
    aq.check(rows, dt.date(2026, 10, 20))         # 3 days old: fine
    with pytest.raises(aq.CheckFailed, match="stale"):
        aq.check(rows, dt.date(2026, 10, 21))     # 4 days old: fail


def test_pm25_above_pm10_only_warns(tmp_path, fake_api):
    rows = copy.deepcopy(good_rows(tmp_path, fake_api))
    rows[0]["pm25_mean"] = str(float(rows[0]["pm10_mean"]) + 1)
    assert any("PM2.5" in w for w in aq.check(rows, dt.date(2026, 10, 18)))


def test_rows_are_labelled_with_the_model_period():
    assert aq.model_period("2024-11-11") == aq.PERIOD_BEFORE
    assert aq.model_period("2024-11-12") == aq.PERIOD_AFTER


def test_full_run_adds_model_period_to_every_row(tmp_path, fake_api):
    aq.run(today=dt.date(2026, 10, 18), fetch=fake_api,
           daily_path=tmp_path / "d.csv", summary_path=tmp_path / "s.csv")
    assert {r["model_period"] for r in aq.read_daily(tmp_path / "d.csv")} == {aq.PERIOD_AFTER}
