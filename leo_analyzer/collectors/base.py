"""Base class for 1 Hz antenna telemetry collectors."""

import asyncio
import time
from pathlib import Path

from ..util import CsvLogger, epoch_now, utc_now_iso


class Collector:
    name = "collector"
    outdir = Path(".")  # set by run() before setup(); extra output files go here

    async def sample(self) -> dict:
        """Return one flat dict of telemetry values. Raise on failure."""
        raise NotImplementedError

    async def setup(self):
        pass

    async def teardown(self):
        pass

    async def run(self, csv_path, stop: asyncio.Event, interval: float = 1.0):
        logger = CsvLogger(csv_path)
        self.outdir = Path(csv_path).parent
        pending = []  # error rows seen before the column set is known
        header_ready = False
        errors = 0
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
                    if not header_ready:
                        # Columns are fixed by the first successful sample;
                        # hold error-only rows until then.
                        pending.append(row)
                        continue
                if not header_ready:
                    header_ready = True
                    first = row
                    for p in pending:
                        logger.write_row({**{k: "" for k in first}, **p})
                        # first write_row call fixed the fields from `first`'s
                        # key set, so pad pending rows to that shape
                    pending.clear()
                logger.write_row(row)
        finally:
            if pending:
                # never got a successful sample: log error rows as-is
                for p in pending:
                    logger.write_row(p)
            logger.close()
            await self.teardown()
