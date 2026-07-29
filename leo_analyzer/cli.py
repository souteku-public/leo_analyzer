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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def ensure_deps():
    """Friendly first-run experience: offer to pip-install requirements."""
    try:
        import aiohttp  # noqa: F401

        return
    except ImportError:
        pass
    print("必要なライブラリ(aiohttp など)がまだインストールされていません。")
    req = Path(__file__).resolve().parent.parent / "requirements.txt"
    if sys.stdin.isatty():
        try:
            ans = input("今すぐ自動インストールしますか? [Y/n]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("", "y", "yes"):
            if req.exists():
                cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
            else:
                cmd = [sys.executable, "-m", "pip", "install", "aiohttp", "PyYAML"]
            print("実行中: " + " ".join(cmd))
            if subprocess.call(cmd) == 0:
                print("\nインストール完了。続行します。\n")
                return
            print("\nインストールに失敗しました。")
    print("次のコマンドを実行してから再度お試しください:")
    print("  python -m pip install -r requirements.txt")
    sys.exit(1)


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
        "--kymeta-array-interval",
        type=float,
        metavar="SEC",
        help="seconds between spectrum (adc-data) fetches; larger values "
        "save a lot of disk on long runs (default: 5)",
    )
    p.add_argument(
        "--gzip-csv",
        action="store_true",
        help="write CSVs gzip-compressed (.csv.gz, roughly 1/10 the size); "
        "reports read either form, Excel needs them unzipped first",
    )
    p.add_argument(
        "--keep-all-columns",
        action="store_true",
        help="write every telemetry field to the CSV, including fields "
        "that never change or are never populated (default: compacted)",
    )
    p.add_argument(
        "--starlink-transport",
        choices=["auto", "grpc", "pure"],
        default="auto",
        help="how to reach the dish: auto (grpcio, falling back to the "
        "pure-Python HTTP/2 client), or force one of them",
    )
    p.add_argument(
        "--starlink-probe",
        action="store_true",
        help="diagnose the Starlink dish connection step by step "
        "(TCP, gRPC reflection, get_status) and exit",
    )
    p.add_argument(
        "--kymeta-full",
        action="store_true",
        help="with --kymeta-probe, print every column instead of the first 40",
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
        "--compare",
        nargs="+",
        metavar="DIR",
        help="build compare.html overlaying the common metrics of several "
        "runs (e.g. Starlink vs Kymeta) on one time axis, then exit",
    )
    p.add_argument(
        "--shrink-csv",
        metavar="FILE",
        help="rewrite an oversized telemetry CSV, moving huge cells "
        "(embedded spectrum data) into a companion .jsonl.gz, then exit",
    )
    p.add_argument(
        "--measure",
        choices=["full", "rtt", "none"],
        default="full",
        help="what to measure alongside the antenna telemetry: "
        "'full' = max throughput + RTT (saturates the link), "
        "'rtt' = latency only, no load (long drives, metered plans), "
        "'none' = telemetry only (default: full)",
    )
    p.add_argument(
        "--collect-only",
        action="store_true",
        help="alias for --measure none (kept for compatibility)",
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
                    "error: --collect starlink requires protobuf and h2 "
                    f"(python -m pip install -r requirements.txt): {e}"
                )
            c = StarlinkCollector(
                addr=args.starlink_addr, transport=args.starlink_transport
            )
            c.compact = not args.keep_all_columns
            collectors.append(c)
        elif name == "kymeta":
            collectors.append(make_kymeta_collector(args))
    return collectors


def preflight(args, collectors):
    """Warn loudly before a long run if an antenna is unreachable."""
    for c in collectors:
        if c.name != "starlink":
            continue
        from .collectors.starlink import check_tcp

        ok, err = check_tcp(c.addr)
        if ok:
            print(f"[starlink] {c.addr} に到達できます")
            continue
        print(
            "\n" + "!" * 62 + "\n"
            f"警告: Starlink アンテナ({c.addr})に接続できません\n"
            f"  {err}\n"
            "  このまま測定を続けるとアンテナ情報は記録されません"
            "(速度測定は実行されます)。\n"
            "  ・PCがStarlinkのLANに接続されているか\n"
            "  ・ブラウザで http://192.168.100.1 が開けるか\n"
            "  ・OneWeb(SSM)も 192.168.100.1 を使うため、両方に同時接続して"
            "いないか\n"
            "  詳しい切り分けは python -m leo_analyzer --starlink-probe\n"
            + "!" * 62 + "\n"
        )


