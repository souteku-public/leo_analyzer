"""Rain around the antenna at the time of the measurement.

Uses Open-Meteo (no API key). Which of its three archives answers for a
given day is not something you can decide from the date alone: the
forecast endpoint still returns 200 for older days and simply leaves the
values null, roughly two months back. So the endpoints are tried in turn
and the first reply that actually carries values wins.

Requests are scoped to the single day being drawn. Asking with past_days
instead pulls every hour since then for all 169 grid points - about 4 MB
for one frame, and enough call weight to burn the free daily quota in a
handful of frames, after which everything fails at once.
"""

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Archived runs of the same high-resolution model (JMA MSM over Japan),
# kept from 2021 on - this is what covers captures older than the
# forecast endpoint's window.
HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# Last resort: ERA5 reanalysis, 25 km, but continuous back to 1940.
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ENDPOINTS = [
    (FORECAST_URL, "Open-Meteo 実況/予報"),
    (HISTORICAL_URL, "Open-Meteo 過去モデル(高解像度)"),
    (ARCHIVE_URL, "Open-Meteo ERA5(25km)"),
]
# The model behind Open-Meteo over Japan (JMA MSM) is on a 0.05 deg grid,
# about 5.5 km. Sampling 40 km across at 3.3 km keeps that detail; the old
# 7x7 over 1 deg threw most of it away at 18 km spacing.
GRID_N = 13
GRID_SPAN_DEG = 0.36
_CACHE = {}
_CACHE_TTL_S = 1800


def _grid(lat: float, lon: float):
    half = GRID_SPAN_DEG / 2
    lon_scale = 1.0 / max(math.cos(math.radians(lat)), 0.2)
    lats, lons = [], []
    for i in range(GRID_N):
        frac = -half + GRID_SPAN_DEG * i / (GRID_N - 1)
        lats.append(round(lat + frac, 4))
        lons.append(round(lon + frac * lon_scale, 4))
    points = [(la, lo) for la in lats for lo in lons]
    return lats, lons, points


def _request(url: str, params: dict):
    query = urllib.parse.urlencode(params, safe=",")
    request = urllib.request.Request(
        f"{url}?{query}", headers={"User-Agent": "leo_analyzer/viewer"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    entries = payload if isinstance(payload, list) else [payload]
    head = entries[0] if entries else {}
    # Quota and parameter errors come back as a normal 200 with a reason
    if isinstance(head, dict) and (head.get("error") or head.get("reason")):
        raise RuntimeError(head.get("reason") or "APIエラー")
    return entries


def _hourly(params: dict, age_days: int):
    """First endpoint that answers with actual values."""
    order = [0, 1, 2] if age_days <= 5 else [1, 0, 2]
    last = None
    for i in order:
        url, label = ENDPOINTS[i]
        try:
            entries = _request(url, params)
        except Exception as e:
            last = e
            continue
        if any(
            v is not None
            for e in entries
            for v in e.get("hourly", {}).get("precipitation", [])
        ):
            return entries, label
        last = RuntimeError("この期間の値が入っていませんでした")
    raise last or RuntimeError("取得できませんでした")


def rain_grid(lat: float, lon: float, epoch: float):
    """Hourly precipitation (mm) on a grid covering the capture area."""
    key = (round(lat, 2), round(lon, 2), int(epoch // 3600))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]

    lats, lons, points = _grid(lat, lon)
    when = datetime.fromtimestamp(epoch, timezone.utc)
    age_days = (datetime.now(timezone.utc) - when).days

    params = {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
        "hourly": "precipitation",
        "timezone": "UTC",
    }
    day = when.strftime("%Y-%m-%d")
    params["start_date"] = day
    params["end_date"] = day

    try:
        payload, source = _hourly(params, age_days)
    except Exception as e:
        return {"error": f"降水データを取得できませんでした: {e}"}

    stamp = when.strftime("%Y-%m-%dT%H:00")
    values, hours = [], None
    for entry in payload:
        hourly = entry.get("hourly", {})
        hours = hourly.get("time", [])
        precipitation = hourly.get("precipitation", [])
        index = hours.index(stamp) if stamp in hours else None
        values.append(
            precipitation[index]
            if index is not None and index < len(precipitation)
            else None
        )

    result = {
        "lats": lats,
        "lons": lons,
        "grid": values,          # row-major: lat index outer, lon index inner
        "n": GRID_N,
        "hour_utc": stamp,
        "source": source,
        "unit": "mm/h",
        "available": any(v is not None for v in values),
    }
    _CACHE[key] = (time.time(), result)
    return result
