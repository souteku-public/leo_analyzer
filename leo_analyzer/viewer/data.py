"""Load measurement runs for the viewer, from any directory on disk."""

import csv
import gzip
import json
import math
import re
import sys
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# canonical channel -> column patterns, matched against any loaded CSV
CHANNELS = [
    ("down_mbps", [r"^mbps_down$", r"downlink_throughput_bps$"], 1.0),
    ("up_mbps", [r"^mbps_up$", r"uplink_throughput_bps$"], 1.0),
    ("rtt_ms", [r"^latency_ms$", r"pop_ping_latency_ms$"], 1.0),
    ("loss", [r"pop_ping_drop_rate$"], 1.0),
    ("snr_db", [r"sinr[_ ]?db$", r"\bsinr$", r"snr_db$"], 1.0),
    ("speed", [r"position\.speed$"], 1.0),
]
# antenna pointing: (source, azimuth patterns, elevation patterns).
# Kymeta's look-angle is the direction of the satellite being tracked and
# jumps at every handover; Starlink only publishes the dish's mounted
# attitude (boresight) — the phased array's beam vector is not exposed —
# so the viewer draws the two differently.
POINTING = [
    ("kymeta", [r"look-angle\.azimuth$"], [r"look-angle\.elevation$"]),
    ("starlink", [r"boresight_azimuth_deg$"], [r"boresight_elevation_deg$"]),
]
LAT_PATTERNS = [r"position\.latitude$", r"\blatitude$", r"\blat$"]
LON_PATTERNS = [r"position\.longitude$", r"\blongitude$", r"\blon$"]
OBSTRUCTION_NAMES = (
    "starlink_obstruction_map.jsonl.gz",
    "starlink_obstruction_map.jsonl",
)
# one grid is ~15k floats, so the index is served on load and each grid is
# fetched on demand; only the last few are held in memory
_obs_index_cache = {}
_obs_grid_cache = {}
_OBS_GRID_CACHE_MAX = 8


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def _num(value):
    if value in ("", None):
        return None
    if value in ("True", "true"):
        return 1.0
    if value in ("False", "false"):
        return 0.0
    try:
        f = float(value)
    except ValueError:
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _match(columns, patterns):
    for pattern in patterns:
        rx = re.compile(pattern, re.I)
        for col in columns:
            if rx.search(col):
                return col
    return None


def list_dir(path: str):
    """Directory listing for the source picker."""
    base = Path(path).expanduser()
    if not base.is_dir():
        base = base.parent if base.parent.is_dir() else Path.home()
    entries = []
    try:
        for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                has_data = any(item.glob("*.csv")) or any(item.glob("*.csv.gz"))
                entries.append(
                    {"name": item.name, "path": str(item), "dir": True,
                     "run": has_data}
                )
            elif item.suffix in (".csv", ".gz"):
                entries.append(
                    {"name": item.name, "path": str(item), "dir": False,
                     "size": item.stat().st_size}
                )
    except PermissionError:
        pass
    return {
        "path": str(base),
        "parent": str(base.parent) if base.parent != base else None,
        "entries": entries,
    }


def _csv_files(target: Path):
    if target.is_file():
        return [target]
    files = []
    for pattern in ("*.csv", "*.csv.gz"):
        files.extend(sorted(target.glob(pattern)))
    return [f for f in files if not f.name.endswith("_slim.csv") or len(files) == 1]


# Starlink reconfigures on UTC 15-second boundaries, so packet loss caused
# by a handover lands in the first seconds of each cycle.
HANDOVER_PERIOD_S = 15
HANDOVER_WINDOW_S = 2


# Throughput only means something inside its own phase: averaging the
# download rate across the idle baseline and the upload run buries it.
STAT_KEYS = ["down_mbps", "up_mbps", "rtt_ms", "snr_db", "loss", "speed"]
STAT_PHASE = {"down_mbps": "download", "up_mbps": "upload"}


def _series_stats(samples: dict):
    """Mean/min/max per channel over every sample, before any thinning."""
    out = {}
    for key in STAT_KEYS:
        phase = STAT_PHASE.get(key)
        rows = samples.values()
        used_phase = None
        if phase and any(r.get("phase") == phase for r in rows):
            rows = [r for r in samples.values() if r.get("phase") == phase]
            used_phase = phase
        values = [r[key] for r in rows if r.get(key) is not None]
        if not values:
            continue
        out[key] = {
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "n": len(values),
            "phase": used_phase,
        }
    return out


def _handover_stats(samples: dict):
    """How much of the packet loss sits in the 15-second handover window.

    Computed on every sample, before the display granularity thins the
    series — sampling every 10th second would alias against the 15-second
    period and invent a concentration that is not there.
    """
    seconds = [t for t, row in samples.items() if (row.get("loss") or 0) > 0]
    if not seconds:
        return None
    hit = sum(1 for t in seconds if t % HANDOVER_PERIOD_S < HANDOVER_WINDOW_S)
    expected = HANDOVER_WINDOW_S / HANDOVER_PERIOD_S
    share = hit / len(seconds)
    return {
        "total": len(seconds),
        "hit": hit,
        "share": round(share, 4),
        "expected": round(expected, 4),
        "ratio": round(share / expected, 2),
    }


