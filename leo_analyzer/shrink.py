"""Shrink an oversized telemetry CSV after the fact.

Older runs embedded the spectrum blob into every CSV row. This rewrites
such a file with the bulk cells moved into a companion .jsonl.gz, so the
CSV becomes small enough to open while no data is thrown away.
"""

import csv
import gzip
import json
import sys
from pathlib import Path

BIG_CELL_CHARS = 400


def _open_text(path: Path, mode="rt"):
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding="utf-8", newline="")
    return open(path, mode, encoding="utf-8", newline="")


def shrink_csv(path, max_chars: int = BIG_CELL_CHARS):
    """Return (slim_csv_path, bulk_jsonl_path, stats)."""
    path = Path(path)
    stem = path.name
    for ext in (".gz", ".csv"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
    slim_path = path.with_name(stem + "_slim.csv")
    bulk_path = path.with_name(stem + "_bulk.jsonl.gz")

    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    stats = {"rows": 0, "moved_cells": 0, "bytes_in": 0, "bulk_columns": {}}

    with _open_text(path) as src, open(
        slim_path, "w", encoding="utf-8", newline=""
    ) as dst, gzip.open(bulk_path, "wt", encoding="utf-8") as bulk:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            stats["rows"] += 1
            heavy = {}
            for key, value in row.items():
                if value and len(value) > max_chars:
                    heavy[key] = value
                    stats["bytes_in"] += len(value)
                    stats["bulk_columns"][key] = (
                        stats["bulk_columns"].get(key, 0) + len(value)
                    )
                    row[key] = f"[{len(value)}文字を分離]"
            if heavy:
                stats["moved_cells"] += len(heavy)
                bulk.write(
                    json.dumps(
                        {
                            "timestamp_utc": row.get("timestamp_utc", ""),
                            "epoch": row.get("epoch", ""),
                            "data": heavy,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            writer.writerow(row)

    stats["slim_bytes"] = slim_path.stat().st_size
    stats["bulk_bytes"] = bulk_path.stat().st_size
    return slim_path, bulk_path, stats
