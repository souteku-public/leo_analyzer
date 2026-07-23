"""Cloudflare-based throughput measurement with 1-second resolution.

Saturates the link with N parallel HTTP streams against
speed.cloudflare.com and samples transferred bytes every second.
A separate lightweight probe measures loaded latency once per second.
"""

import asyncio
import os
import time

import aiohttp

from .util import CsvLogger, summarize, utc_now_iso

DOWN_URL = "https://speed.cloudflare.com/__down?bytes={size}"
UP_URL = "https://speed.cloudflare.com/__up"
PROBE_URL = "https://speed.cloudflare.com/__down?bytes=0"

DOWN_REQUEST_BYTES = 50 * 1024 * 1024  # per-GET payload, re-requested until stop
DOWN_REQUEST_MIN = 4 * 1024 * 1024  # floor when shrinking after HTTP errors
UP_REQUEST_BYTES = 50 * 1024 * 1024  # per-POST payload, re-posted until stop
CHUNK = 64 * 1024


class _State:
    def __init__(self):
        self.bytes_down = 0
        self.bytes_up = 0
        self.latency_ms = None
        self.phase = "idle"
        self.down_request_bytes = DOWN_REQUEST_BYTES


async def _download_worker(session, state, stop):
    while not stop.is_set():
        try:
            url = DOWN_URL.format(size=state.down_request_bytes)
            async with session.get(url) as resp:
                if resp.status >= 400:
                    # some paths (proxies/CDN policy) reject large payloads;
                    # shrink the per-request size and retry
                    state.down_request_bytes = max(
                        state.down_request_bytes // 2, DOWN_REQUEST_MIN
                    )
                    await asyncio.sleep(0.2)
                    continue
                async for chunk in resp.content.iter_chunked(CHUNK):
                    state.bytes_down += len(chunk)
                    if stop.is_set():
                        return
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(0.5)


async def _upload_worker(session, state, stop):
    payload = os.urandom(CHUNK)

    async def body():
        sent = 0
        while sent < UP_REQUEST_BYTES and not stop.is_set():
            state.bytes_up += len(payload)
            sent += len(payload)
            yield payload

    while not stop.is_set():
        try:
            async with session.post(UP_URL, data=body()) as resp:
                await resp.read()
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(0.5)


async def _latency_probe(session, state, stop):
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            async with session.get(PROBE_URL) as resp:
                await resp.read()
            state.latency_ms = (time.perf_counter() - t0) * 1000.0
        except asyncio.CancelledError:
            return
        except Exception:
            state.latency_ms = None
        await asyncio.sleep(max(0.0, 1.0 - (time.perf_counter() - t0)))


async def _sampler(state, logger, stop, results):
    prev_down = state.bytes_down
    prev_up = state.bytes_up
    next_t = int(time.time()) + 1
    while not stop.is_set():
        await asyncio.sleep(max(0.0, next_t - time.time()))
        next_t += 1
        d, u = state.bytes_down, state.bytes_up
        mbps_down = (d - prev_down) * 8 / 1e6
        mbps_up = (u - prev_up) * 8 / 1e6
        prev_down, prev_up = d, u
        lat = state.latency_ms
        row = {
            "timestamp_utc": utc_now_iso(),
            "epoch": round(time.time(), 3),
            "phase": state.phase,
            "mbps_down": round(mbps_down, 3),
            "mbps_up": round(mbps_up, 3),
            "latency_ms": round(lat, 1) if lat is not None else "",
        }
        logger.write_row(row)
        if state.phase in results:
            key = "mbps_down" if state.phase == "download" else "mbps_up"
            results[state.phase].append(row[key])
        print(
            f"[{row['timestamp_utc']}] {state.phase:>8} "
            f"down={mbps_down:8.2f} Mbps  up={mbps_up:7.2f} Mbps  "
            f"rtt={row['latency_ms'] or '-'} ms"
        )


async def run_speedtest(
    csv_path,
    duration: int = 60,
    streams: int = 8,
    direction: str = "both",
) -> dict:
    """Run download/upload phases; returns per-phase summary stats."""
    state = _State()
    stop_all = asyncio.Event()
    logger = CsvLogger(csv_path)
    results = {"download": [], "upload": []}

    connector = aiohttp.TCPConnector(limit=streams * 2 + 4, ssl=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, trust_env=True
    ) as session:
        sampler_task = asyncio.create_task(_sampler(state, logger, stop_all, results))
        probe_task = asyncio.create_task(_latency_probe(session, state, stop_all))

        phases = []
        if direction in ("both", "down"):
            phases.append(("download", _download_worker))
        if direction in ("both", "up"):
            phases.append(("upload", _upload_worker))

        try:
            for phase_name, worker in phases:
                state.phase = phase_name
                phase_stop = asyncio.Event()
                workers = [
                    asyncio.create_task(worker(session, state, phase_stop))
                    for _ in range(streams)
                ]
                await asyncio.sleep(duration)
                phase_stop.set()
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        finally:
            state.phase = "idle"
            stop_all.set()
            sampler_task.cancel()
            probe_task.cancel()
            await asyncio.gather(sampler_task, probe_task, return_exceptions=True)
            logger.close()

    summary = {}
    if results["download"]:
        summary["download_mbps"] = summarize(results["download"])
    if results["upload"]:
        summary["upload_mbps"] = summarize(results["upload"])
    return summary
