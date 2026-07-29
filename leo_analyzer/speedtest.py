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

# each latency probe gets its own deadline so an outage (e.g. satellite
# handover) is detected within a few seconds instead of hanging
PROBE_TIMEOUT_S = 4.0
# a probe result older than this is treated as "no measurement" for that
# second, so stale values never mask an outage
PROBE_STALE_S = 3.0


class _State:
    def __init__(self):
        self.bytes_down = 0
        self.bytes_up = 0
        self.latency_ms = None
        self.latency_ts = None  # perf_counter of the last probe verdict
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
    probe_timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S)
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            async with session.get(PROBE_URL, timeout=probe_timeout) as resp:
                await resp.read()
            state.latency_ms = (time.perf_counter() - t0) * 1000.0
        except asyncio.CancelledError:
            return
        except Exception:
            state.latency_ms = None  # unreachable this round (e.g. handover)
        state.latency_ts = time.perf_counter()
        await asyncio.sleep(max(0.0, 1.0 - (time.perf_counter() - t0)))


async def _sampler(state, logger, stop, results, latencies, lat_missing):
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
        if lat is not None and (
            state.latency_ts is None
            or time.perf_counter() - state.latency_ts > PROBE_STALE_S
        ):
            lat = None  # probe is hanging: treat this second as unreachable
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
        if state.phase in latencies:
            if lat is not None:
                latencies[state.phase].append(lat)
            else:
                lat_missing[state.phase] += 1
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
    baseline: int = 15,
    mode: str = "full",
) -> dict:
    """Run baseline/download/upload phases; returns per-phase summary stats.

    `baseline` seconds of latency-only probing (no load) run before the
    first transfer phase and between download and upload, so each run
    records idle RTT alongside loaded RTT. Set 0 to skip.

    mode="rtt" skips the transfers entirely and probes latency for
    `duration` seconds, which keeps the link unloaded — the right choice
    for long drives where saturating the link would burn the data plan.
    """
    state = _State()
    stop_all = asyncio.Event()
    logger = CsvLogger(csv_path)
    results = {"download": [], "upload": []}
    latencies = {"baseline": [], "download": [], "upload": []}
    lat_missing = {"baseline": 0, "download": 0, "upload": 0}

    connector = aiohttp.TCPConnector(limit=streams * 2 + 4, ssl=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, trust_env=True
    ) as session:
        # warm up the probe connection (TCP+TLS handshake) so the first
        # baseline sample measures RTT, not connection setup
        try:
            async with session.get(PROBE_URL) as resp:
                await resp.read()
        except Exception:
            pass

        sampler_task = asyncio.create_task(
            _sampler(state, logger, stop_all, results, latencies, lat_missing)
        )
        probe_task = asyncio.create_task(_latency_probe(session, state, stop_all))

        phases = []
        if mode == "rtt":
            # latency only, for the whole requested duration
            phases.append(("baseline", None, duration))
        else:
            if baseline > 0:
                phases.append(("baseline", None, baseline))
            if direction in ("both", "down"):
                phases.append(("download", _download_worker, duration))
            if direction == "both" and baseline > 0:
                phases.append(("baseline", None, baseline))
            if direction in ("both", "up"):
                phases.append(("upload", _upload_worker, duration))

        try:
            for phase_name, worker, phase_seconds in phases:
                state.phase = phase_name
                if worker is None:  # latency-only baseline, no load
                    await asyncio.sleep(phase_seconds)
                    continue
                phase_stop = asyncio.Event()
                workers = [
                    asyncio.create_task(worker(session, state, phase_stop))
                    for _ in range(streams)
                ]
                await asyncio.sleep(phase_seconds)
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
    lat = {}
    for phase, key in (
        ("baseline", "idle"),
        ("download", "download_loaded"),
        ("upload", "upload_loaded"),
    ):
        if latencies[phase] or lat_missing[phase]:
            stats = summarize(latencies[phase])
            # seconds with no reachable probe (e.g. satellite handover);
            # these are excluded from avg/min/max above
            stats["unreachable_s"] = lat_missing[phase]
            lat[key] = stats
    if lat:
        summary["latency_ms"] = lat
    return summary
