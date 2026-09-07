"""Rain around the antenna at the time of the measurement.

Uses Open-Meteo (no API key): the forecast endpoint with past_days for
recent captures, the ERA5 archive for older ones. One request returns a
whole grid of points, which the viewer draws as a rain overlay.
"""

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
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
        return json.loads(response.read().decode("utf-8"))


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
    if age_days <= 90:
        params["past_days"] = str(max(1, min(92, age_days + 1)))
        params["forecast_days"] = "1"
        url = FORECAST_URL
        source = "Open-Meteo (実況/予報モデル)"
    else:
        day = when.strftime("%Y-%m-%d")
        params["start_date"] = day
        params["end_date"] = (when + timedelta(days=1)).strftime("%Y-%m-%d")
        url = ARCHIVE_URL
        source = "Open-Meteo (ERA5 再解析)"

    try:
        payload = _request(url, params)
    except Exception as e:
        return {"error": f"降水データを取得できませんでした: {e}"}
    if isinstance(payload, dict):
        payload = [payload]

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
