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


def parse_duration(value, minimum: int = 1) -> int:
    """'300' -> 300, '90s' -> 90, '10m' -> 600, '1.5h' -> 5400."""
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    factor = 1
    if s and s[-1] in units:
        factor = units[s[-1]]
        s = s[:-1]
    try:
        seconds = int(float(s) * factor)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid duration: {value!r} (examples: 300, 90s, 10m, 1h)"
        )
    if seconds < minimum:
        raise argparse.ArgumentTypeError(
            f"duration must be at least {minimum} second(s)"
        )
    return seconds


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
        type=parse_duration,
        default="300",
        metavar="TIME",
        help="measurement time per direction: seconds, or with a unit "
        "like 90s / 10m / 1h (default: 300 = 5 minutes)",
    )
    p.add_argument(
        "--baseline",
        type=lambda v: parse_duration(v, minimum=0),
        default="5m",
        metavar="TIME",
        help="idle RTT baseline before the first transfer and between "
        "directions, latency probe only (default: 5m; 0 to disable). "
        "Unreachable seconds (handovers) are excluded from the stats "
        "and counted separately",
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
        help="YAML config for the Kymeta collector "
        "(optional; defaults to https://192.168.44.2 with the factory admin "
        "login and endpoint auto-discovery — see config/kymeta.example.yaml)",
    )
    p.add_argument(
        "--kymeta-probe",
        action="store_true",
        help="probe the Kymeta antenna for JSON endpoints, print what was "
        "found (with a ready-to-use YAML snippet) and exit",
    )
    p.add_argument(
        "--report",
        metavar="DIR",
        help="regenerate report.html for an existing results directory "
        "and exit (reports are also generated automatically after each run)",
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
            collectors.append(make_kymeta_collector(args))
    return collectors


def make_kymeta_collector(args):
    from .collectors.kymeta import KymetaCollector

    cfg = None  # None -> built-in defaults (192.168.44.2, admin, discovery)
    if args.kymeta_config:
        import yaml

        with open(args.kymeta_config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    return KymetaCollector(cfg)


async def kymeta_probe(args):
    collector = make_kymeta_collector(args)
    collector.endpoints = []  # force discovery even if the config lists paths
    try:
        await collector.setup()
        sample = await collector.sample()
    finally:
        await collector.teardown()

    print(f"\nprobed {collector.base_url}: {len(collector.endpoints)} endpoint(s)")
    print(f"columns per sample: {len(sample)}")
    shown = [k for k in sample if not k.endswith("._error")][:40]
    for k in shown:
        print(f"  {k} = {sample[k]}")
    if len(sample) > len(shown):
        print(f"  ... and {len(sample) - len(shown)} more columns")

    print("\n# config/kymeta.yaml snippet for these endpoints:")
    print(f'base_url: "{collector.base_url}"')
    print("verify_ssl: false")
    print("auth:")
    print(f'  type: {collector.auth_cfg.get("type", "basic")}')
    print(f'  username: "{collector.auth_cfg.get("username", "")}"')
    print(f'  password: "{collector.auth_cfg.get("password", "")}"')
    print("endpoints:")
    for ep in collector.endpoints:
        print(f'  - name: {ep["name"]}')
        print(f'    path: "{ep["path"]}"')


async def run(args):
    if args.report:
        from .report import generate_report

        out = generate_report(args.report)
        print(f"report written: {out}")
        return

    if args.kymeta_probe:
        await kymeta_probe(args)
        return

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
        "baseline_s": args.baseline,
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
                baseline=args.baseline,
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

    try:
        from .report import generate_report

        report_path = generate_report(rundir)
        print(f"\ngraph report: {report_path}  (ブラウザで開けます)")
    except Exception as e:
        print(f"\nreport generation failed: {e}")
    print(f"files written to {rundir}/")


def _ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def interactive_args():
    """Japanese Q&A wizard for users who start the tool with no arguments."""
    print("=" * 60)
    print(" LEO回線 スループット測定ツール")
    print("=" * 60)
    print("そのままEnterを押すと [ ] 内の既定値が使われます。\n")

    print("測定する回線を選んでください:")
    print("  1) Starlink        (アンテナ情報も同時記録)")
    print("  2) OneWeb Kymeta   (アンテナ情報も同時記録)")
    print("  3) OneWeb Intellian / その他 (速度測定のみ)")
    choice = _ask("番号を入力", "3")

    argv = []
    if choice == "1":
        argv += ["--label", "starlink", "--collect", "starlink"]
    elif choice == "2":
        argv += ["--label", "oneweb_kymeta", "--collect", "kymeta"]
    else:
        argv += ["--label", "measure"]

    print("\n測定時間(下り・上りそれぞれ)。例: 300、10m(10分)、1h(1時間)")
    duration = _ask("測定時間", "10m")
    argv += ["--duration", duration]

    print("\n無負荷でのRTT測定時間(測定前と下り→上りの間の2回)。0で省略")
    baseline = _ask("アイドルRTT測定時間", "5m")
    argv += ["--baseline", baseline]

    print("\n測定方向: both=下り→上りの順に両方 / down=下りのみ / up=上りのみ")
    direction = _ask("方向", "both")
    argv += ["--direction", direction]

    print("\n次のコマンドと同じ内容で実行します:")
    print(f"  python -m leo_analyzer {' '.join(argv)}\n")
    return argv


def main(argv=None):
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty():
        argv = interactive_args()
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
