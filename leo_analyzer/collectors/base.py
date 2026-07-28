"""Base class for 1 Hz antenna telemetry collectors."""

import asyncio
import json
import time
from pathlib import Path

from ..util import CsvLogger, epoch_now, utc_now_iso

# Columns are chosen after watching this many seconds of samples, so that
# fields which never arrive (or never change) can be left out of the CSV.
LEARN_SECONDS = 90
ALWAYS_KEEP = ("timestamp_utc", "epoch", "error", "notes")


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
                next_t += interval
                row = {"timestamp_utc": utc_now_iso(), "epoch": round(epoch_now(), 3)}
                try:
                    data = await self.sample()
                    row["error"] = ""
                    row.update(data)
                    errors = 0
                except Exception as e:
                    errors += 1
                    row["error"] = f"{type(e).__name__}: {e}"
                    if errors in (1, 10) or errors % 60 == 0:
                        print(f"[{self.name}] sample failed ({errors}x): {e}")

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
