"""Satellite positions overhead, from TLEs propagated with SGP4.

TLEs are current-epoch sets from CelesTrak, cached locally. Propagating
them backwards stays accurate for a few days; for older captures supply
a historical TLE file (viewer UI: TLEファイル) instead.
"""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "leo_analyzer"
CACHE_TTL_S = 12 * 3600
GROUPS = {
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle",
    "oneweb": "https://celestrak.org/NORAD/elements/gp.php?GROUP=oneweb&FORMAT=tle",
}


def _satrec_class():
    """Prefer the C build; fall back to pure Python where DLLs are blocked."""
    try:
        from sgp4.api import Satrec

        return Satrec
    except Exception:
        from sgp4.model import Satrec  # pure Python

        return Satrec


def tle_path(group: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"tle_{group}.txt"


def fetch_tles(group: str, force: bool = False) -> str:
    path = tle_path(group)
    if not force and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_S:
        return path.read_text(encoding="utf-8")
    request = urllib.request.Request(
        GROUPS[group], headers={"User-Agent": "leo_analyzer/viewer"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", errors="ignore")
    if "1 " not in text:
        raise RuntimeError(f"TLEを取得できませんでした({group})")
    path.write_text(text, encoding="utf-8")
    return text


def parse_tles(text: str):
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out = []
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append((name.strip(), l1, l2))
    return out


class Constellation:
    """Propagates one TLE set and reports what is above the horizon."""

    def __init__(self, group: str, text: str = None):
        self.group = group
        self.records = []
        Satrec = _satrec_class()
        for name, l1, l2 in parse_tles(text if text is not None else fetch_tles(group)):
            try:
                self.records.append((name, Satrec.twoline2rv(l1, l2)))
            except Exception:
                continue

    def __len__(self):
        return len(self.records)

    def look_angles(self, epoch: float, lat: float, lon: float, alt_m: float = 0.0,
                    min_elevation: float = 0.0):
        """Return [{name, az, el, range_km}] for satellites above the horizon."""
        jd, fr = _julian(epoch)
        gmst = _gmst(jd + fr)
        obs = _geodetic_to_ecef(lat, lon, alt_m)
        sin_lat, cos_lat = math.sin(math.radians(lat)), math.cos(math.radians(lat))
        sin_lon, cos_lon = math.sin(math.radians(lon)), math.cos(math.radians(lon))

        results = []
        for name, sat in self.records:
            code, position, _velocity = sat.sgp4(jd, fr)
            if code != 0:
                continue
            # TEME -> ECEF (rotate by GMST; polar motion is negligible here)
            x, y, z = (v * 1000.0 for v in position)
            xe = x * math.cos(gmst) + y * math.sin(gmst)
            ye = -x * math.sin(gmst) + y * math.cos(gmst)
            ze = z
            dx, dy, dz = xe - obs[0], ye - obs[1], ze - obs[2]
            # ECEF -> local ENU
            east = -sin_lon * dx + cos_lon * dy
            north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
            up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
            horizon = math.hypot(east, north)
            elevation = math.degrees(math.atan2(up, horizon))
            if elevation < min_elevation:
                continue
            azimuth = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
            results.append({
                "name": name,
                "az": round(azimuth, 2),
                "el": round(elevation, 2),
                "range_km": round(math.hypot(horizon, up) / 1000.0, 1),
            })
        results.sort(key=lambda s: -s["el"])
        return results


def _julian(epoch: float):
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    jd = 367 * dt.year - (7 * (dt.year + ((dt.month + 9) // 12))) // 4 \
        + (275 * dt.month) // 9 + dt.day + 1721013.5
    fr = (dt.hour + dt.minute / 60 + (dt.second + dt.microsecond / 1e6) / 3600) / 24
    return jd, fr


def _gmst(jd_ut1: float) -> float:
    t = (jd_ut1 - 2451545.0) / 36525.0
    seconds = (67310.54841 + (876600.0 * 3600 + 8640184.812866) * t
               + 0.093104 * t * t - 6.2e-6 * t * t * t)
    return math.radians((seconds % 86400.0) / 240.0) % (2 * math.pi)


def _geodetic_to_ecef(lat: float, lon: float, alt_m: float):
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    return (
        (n + alt_m) * math.cos(lat_r) * math.cos(lon_r),
        (n + alt_m) * math.cos(lat_r) * math.sin(lon_r),
        (n * (1 - e2) + alt_m) * math.sin(lat_r),
    )


_CACHE = {}


def constellation(group: str, tle_file: str = None) -> Constellation:
    key = (group, tle_file)
    if key not in _CACHE:
        text = Path(tle_file).read_text(encoding="utf-8") if tle_file else None
        _CACHE[key] = Constellation(group, text)
    return _CACHE[key]


def tle_age_days(group: str) -> float:
    """How old the cached elements are — accuracy degrades with distance."""
    path = tle_path(group)
    if not path.exists():
        return float("nan")
    return (time.time() - path.stat().st_mtime) / 86400.0
