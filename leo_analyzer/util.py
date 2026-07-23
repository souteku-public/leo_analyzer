"""Shared helpers: timestamping, JSON flattening, CSV writing."""

import csv
import json
import time
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def epoch_now() -> float:
    return time.time()


def flatten(obj, parent_key: str = "", sep: str = ".") -> dict:
    """Flatten nested dicts into dot-separated keys.

    Lists are JSON-encoded into a single cell so the CSV column set stays
    stable even when list lengths vary between samples.
    """
    items = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{parent_key}{sep}{k}" if parent_key else str(k)
            if isinstance(v, dict):
                items.update(flatten(v, key, sep))
            elif isinstance(v, list):
                items[key] = json.dumps(v, ensure_ascii=False)
            else:
                items[key] = v
    else:
        items[parent_key or "value"] = obj
    return items


class CsvLogger:
    """CSV writer whose columns are fixed by the first row.

    Later rows may omit keys (left blank); keys not present in the first
    row are dropped, keeping the file rectangular.
    """

    def __init__(self, path):
        self.path = path
        self._file = None
        self._writer = None
        self._fields = None

    def write_row(self, row: dict):
        if self._writer is None:
            self._fields = list(row.keys())
            self._file = open(self.path, "w", newline="", encoding="utf-8")
            self._writer = csv.DictWriter(
                self._file, fieldnames=self._fields, extrasaction="ignore"
            )
            self._writer.writeheader()
        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


def percentile(sorted_values, p: float):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def summarize(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {}
    return {
        "samples": len(vals),
        "min": round(vals[0], 3),
        "max": round(vals[-1], 3),
        "avg": round(sum(vals) / len(vals), 3),
        "p50": round(percentile(vals, 50), 3),
        "p90": round(percentile(vals, 90), 3),
        "p10": round(percentile(vals, 10), 3),
    }
