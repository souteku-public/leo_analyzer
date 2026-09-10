"""Which of the RTT analyses a captured run can actually support.

The six checks discussed for the Starlink/OneWeb latency gap each need
particular columns, and whether a column is there is not the same as
whether it holds usable values: a header can be present and empty for
the whole run. So every check is decided on values, over the seconds the
two series actually share, and the numbers behind the verdict are
printed with it - for the cheap ones (idle vs loaded, the RTT floor)
that verdict already carries the answer.

Read-only: nothing here touches the capture.
"""

import csv
import gzip
import io
import json
import math
import re
from pathlib import Path
from unicodedata import east_asian_width

# One canonical name per column we need, with the spellings seen in real
# captures. Anchored at the end because Kymeta prefixes every field with
# the endpoint it came from.
COLUMNS = {
    "epoch": [r"^epoch$"],
    "phase": [r"^phase$"],
    "latency_ms": [r"^latency_ms$"],
    "down_mbps": [r"^mbps_down$"],
    "up_mbps": [r"^mbps_up$"],
    "pop_ping_ms": [r"pop_ping_latency_ms$"],
    "elevation": [r"look[-_ ]?angle\.elevation$"],
    "sinr_db": [r"sinr[_ ]?db$", r"\bsinr$", r"cnr[_ ]?db$"],
}
# Fields that would say which gateway/beam served the terminal. Nothing
# records the IP path, so these are the only route evidence left.
ROUTE_HINTS = re.compile(
    r"gateway|beam|snp|teleport|nms|network[-_ ]?id|pop\b|colo|satellite",
    re.I,
)
IDLE_PHASES = {"idle", "baseline"}
MIN_SAMPLES = 300          # below this a percentile is not worth quoting
MIN_ELEVATION_SPAN = 10.0  # degrees; less than this cannot show a slope


def _open(path: Path):
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8",
                                errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def _num(text):
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _match(header, patterns):
    for pattern in patterns:
        for col in header:
            if re.search(pattern, col, re.I):
                return col
    return None


def _read(path: Path):
    """{canonical name: {epoch: value}} plus the raw header."""
    with _open(path) as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        picked = {k: _match(header, pats) for k, pats in COLUMNS.items()}
        picked = {k: c for k, c in picked.items() if c}
        if "epoch" not in picked:
            return header, {}, 0
        out = {k: {} for k in picked if k != "epoch"}
        rows = 0
        for row in reader:
            rows += 1
            t = _num(row.get(picked["epoch"]))
            if t is None:
                continue
            t = int(t)
            for key, col in picked.items():
                if key == "epoch":
                    continue
                raw = row.get(col)
                if key == "phase":
                    if raw:
                        out[key][t] = raw
                    continue
                v = _num(raw)
                if v is not None:
                    out[key][t] = v
    return header, out, rows


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    i = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[i]


def _gaps(latency, span):
    """Runs of seconds with no RTT, and the first sample after each.

    The probe rides a reused HTTPS connection, so the first measurement
    after an outage pays for a fresh TCP+TLS handshake. Those seconds
    inflate the mean of whichever link dropped out more often.
    """
    blocks, after = 0, []
    in_gap = False
    for t in span:
        if t not in latency:
            in_gap = True
            continue
        if in_gap:
            blocks += 1
            after.append(latency[t])
            in_gap = False
    return blocks, after


def _fmt(v, unit="", digits=1):
    return "—" if v is None else f"{v:.{digits}f}{unit}"


def _width(text):
    """Display columns, counting full-width characters as two."""
    return sum(2 if east_asian_width(c) in "WF" else 1 for c in text)


def _scan_dir(path: Path):
    """Load every CSV in the folder into one view of the run."""
    files, data, headers = [], {}, []
    for f in sorted(path.iterdir()):
        if not re.search(r"\.csv(\.gz)?$", f.name, re.I):
            continue
        header, series, rows = _read(f)
        files.append((f.name, rows))
        headers += header
        for key, values in series.items():
            data.setdefault(key, {}).update(values)
    summary = None
    sj = path / "summary.json"
    if sj.exists():
        try:
            summary = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            summary = None
    static = {}
    for name in ("kymeta_status_static.json", "starlink_status_static.json"):
        sp = path / name
        if sp.exists():
            try:
                static.update(json.loads(sp.read_text(encoding="utf-8")))
            except Exception:
                pass
    return files, data, headers, summary, static


