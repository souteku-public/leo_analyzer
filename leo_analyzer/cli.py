"""Command-line entry point.

Example:
    python -m leo_analyzer --label starlink_mini --duration 60 \
        --collect starlink
    python -m leo_analyzer --label oneweb_kymeta --duration 60 \
        --collect kymeta --kymeta-config config/kymeta.yaml
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .speedtest import run_speedtest


def build_parser():
    p = argparse.ArgumentParser(
        prog="leo_analyzer",
        description=(
            "LEO link throughput measurement (Cloudflare, 1-second resolution) "
            "with simultaneous antenna telemetry logging"
        ),
    )
    p.add_argument(
        "--label",
        default="run",
        help="run label used in the output directory name (e.g. starlink_mini)",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=60,
        help="seconds per direction (default: 60)",
    )
    p.add_argument(
        "--streams",
        type=int,
        default=8,
        help="number of parallel HTTP streams (default: 8)",
    )
    p.add_argument(
        "--direction",
        choices=["both", "down", "up"],
        default="both",
        help="which direction(s) to measure (default: both)",
    )
    p.add_argument(
        "--outdir",
        default="results",
        help="base output directory (default: results/)",
    )
    p.add_argument(
        "--collect",
        action="append",
        choices=["starlink", "kymeta"],
        default=[],
        help="antenna telemetry to log alongside (repeatable)",
    )
    p.add_argument(
        "--starlink-addr",
        default="192.168.100.1:9200",
        help="Starlink dish gRPC address (default: 192.168.100.1:9200)",
    )
    p.add_argument(
        "--kymeta-config",
        help="YAML config for the Kymeta collector (see config/kymeta.example.yaml)",
    )
    p.add_argument(
        "--collect-only",
        action="store_true",
        help="log antenna telemetry without running the speed test "
        "(runs for --duration seconds total)",
    )
    return p


def make_collectors(args):
    collectors = []
    for name in dict.fromkeys(args.collect):  # dedupe, keep order
        if name == "starlink":
            try:
                from .collectors.starlink import StarlinkCollector
            except ImportError as e:
                sys.exit(
                    "error: --collect starlink requires grpcio/protobuf/yagrc "
                    f"(pip install -r requirements.txt): {e}"
                )
            collectors.append(StarlinkCollector(addr=args.starlink_addr))
        elif name == "kymeta":
            if not args.kymeta_config:
                sys.exit("error: --collect kymeta requires --kymeta-config")
            import yaml

            from .collectors.kymeta import KymetaCollector

            with open(args.kymeta_config, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            collectors.append(KymetaCollector(cfg))
    return collectors


async def run(args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rundir = Path(args.outdir) / f"{stamp}_{args.label}"
    rundir.mkdir(parents=True, exist_ok=True)
    print(f"output directory: {rundir}")

    stop = asyncio.Event()
    collectors = make_collectors(args)
    collector_tasks = [
        asyncio.create_task(c.run(rundir / f"{c.name}_status.csv", stop))
        for c in collectors
    ]

    summary = {
        "label": args.label,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "duration_per_direction_s": args.duration,
        "streams": args.streams,
        "direction": args.direction,
        "collectors": [c.name for c in collectors],
    }

    try:
        if args.collect_only:
            if not collectors:
                sys.exit("error: --collect-only requires at least one --collect")
            await asyncio.sleep(args.duration)
        else:
            result = await run_speedtest(
                rundir / "throughput.csv",
                duration=args.duration,
                streams=args.streams,
                direction=args.direction,
            )
            summary.update(result)
    except KeyboardInterrupt:
        print("interrupted; finalizing logs...")
    finally:
        stop.set()
        if collector_tasks:
            await asyncio.gather(*collector_tasks, return_exceptions=True)
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        with open(rundir / "summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

    print("\n=== summary ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nfiles written to {rundir}/")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
