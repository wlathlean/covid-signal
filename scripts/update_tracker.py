#!/usr/bin/env python3
"""Refresh the WA/TX COVID Signal dataset from official CDC APIs."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config" / "sources.json").read_text())
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
PUBLIC = ROOT / "public" / "data"
APP_PUBLIC = ROOT / "app" / "public" / "data"
DB = ROOT / "data" / "covid_tracker.sqlite3"
STATE_NAMES = CONFIG["states"]
WEIGHTS = {"wastewater": 0.40, "emergency": 0.25, "hospital": 0.20, "deaths": 0.15}
WASTEWATER_COMPLETENESS_MINIMUM = 0.75


def api_get(source: str, params: dict[str, str]) -> list[dict]:
    base = CONFIG["sources"][source]["url"]
    url = f"{base}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "COVID-Signal/1.0 personal-research"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Unable to fetch {source}: {last_error}")


def fetch_all() -> dict[str, list[dict]]:
    names = ','.join(f"'{name}'" for name in STATE_NAMES.values())
    codes = ','.join(f"'{code}'" for code in STATE_NAMES)
    return {
        "wastewater": api_get("wastewater", {
            "$where": f"state_territory in ({names}) AND pathogen_target='SARS-CoV-2'",
            "$order": "week_end ASC", "$limit": "50000"
        }),
        "emergency": api_get("emergency", {
            "$where": f"geography in ({names}) AND pathogen='COVID-19'",
            "$order": "week_end ASC", "$limit": "10000"
        }),
        "county_emergency": api_get("county_emergency", {
            "$where": f"geography in ({names})",
            "$order": "week_end DESC", "$limit": "50000"
        }),
        "hospital": api_get("hospital", {
            "$where": f"jurisdiction in ({codes})",
            "$order": "weekendingdate ASC", "$limit": "10000"
        }),
        "deaths": api_get("deaths", {
            "$where": f"state in ({names}) AND `group`='By Week'",
            "$order": "week_ending_date ASC", "$limit": "10000"
        }),
    }


def number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def day(value: str) -> str:
    return value[:10]


def aggregate(raw: dict[str, list[dict]]) -> dict[str, dict[str, list[dict]]]:
    output = {code: defaultdict(list) for code in STATE_NAMES}
    reverse = {name: code for code, name in STATE_NAMES.items()}

    ww = defaultdict(list)
    for row in raw["wastewater"]:
        value = number(row.get("site_wval"))
        if value is None: continue
        code = reverse.get(row.get("state_territory"))
        if not code: continue
        ww[(code, day(row["week_end"]))].append((value, number(row.get("population_served")) or 0))
    for (code, week), values in ww.items():
        covered = sum(weight for _, weight in values)
        metric = sum(value * weight for value, weight in values) / covered if covered else statistics.mean(v for v, _ in values)
        output[code]["wastewater"].append({"date": week, "value": round(metric, 2), "sites": len(values), "population_covered": int(covered)})

    for row in raw["emergency"]:
        code = reverse.get(row.get("geography")); value = number(row.get("percent_visits"))
        if code and value is not None:
            output[code]["emergency"].append({"date": day(row["week_end"]), "value": value})

    for row in raw["hospital"]:
        code = row.get("jurisdiction"); value = number(row.get("totalconfc19newadmper100k"))
        if code in STATE_NAMES and value is not None:
            output[code]["hospital"].append({"date": day(row["weekendingdate"]), "value": value, "reporting_percent": number(row.get("totalconfc19newadmperchosprep"))})

    death_cutoff = date.today() - timedelta(days=28)
    for row in raw["deaths"]:
        code = reverse.get(row.get("state")); value = number(row.get("covid_19_deaths"))
        if code and value is not None:
            when = day(row["week_ending_date"])
            output[code]["deaths"].append({"date": when, "value": value, "provisional": date.fromisoformat(when) > death_cutoff})

    return {code: {metric: sorted(rows, key=lambda x: x["date"]) for metric, rows in metrics.items()} for code, metrics in output.items()}


def percentile(series: list[dict], latest: dict) -> float:
    cutoff = date.fromisoformat(latest["date"]) - timedelta(weeks=104)
    values = [row["value"] for row in series if date.fromisoformat(row["date"]) >= cutoff and row["date"] <= latest["date"]]
    if len(values) < 8: return 50.0
    below = sum(v < latest["value"] for v in values)
    equal = sum(v == latest["value"] for v in values)
    return round(100 * (below + 0.5 * equal) / len(values), 1)


def recent_complete(metric: str, series: list[dict]) -> list[dict]:
    if metric == "deaths": return [row for row in series if not row.get("provisional")]
    return series


def wastewater_reporting_status(series: list[dict], index: int) -> dict:
    row = series[index]
    prior = [item for item in series[max(0, index - 6):index] if item.get("sites")]
    expected_sites = round(statistics.median(item["sites"] for item in prior)) if prior else row.get("sites", 0)
    populations = [item.get("population_covered", 0) for item in prior if item.get("population_covered", 0) > 0]
    expected_population = round(statistics.median(populations)) if populations else row.get("population_covered", 0)
    ratios = []
    if expected_sites: ratios.append(row.get("sites", 0) / expected_sites)
    if expected_population: ratios.append(row.get("population_covered", 0) / expected_population)
    reporting_ratio = min(ratios) if ratios else 1.0
    return {
        "reporting_complete": len(prior) < 4 or reporting_ratio >= WASTEWATER_COMPLETENESS_MINIMUM,
        "reporting_percent": round(min(reporting_ratio, 1.0) * 100),
        "expected_sites": expected_sites,
        "expected_population_covered": expected_population,
    }


def select_wastewater_week(series: list[dict]) -> tuple[int, dict]:
    if not series: return -1, {}
    newest_status = wastewater_reporting_status(series, len(series) - 1)
    selected = len(series) - 1
    if not newest_status["reporting_complete"]:
        for index in range(len(series) - 2, max(-1, len(series) - 5), -1):
            if wastewater_reporting_status(series, index)["reporting_complete"]:
                selected = index
                break
    status = wastewater_reporting_status(series, selected)
    status.update({"latest_available_date": series[-1]["date"], "reporting_delayed": selected != len(series) - 1})
    return selected, status


def build_geography(raw: dict[str, list[dict]]) -> dict[str, dict]:
    reverse = {name: code for code, name in STATE_NAMES.items()}
    result = {code: {"counties": {}, "wastewater_counties": {}, "wastewater_summary": {}} for code in STATE_NAMES}

    for code, name in STATE_NAMES.items():
        ed_rows = [row for row in raw["county_emergency"] if row.get("geography") == name and row.get("fips")]
        if ed_rows:
            latest_week = max(day(row["week_end"]) for row in ed_rows)
            for row in ed_rows:
                if day(row["week_end"]) != latest_week: continue
                value = number(row.get("percent_visits_smoothed_covid") or row.get("percent_visits_covid"))
                if value is None: continue
                result[code]["counties"][row["fips"]] = {
                    "name": row.get("county"), "emergency": value,
                    "emergency_trend": row.get("ed_trends_covid", "Unknown"),
                    "hsa": row.get("hsa"), "date": latest_week,
                }

        ww_rows = [row for row in raw["wastewater"] if row.get("state_territory") == name and row.get("counties_served")]
        if ww_rows:
            weekly = defaultdict(list)
            for row in ww_rows: weekly[day(row["week_end"])].append(row)
            weekly_summary = []
            for week, rows in sorted(weekly.items()):
                weekly_summary.append({
                    "date": week,
                    "sites": len(rows),
                    "population_covered": int(sum(number(row.get("population_served")) or 0 for row in rows)),
                })
            selected_index, reporting = select_wastewater_week(weekly_summary)
            latest_week = weekly_summary[selected_index]["date"]
            result[code]["wastewater_summary"] = {**weekly_summary[selected_index], **reporting}
            county_values = defaultdict(list)
            for row in ww_rows:
                if day(row["week_end"]) != latest_week: continue
                value = number(row.get("site_wval"))
                if value is None: continue
                for county in (part.strip() for part in row["counties_served"].split(",")):
                    if county:
                        county_values[county].append((value, number(row.get("population_served")) or 0))
            for county, values in county_values.items():
                population = sum(weight for _, weight in values)
                value = sum(item * weight for item, weight in values) / population if population else statistics.mean(item for item, _ in values)
                result[code]["wastewater_counties"][county] = {
                    "value": round(value, 2), "sites": len(values), "date": latest_week,
                }
    return result


def direction(series: list[dict]) -> str:
    if len(series) < 6: return "Unknown"
    recent = statistics.mean(row["value"] for row in series[-3:])
    prior = statistics.mean(row["value"] for row in series[-6:-3])
    if prior == 0: return "Rising" if recent > 0 else "Stable"
    change = (recent - prior) / prior
    return "Rising" if change > 0.15 else "Falling" if change < -0.15 else "Stable"


def band(score: float) -> str:
    return ["Very low", "Low", "Moderate", "High", "Very high"][min(4, int(score // 20))]


def build_payload(series: dict[str, dict[str, list[dict]]], geography: dict[str, dict]) -> dict:
    now = datetime.now(timezone.utc)
    result = {"schema_version": 1, "generated_at": now.isoformat(), "states": {}, "sources": CONFIG["sources"], "methodology": {"weights": WEIGHTS, "history_window_weeks": 104, "death_lag_days": 28}}
    for code, name in STATE_NAMES.items():
        summaries = {}
        scored = []
        for metric, weight in WEIGHTS.items():
            complete = recent_complete(metric, series[code].get(metric, []))
            if not complete:
                summaries[metric] = {"available": False}
                continue
            reporting = {}
            selected = len(complete) - 1
            if metric == "wastewater": selected, reporting = select_wastewater_week(complete)
            selected_history = complete[:selected + 1]
            latest = selected_history[-1]
            rank = percentile(selected_history, latest)
            scored.append((rank, weight))
            age = (now.date() - date.fromisoformat(latest["date"])).days
            summaries[metric] = {"available": True, "latest": latest, "percentile": rank, "trend": direction(selected_history), "age_days": age, "stale": age > (45 if metric == "deaths" else 14), "history": selected_history[-52:], **reporting}
        score = round(sum(value * weight for value, weight in scored) / sum(weight for _, weight in scored)) if scored else None
        trends = [value["trend"] for value in summaries.values() if value.get("available")]
        overall_trend = "Rising" if trends.count("Rising") >= 2 else "Falling" if trends.count("Falling") >= 2 else "Stable"
        result["states"][code] = {"name": name, "score": score, "level": band(score) if score is not None else "Unavailable", "trend": overall_trend, "metrics": summaries, "geography": geography[code]}
    return result


def persist_raw(raw: dict[str, list[dict]], stamp: str) -> None:
    snapshot = RAW / "archive" / stamp
    snapshot.mkdir(parents=True, exist_ok=True)
    for source, rows in raw.items():
        content = json.dumps(rows, indent=2) + "\n"
        (RAW / f"{source}.json").write_text(content)
        (snapshot / f"{source}.json").write_text(content)


def persist_database(raw: dict[str, list[dict]], payload: dict) -> None:
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS refreshes (generated_at TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS observations (state TEXT, metric TEXT, observed_on TEXT, value REAL, metadata TEXT, PRIMARY KEY(state, metric, observed_on))")
        db.execute("INSERT OR REPLACE INTO refreshes VALUES (?, ?)", (payload["generated_at"], json.dumps(payload)))
        for code, state in payload["states"].items():
            for metric, summary in state["metrics"].items():
                for row in summary.get("history", []):
                    db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?)", (code, metric, row["date"], row["value"], json.dumps(row)))


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def main() -> int:
    for directory in (RAW, PROCESSED, PUBLIC, APP_PUBLIC): directory.mkdir(parents=True, exist_ok=True)
    raw = fetch_all()
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    persist_raw(raw, stamp)
    payload = build_payload(aggregate(raw), build_geography(raw))
    persist_database(raw, payload)
    atomic_json(PROCESSED / "tracker.json", payload)
    atomic_json(PUBLIC / "tracker.json", payload)
    atomic_json(APP_PUBLIC / "tracker.json", payload)
    print(f"Updated {', '.join(STATE_NAMES)} at {payload['generated_at']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