def check_run(path: Path):
    """Feasibility of each analysis for one measurement folder."""
    files, data, headers, summary, static = _scan_dir(path)
    lat = data.get("latency_ms", {})
    phase = data.get("phase", {})
    lines = []
    print(f"\n=== {path}")
    if not files:
        print("  CSVが見つかりません")
        return
    print("  ファイル: "
          + " / ".join(f"{n}({r}行)" for n, r in files)
          + (" / summary.json" if summary else ""))
    if not lat:
        print("  latency_ms 列がありません — RTTの検証はできません "
              "(--measure none で取った測定の可能性)")
        return

    span = range(min(lat), max(lat) + 1)
    values = list(lat.values())

    # (1) idle vs loaded
    by_phase = {}
    for t, name in phase.items():
        if t in lat:
            by_phase.setdefault(name, []).append(lat[t])
    idle = [v for name, vs in by_phase.items() if name in IDLE_PHASES for v in vs]
    loaded = {n: vs for n, vs in by_phase.items() if n not in IDLE_PHASES}
    ok1 = bool(idle) and bool(loaded)
    detail = " / ".join(
        f"{name} {len(vs)}秒 中央値 {_fmt(_percentile(vs, .5), 'ms')}"
        for name, vs in sorted(by_phase.items())) or "phase列なし"
    lines.append(("①無負荷 vs 負荷", ok1, detail if ok1 else
                  ("phase列に idle が無く、負荷の有無を分けられません "
                   "(--baseline 0 で測ると無くなります)" if phase
                   else "phase列がありません")))

    # (2) RTT floor
    ok2 = len(values) >= MIN_SAMPLES
    lines.append(("②RTTのfloor", ok2,
                  f"有効 {len(values)}秒 / 最小 {_fmt(min(values), 'ms')} "
                  f"/ 下位5% {_fmt(_percentile(values, .05), 'ms')} "
                  f"/ 中央値 {_fmt(_percentile(values, .5), 'ms')}"
                  if ok2 else f"有効 {len(values)}秒しかありません"
                              f"(目安 {MIN_SAMPLES}秒以上)"))

    # (3) elevation vs RTT
    el = data.get("elevation", {})
    common = [t for t in el if t in lat]
    span_deg = (max(el[t] for t in common) - min(el[t] for t in common)
                if common else 0.0)
    ok3 = len(common) >= MIN_SAMPLES and span_deg >= MIN_ELEVATION_SPAN
    if not el:
        why = "look-angle.elevation がありません(Kymetaのみ。Starlinkは設置姿勢しか出しません)"
    elif len(common) < MIN_SAMPLES:
        why = f"RTTと仰角が揃う秒が {len(common)}秒 しかありません"
    else:
        why = (f"揃う秒 {len(common)} / 仰角 {span_deg:.1f}° の幅"
               + ("" if ok3 else f" — {MIN_ELEVATION_SPAN}°未満では傾きが出ません"))
    lines.append(("③仰角との相関", ok3, why))

    # (4) reconnect cost after an outage
    blocks, after = _gaps(lat, span)
    ok4 = blocks >= 5
    median = _percentile(values, .5)
    ratio = (_percentile(after, .5) / median
             if after and median else None)
    lines.append(("④切断直後の再接続コスト", ok4,
                  f"欠測ブロック {blocks}個 / 直後の中央値 "
                  f"{_fmt(_percentile(after, .5), 'ms')}"
                  + (f"(全体の {ratio:.1f}倍)" if ratio else "")
                  if ok4 else f"欠測ブロックが {blocks}個 しかありません"
                              "(切断が少ない = この効果は無視できます)"))

    # (5) segment split, Starlink only
    pop = data.get("pop_ping_ms", {})
    pop_common = [t for t in pop if t in lat]
    ok5 = len(pop_common) >= MIN_SAMPLES
    if ok5:
        beyond = [lat[t] - pop[t] for t in pop_common]
        why = (f"揃う秒 {len(pop_common)} / PoPまで 中央値 "
               f"{_fmt(_percentile([pop[t] for t in pop_common], .5), 'ms')}"
               f" / PoPから先 中央値 {_fmt(_percentile(beyond, .5), 'ms')}")
    else:
        why = ("pop_ping_latency_ms がありません(Starlinkのみ)"
               if not pop else f"揃う秒が {len(pop_common)}秒 しかありません")
    lines.append(("⑤区間分解(衛星区間/地上区間)", ok5, why))

    # (6) SINR vs RTT
    sinr = data.get("sinr_db", {})
    sinr_common = [t for t in sinr if t in lat]
    ok6 = len(sinr_common) >= MIN_SAMPLES
    lines.append(("⑥SINRとの相関", ok6,
                  f"揃う秒 {len(sinr_common)}"
                  if ok6 else ("SINR列がありません" if not sinr
                               else f"揃う秒が {len(sinr_common)}秒 しかありません")))

    width = max(_width(name) for name, _, _ in lines)
    for name, ok, why in lines:
        print(f"  {name}{' ' * (width - _width(name))}  "
              f"{'○' if ok else '×'}  {why}")

    # Route evidence: the only thing that could say where traffic went
    hints = sorted({c for c in headers if ROUTE_HINTS.search(c)}
                   | {k for k in static if ROUTE_HINTS.search(k)})
    print("  経路の手がかり: "
          + (", ".join(hints[:8]) + (" …" if len(hints) > 8 else "")
             if hints else "なし(経路・ゲートウェイの特定はできません)"))
    return {"path": path, "lat": lat}


def check(paths):
    """Print the feasibility table for each run, then compare the runs."""
    try:
        _check(paths)
    except BrokenPipeError:
        pass          # piped into head/more and the reader went away


def _check(paths):
    runs = []
    for p in paths:
        path = Path(p)
        if not path.is_dir():
            print(f"\n=== {p}\n  フォルダではありません")
            continue
        got = check_run(path)
        if got:
            runs.append(got)

    if len(runs) < 2:
        return
    print("\n=== 測定どうしの重なり")
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            a, b = runs[i], runs[j]
            shared = set(a["lat"]) & set(b["lat"])
            print(f"  {a['path'].name} と {b['path'].name}: "
                  + (f"同じ秒が {len(shared)}秒 あります(そのまま比較できます)"
                     if len(shared) >= MIN_SAMPLES
                     else f"同じ秒が {len(shared)}秒 しかありません — "
                          "時計がずれている可能性があります"
                          "(ビューアの「時刻を合わせる」で補正してください)"))
