"""Base class for 1 Hz antenna telemetry collectors."""

import asyncio
import json
import re
import time
from pathlib import Path

from ..util import CsvLogger, epoch_now, utc_now_iso

# Columns are chosen after watching this many seconds of samples, so that
# fields which never arrive (or never change) can be left out of the CSV.
LEARN_SECONDS = 90
ALWAYS_KEEP = ("timestamp_utc", "epoch", "error", "notes")
MAX_ERROR_CHARS = 200
# when an antenna keeps failing, slow down instead of hammering it every
# second (and filling the CSV with identical errors)
BACKOFF_STEPS = ((10, 5.0), (60, 15.0), (300, 30.0))


def compact_error(exc) -> str:
    """One-line, bounded error text — gRPC dumps span several lines."""
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"\s+", " ", text).strip()
    # gRPC repeats the same message in debug_error_string; keep the first
    text = re.split(r"\s*debug_error_string\s*=", text)[0].strip()
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS] + "…"
    return text


def select_columns(rows):
    """Split observed columns into (keep, static).

    - never populated  -> dropped entirely
    - always identical -> 'static' (written once to a side file)
    - otherwise        -> kept as a CSV column
    """
    cols = list(ALWAYS_KEEP)  # reserved even if unused so far, so that a
    for r in rows:            # late error or static-value change has a home
        for k in r:
            if k not in cols:
                cols.append(k)
    keep, static = [], {}
    for c in cols:
        if c in ALWAYS_KEEP:
            keep.append(c)
            continue
        vals = [r.get(c, "") for r in rows]
        nonempty = [v for v in vals if v not in ("", None)]
        if not nonempty:
            continue  # never obtained
        if len(nonempty) == len(vals) and len({str(v) for v in nonempty}) == 1:
            static[c] = nonempty[0]
            continue
        keep.append(c)
    return keep, static


class Collector:
    name = "collector"
    outdir = Path(".")  # set by run() before setup(); extra output files go here
    compact = True  # drop never-populated and constant columns from the CSV
    learn_seconds = LEARN_SECONDS

    async def sample(self) -> dict:
        """Return one flat dict of telemetry values. Raise on failure."""
        raise NotImplementedError

    async def setup(self):
        pass

    async def teardown(self):
        pass

    def _compact_row(self, row, keep, static):
        """Project a row onto the kept columns without losing information.

        Values for dropped columns are not silently discarded: errors are
        merged into 'error' and any change to a supposedly-static field is
        recorded in 'notes'.
        """
        out = {k: row.get(k, "") for k in keep}
        extra_err, changed = [], []
        for k, v in row.items():
            if k in out or v in ("", None):
                continue
            if k in static:
                if str(v) != str(static[k]):
                    changed.append(f"{k}={v}")
            elif k.endswith("error"):
                extra_err.append(f"{k}: {v}")
        if extra_err:
            out["error"] = "; ".join(filter(None, [out.get("error", "")] + extra_err))
        if changed:
            out["notes"] = "; ".join(filter(None, [out.get("notes", "")] + changed))
        return out

    async def run(self, csv_path, stop: asyncio.Event, interval: float = 1.0):
        logger = CsvLogger(csv_path)
        self.outdir = Path(csv_path).parent
        learn_buf = []  # samples held while the column set is decided
        keep = static = None
        errors = 0
        deadline = time.time() + self.learn_seconds

        def flush_learned():
            nonlocal keep, static
            keep, static = select_columns(learn_buf)
            if static:
                stem = Path(csv_path).name
                for ext in (".gz", ".csv"):
                    if stem.endswith(ext):
                        stem = stem[: -len(ext)]
                path = Path(csv_path).with_name(stem + "_static.json")
                path.write_text(
                    json.dumps(static, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(
                    f"[{self.name}] CSV列を {len(keep)} 列に圧縮しました"
                    f"(固定値 {len(static)} 項目は {path.name} に保存、"
                    "未取得の項目は省略)"
                )
            for r in learn_buf:
                logger.write_row(self._compact_row(r, keep, static))
            learn_buf.clear()

        try:
            await self.setup()
            next_t = int(time.time()) + 1
            while not stop.is_set():
                await asyncio.sleep(max(0.0, next_t - time.time()))
                # a slow or failing sample can put us behind by many ticks;
                # skip the missed ones instead of firing them back to back
                step = interval
                for threshold, slowed in BACKOFF_STEPS:
                    if errors >= threshold:
                        step = max(step, slowed)
                next_t += step
                now = time.time()
                if next_t <= now:
                    next_t = int(now) + step

                row = {"timestamp_utc": utc_now_iso(), "epoch": round(epoch_now(), 3)}
                try:
                    data = await self.sample()
                    if errors:
                        print(f"[{self.name}] 取得を再開しました({errors}回の失敗後)")
                    row["error"] = ""
                    row.update(data)
                    errors = 0
                except Exception as e:
                    errors += 1
                    row["error"] = compact_error(e)
                    if errors in (1, 5, 30) or errors % 120 == 0:
                        print(
                            f"[{self.name}] 取得失敗 {errors} 回目: {row['error']}"
                        )
                        if errors == 5:
                            print(
                                f"[{self.name}] 失敗が続くため取得間隔を"
                                "自動的に広げます(復旧すれば元に戻ります)"
                            )

                if not self.compact:
                    logger.write_row(row)
                    continue
                if keep is None:
                    learn_buf.append(row)
                    # decide once the window elapses and real data exists
                    if time.time() >= deadline and any(
                        r.get("error") == "" for r in learn_buf
                    ):
                        flush_learned()
                    continue
                logger.write_row(self._compact_row(row, keep, static))
        finally:
            if learn_buf:
                if any(r.get("error") == "" for r in learn_buf):
                    flush_learned()
                else:  # never got a successful sample: log what we have
                    for r in learn_buf:
                        logger.write_row(r)
            logger.close()
            await self.teardown()
