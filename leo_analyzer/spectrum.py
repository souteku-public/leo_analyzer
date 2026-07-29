"""Read back the spectrum captured into kymeta_*adc*.jsonl(.gz).

The antenna returns each sweep as a base64 float32 blob. Nothing is lost
when it is moved out of the CSV: this module decodes the stored records
so the sweep can be plotted or exported exactly as captured.
"""

import base64
import gzip
import json
import math
import struct
import zlib
from pathlib import Path


def find_spectrum_files(rundir):
    rundir = Path(rundir)
    return sorted(
        p
        for p in rundir.glob("kymeta_*.jsonl*")
        if "adc" in p.name or "spectrum" in p.name
    )


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def decode_blob(text: str):
    """base64 float32 -> list of floats (None if it is not that)."""
    if not text or len(text) < 64:
        return None
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except Exception:
        return None
    count = len(raw) // 4
    if count < 16:
        return None
    values = struct.unpack("<%df" % count, raw[: count * 4])
    finite = [v if math.isfinite(v) else 0.0 for v in values]
    return finite


def _longest_string(obj) -> str:
    best = ""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str) and len(cur) > len(best):
            best = cur
    return best


def _numeric_list(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            if len(cur) >= 16 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in cur
            ):
                return list(cur)
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def load_sweeps(path, max_sweeps=1200):
    """Return (epochs, sweeps) decoded from a spectrum jsonl file."""
    path = Path(path)
    records = []
    with _open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            data = rec.get("data", rec)
            values = _numeric_list(data) or decode_blob(_longest_string(data))
            if values:
                records.append((rec.get("epoch"), values))

    if len(records) > max_sweeps:  # keep an even spread across the capture
        step = len(records) / max_sweeps
        records = [records[int(i * step)] for i in range(max_sweeps)]
    epochs = [r[0] for r in records]
    sweeps = [r[1] for r in records]
    return epochs, sweeps


def downsample_bins(sweep, target=320):
    """Average neighbouring bins down to `target` points."""
    n = len(sweep)
    if n <= target:
        return list(sweep)
    out = []
    for i in range(target):
        lo = i * n // target
        hi = max(lo + 1, (i + 1) * n // target)
        chunk = sweep[lo:hi]
        out.append(sum(chunk) / len(chunk))
    return out


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# --- minimal PNG writer (stdlib only, keeps the report self-contained) ---

def encode_png(rows_rgb, width, height) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        raw += rows_rgb[y]
    compressed = zlib.compress(bytes(raw), 9)

    def chunk(tag, payload):
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


# sequential blue ramp (validated palette, step 100 -> 700), light to dark
RAMP = [
    (0xCD, 0xE2, 0xFB), (0xB7, 0xD3, 0xF6), (0x9E, 0xC5, 0xF4),
    (0x86, 0xB6, 0xEF), (0x6D, 0xA7, 0xEC), (0x55, 0x98, 0xE7),
    (0x39, 0x87, 0xE5), (0x2A, 0x78, 0xD6), (0x25, 0x6A, 0xBF),
    (0x1C, 0x5C, 0xAB), (0x18, 0x4F, 0x95), (0x10, 0x42, 0x81),
    (0x0D, 0x36, 0x6B),
]


def waterfall_png(sweeps, bins=320):
    """Time (x) x frequency (y) heat map as PNG bytes, plus the value range."""
    if not sweeps:
        return None, 0, 0, 0.0, 0.0
    matrix = [downsample_bins(s, bins) for s in sweeps]
    height = len(matrix[0])
    width = len(matrix)

    flat = sorted(v for row in matrix for v in row)
    lo = percentile(flat, 2)
    hi = percentile(flat, 98)
    if hi <= lo:
        hi = lo + 1.0

    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            value = matrix[x][height - 1 - y]  # low frequency at the bottom
            t = (value - lo) / (hi - lo)
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            r, g, b = RAMP[int(t * (len(RAMP) - 1))]
            row += bytes((r, g, b))
        rows.append(row)
    return encode_png(rows, width, height), width, height, lo, hi