def _obstruction_file(target: str):
    path = Path(target).expanduser()
    base = path if path.is_dir() else path.parent
    for name in OBSTRUCTION_NAMES:
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def obstruction_index(target: str):
    """Timestamps and shape of every stored obstruction map, no grids."""
    file = _obstruction_file(target)
    if file is None:
        return {"file": None, "maps": []}
    stamp = (str(file), file.stat().st_mtime, file.stat().st_size)
    hit = _obs_index_cache.get(str(file))
    if hit and hit[0] == stamp:
        return hit[1]

    maps = []
    with _open_text(file) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            maps.append(
                {
                    "i": i,
                    "epoch": record.get("epoch"),
                    "rows": record.get("rows"),
                    "cols": record.get("cols"),
                    "meta": record.get("meta") or {},
                }
            )
    out = {"file": file.name, "maps": maps}
    _obs_index_cache[str(file)] = (stamp, out)
    return out


def obstruction_map(target: str, index: int):
    """One grid, read back from the file by line number."""
    file = _obstruction_file(target)
    if file is None:
        raise FileNotFoundError("障害物マップのファイルがありません")
    key = (str(file), index)
    if key in _obs_grid_cache:
        return _obs_grid_cache[key]
    with _open_text(file) as f:
        for i, line in enumerate(f):
            if i != index:
                continue
            record = json.loads(line)
            if len(_obs_grid_cache) >= _OBS_GRID_CACHE_MAX:
                _obs_grid_cache.clear()
            _obs_grid_cache[key] = record
            return record
    raise FileNotFoundError(f"障害物マップ #{index} が見つかりません")


def load_run(target: str, step: int = 1):
    """Read a run directory (or a single CSV) into time-aligned arrays."""
    path = Path(target).expanduser()
    if not path.exists():
        raise FileNotFoundError(target)

    label = path.name
    meta = {}
    summary_path = (path / "summary.json") if path.is_dir() else None
    if summary_path and summary_path.exists():
        try:
            meta = json.loads(summary_path.read_text(encoding="utf-8"))
            label = meta.get("label", label)
        except Exception:
            meta = {}

    samples = {}  # epoch -> {channel: value}
    sources = []
    for file in _csv_files(path):
        with _open_text(file) as f:
            reader = csv.DictReader(f)
            columns = reader.fieldnames or []
            if "epoch" not in columns:
                continue
            picked = {
                name: (_match(columns, pats), scale)
                for name, pats, scale in CHANNELS
            }
            point_cols = {
                src: (_match(columns, azp), _match(columns, elp))
                for src, azp, elp in POINTING
            }
            lat_col = _match(columns, LAT_PATTERNS)
            lon_col = _match(columns, LON_PATTERNS)
            found = [n for n, (c, _s) in picked.items() if c]
            found += [f"{s}_pointing" for s, (a, e) in point_cols.items() if a and e]
            if lat_col:
                found.append("position")
            sources.append({"file": file.name, "channels": found})

            for row in reader:
                epoch = _num(row.get("epoch"))
                if epoch is None:
                    continue
                slot = samples.setdefault(int(epoch), {})
                for name, (col, scale) in picked.items():
                    if not col:
                        continue
                    value = _num(row.get(col))
                    if value is None:
                        continue
                    if col.endswith("_bps"):
                        value /= 1e6
                    slot[name] = value
                for src, (az_col, el_col) in point_cols.items():
                    if not (az_col and el_col):
                        continue
                    az, el = _num(row.get(az_col)), _num(row.get(el_col))
                    if az is not None and el is not None:
                        slot[f"{src}_az"] = az
                        slot[f"{src}_el"] = el
                if lat_col and lon_col:
                    lat, lon = _num(row.get(lat_col)), _num(row.get(lon_col))
                    if lat is not None and lon is not None and (lat or lon):
                        slot["lat"] = lat
                        slot["lon"] = lon
                if row.get("phase"):
                    slot["phase"] = row["phase"]

    if not samples:
        raise ValueError("epoch 列を持つCSVが見つかりませんでした")

    handover = _handover_stats(samples)
    stats = _series_stats(samples)

    times = sorted(samples)
    if step > 1:  # thin to the requested granularity
        kept, next_t = [], times[0]
        for t in times:
            if t >= next_t:
                kept.append(t)
                next_t = t + step
        times = kept

    keys = ["down_mbps", "up_mbps", "rtt_ms", "loss", "snr_db", "speed",
            "kymeta_az", "kymeta_el", "starlink_az", "starlink_el",
            "lat", "lon"]
    series = {k: [samples[t].get(k) for t in times] for k in keys}
    series["phase"] = [samples[t].get("phase", "") for t in times]

    positions = [
        (la, lo) for la, lo in zip(series["lat"], series["lon"])
        if la is not None and lo is not None
    ]
    home = None
    if positions:
        mid_lat = sum(p[0] for p in positions) / len(positions)
        # in metres, so that a test course a few hundred metres across still
        # counts as a drive (a degree threshold makes that look stationary)
        span_ns = (max(p[0] for p in positions) - min(p[0] for p in positions)) * 111320
        span_ew = (max(p[1] for p in positions) - min(p[1] for p in positions)) * (
            111320 * math.cos(math.radians(mid_lat))
        )
        home = {
            "lat": mid_lat,
            "lon": sum(p[1] for p in positions) / len(positions),
            "from_data": True,
            "moving": math.hypot(span_ns, span_ew) > 50,
        }

    return {
        "label": label,
        "path": str(path),
        "meta": {k: meta.get(k) for k in
                 ("tool_version", "measure_mode", "started_utc", "collectors")},
        "sources": sources,
        "times": times,
        "series": series,
        "home": home,
        "step": step,
        "handover": handover,
        "stats": stats,
    }