def make_kymeta_collector(args):
    from .collectors.kymeta import KymetaCollector, default_config

    cfg = default_config()  # 192.168.44.2, admin, auto-discovery
    if args.kymeta_config:
        import yaml

        with open(args.kymeta_config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    if args.kymeta_array_interval is not None:
        cfg["array_interval"] = args.kymeta_array_interval
    if args.keep_all_columns:
        cfg["compact"] = False
    return KymetaCollector(cfg)


def starlink_probe(args):
    print(f"Starlink ディッシュ診断: {args.starlink_addr}\n")
    try:
        from .collectors.starlink import StarlinkCollector
    except ImportError as e:
        print(f"  [NG ] 必要なライブラリが未インストール: {e}")
        print("\n  対処: python -m pip install -r requirements.txt")
        sys.exit(1)

    collector = StarlinkCollector(
        addr=args.starlink_addr, transport=args.starlink_transport
    )
    try:
        report = collector.diagnose()
    finally:
        collector._close()

    print(f"\n結論: {report.get('conclusion', '不明')}")
    sample = report.get("sample")
    if sample:
        out = Path(
            args.outdir if args.outdir != "results" else "."
        ) / "starlink_sample.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2, ensure_ascii=False, default=str)
        keys = list(sample)
        for k in keys[:30]:
            print(f"  {k} = {sample[k]}")
        if len(keys) > 30:
            print(f"  ... 他 {len(keys) - 30} 項目")
        print(f"\n1サンプル分の全データを保存しました: {out}")
    else:
        sys.exit(1)


async def kymeta_probe(args):
    collector = make_kymeta_collector(args)
    collector.endpoints = []  # force discovery even if the config lists paths
    try:
        await collector.setup()
        sample = await collector.sample()
    except Exception as e:
        print("\n=== 探索の診断ログ ===")
        for line in collector.probe_log:
            print(" ", line)
        print(f"\n探索失敗: {e}")
        print(
            "\n次のステップ:\n"
            "  1) このPCのブラウザで " + collector.base_url + " が開けるか確認\n"
            "     開けない場合はネットワーク(接続ポート/VLAN)の問題です\n"
            "  2) 開ける場合は F12 -> ネットワーク -> Fetch/XHR で、"
            "GUIが定期取得しているURLを確認\n"
            "     (plots/spectrumページはWSタブも確認)\n"
            "  3) 見つけたURLを config/kymeta.yaml の endpoints / "
            "stream_endpoints に記入して再実行"
        )
        sys.exit(1)
    finally:
        await collector.teardown()

    print("\n=== 探索の診断ログ ===")
    for line in collector.probe_log:
        print(" ", line)

    print(f"\nprobed {collector.base_url}: {len(collector.endpoints)} endpoint(s)")
    print(f"columns per sample: {len(sample)}")

    dump = Path(args.outdir if args.outdir != "results" else ".") / "kymeta_sample.json"
    dump.parent.mkdir(parents=True, exist_ok=True)
    with open(dump, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2, ensure_ascii=False, default=str)

    keys = [k for k in sample if not k.endswith("._error")]
    if args.kymeta_full:
        for k in keys:
            print(f"  {k} = {sample[k]}")
    else:
        for k in keys[:40]:
            print(f"  {k} = {sample[k]}")
        if len(keys) > 40:
            print(f"  ... 他 {len(keys) - 40} 列(全項目は --kymeta-full で表示)")
    print(f"\n1サンプル分の全データを保存しました: {dump}")

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
    if args.shrink_csv:
        from .shrink import shrink_csv

        slim, bulk, st = shrink_csv(args.shrink_csv)
        print(f"元ファイル: {args.shrink_csv}")
        print(f"  {st['rows']:,} 行を処理、{st['moved_cells']:,} セルを分離")
        for col, size in sorted(
            st["bulk_columns"].items(), key=lambda kv: -kv[1]
        ):
            print(f"    {col}: {size / 1e6:.1f} MB")
        print(f"軽量CSV : {slim}  ({st['slim_bytes'] / 1e6:.1f} MB)")
        print(f"分離データ: {bulk}  ({st['bulk_bytes'] / 1e6:.1f} MB)")
        return

    if args.compare:
        from .report import generate_compare_report

        out = generate_compare_report(args.compare)
        print(f"comparison report written: {out}")
        return

    if args.report:
        from .report import generate_report

        out = generate_report(args.report)
        print(f"report written: {out}")
        return

    if args.starlink_probe:
        starlink_probe(args)
        return

    if args.kymeta_probe:
        await kymeta_probe(args)
        return

    mode = "none" if args.collect_only else args.measure

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rundir = Path(args.outdir) / f"{stamp}_{args.label}"
    rundir.mkdir(parents=True, exist_ok=True)
    print(f"output directory: {rundir}")
    print(
        "測定モード: "
        + {
            "full": "最大スループット + RTT(回線を飽和させます)",
            "rtt": "RTTのみ(回線に負荷をかけません)",
            "none": "アンテナ情報のみ",
        }[mode]
    )

    suffix = ".csv.gz" if args.gzip_csv else ".csv"
    stop = asyncio.Event()
    collectors = make_collectors(args)
    preflight(args, collectors)
    collector_tasks = [
        asyncio.create_task(c.run(rundir / f"{c.name}_status{suffix}", stop))
        for c in collectors
    ]

    from . import build_id

    summary = {
        "label": args.label,
        "tool_version": build_id(),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "duration_per_direction_s": args.duration,
        "baseline_s": args.baseline,
        "streams": args.streams,
        "direction": args.direction,
        "measure_mode": mode,
        "collectors": [c.name for c in collectors],
    }

    try:
        if mode == "none":
            if not collectors:
                sys.exit(
                    "error: --measure none / --collect-only requires "
                    "at least one --collect"
                )
            await asyncio.sleep(args.duration)
        else:
            from .speedtest import run_speedtest

            result = await run_speedtest(
                rundir / f"throughput{suffix}",
                duration=args.duration,
                streams=args.streams,
                direction=args.direction,
                baseline=args.baseline,
                mode=mode,
            )
            summary.update(result)
    except KeyboardInterrupt:
        print("interrupted; finalizing logs...")
    finally:
        stop.set()
        if collector_tasks:
            await asyncio.gather(*collector_tasks, return_exceptions=True)
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        files = sorted(
            ((p.name, p.stat().st_size) for p in rundir.iterdir() if p.is_file()),
            key=lambda kv: -kv[1],
        )
        summary["output_files"] = {n: f"{s / 1e6:.1f} MB" for n, s in files}
        with open(rundir / "summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        total = sum(s for _, s in files)
        if total > 200e6:
            print(f"\n出力ファイル合計 {total / 1e9:.2f} GB:")
            for n, s in files[:5]:
                print(f"  {s / 1e6:9.1f} MB  {n}")

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

    print("\n測定内容を選んでください:")
    print("  1) 最大スループット + RTT  (回線を飽和させます。通信量大)")
    print("  2) RTTのみ                 (回線に負荷をかけません。長時間向き)")
    print("  3) アンテナ情報のみ        (通信なし)")
    mode = {"1": "full", "2": "rtt", "3": "none"}.get(_ask("番号を入力", "1"), "full")
    argv += ["--measure", mode]

    if mode == "full":
        print("\n測定時間(下り・上りそれぞれ)。例: 300、10m(10分)、1h(1時間)")
    else:
        print("\n測定時間。例: 300、10m(10分)、1h(1時間)")
    duration = _ask("測定時間", "10m")
    argv += ["--duration", duration]

    if mode == "full":
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
    ensure_deps()
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty():
        argv = interactive_args()
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
