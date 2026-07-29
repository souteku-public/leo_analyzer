"""Self-contained HTML report (inline SVG charts) for a measurement run.

Reads throughput.csv / summary.json / *_status.csv from a run directory
and writes report.html next to them. No external libraries, no network:
the file opens offline in any browser.
"""

import csv
import html
import json
import math
from pathlib import Path

# Reference palette (validated): slot1 blue, slot2 orange, status-critical,
# chrome inks. Light/dark variants swap via CSS custom properties.
CSS = """
:root {
  color-scheme: light dark;
  --surface: #fcfcfb; --page: #f9f9f7;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --s5: #e87ba4; --s6: #008300; --crit: #d03b3b;
  --band: rgba(137,135,129,0.10);
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #1a1a19; --page: #0d0d0d;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s5: #d55181; --s6: #008300; --crit: #d03b3b;
    --band: rgba(137,135,129,0.14);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--ink-2); font-size: 13px; margin-bottom: 20px; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
.tile {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px; min-width: 150px;
}
.tile .k { font-size: 12px; color: var(--ink-2); }
.tile .v { font-size: 26px; margin-top: 2px; }
.tile .u { font-size: 13px; color: var(--muted); margin-left: 2px; }
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; margin-bottom: 20px;
  overflow-x: auto;
}
.card h2 { font-size: 15px; margin: 0 0 2px; }
.card .cap { font-size: 12px; color: var(--ink-2); margin-bottom: 8px; }
.legend { display: flex; gap: 16px; font-size: 12px; color: var(--ink-2);
  margin: 4px 0 2px; }
.legend .sw { display: inline-block; width: 10px; height: 10px;
  border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
svg { display: block; width: 100%; height: auto; }
svg text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.tooltip {
  position: fixed; pointer-events: none; display: none; z-index: 10;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 9px; font-size: 12px; color: var(--ink);
  box-shadow: 0 2px 8px rgba(0,0,0,0.15); white-space: nowrap;
}
.tooltip .t { color: var(--ink-2); margin-bottom: 2px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 20px; }
@media (max-width: 500px) { .grid2 { grid-template-columns: 1fr; } }
"""

JS = """
(function () {
  var tip = document.createElement('div');
  tip.className = 'tooltip';
  document.body.appendChild(tip);
  document.querySelectorAll('svg[data-chart]').forEach(function (svg) {
    var d = JSON.parse(svg.getAttribute('data-chart'));
    var cross = svg.querySelector('.cross');
    var dots = svg.querySelectorAll('.hoverdot');
    svg.addEventListener('mousemove', function (ev) {
      var pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      var p = pt.matrixTransform(svg.getScreenCTM().inverse());
      if (p.x < d.x0 || p.x > d.x1) { hide(); return; }
      var frac = (p.x - d.x0) / (d.x1 - d.x0);
      var i = Math.round(frac * (d.t.length - 1));
      i = Math.max(0, Math.min(d.t.length - 1, i));
      var cx = d.x0 + (d.t.length < 2 ? 0 : i / (d.t.length - 1) * (d.x1 - d.x0));
      cross.setAttribute('x1', cx); cross.setAttribute('x2', cx);
      cross.style.display = '';
      var lines = '<div class="t">' + d.t[i] + '</div>';
      d.series.forEach(function (s, k) {
        var v = s.v[i];
        var dot = dots[k];
        if (v === null || v === undefined) {
          lines += s.n + ': &mdash;<br>';
          if (dot) dot.style.display = 'none';
        } else {
          lines += '<span class="sw" style="background:' + s.c +
            ';display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px"></span>' +
            s.n + ': <b>' + v + '</b> ' + d.unit + '<br>';
          if (dot) {
            var cy = d.y1 - (v - d.vmin) / (d.vmax - d.vmin) * (d.y1 - d.y0);
            dot.setAttribute('cx', cx); dot.setAttribute('cy', cy);
            dot.style.display = '';
          }
        }
      });
      tip.innerHTML = lines;
      tip.style.display = 'block';
      var tx = ev.clientX + 14, ty = ev.clientY + 14;
      var r = tip.getBoundingClientRect();
      if (tx + r.width > window.innerWidth - 8) tx = ev.clientX - r.width - 14;
      if (ty + r.height > window.innerHeight - 8) ty = ev.clientY - r.height - 14;
      tip.style.left = tx + 'px'; tip.style.top = ty + 'px';
    });
    svg.addEventListener('mouseleave', hide);
    function hide() {
      tip.style.display = 'none';
      cross.style.display = 'none';
      dots.forEach(function (x) { x.style.display = 'none'; });
    }
  });
})();
"""

W, H = 900, 260
ML, MR, MT, MB = 58, 16, 14, 30  # margins
PHASE_LABELS = {"baseline": "無負荷", "download": "下り測定", "upload": "上り測定"}
# categorical slots (validated order); index k -> CSS var / literal hex
PALETTE_VARS = [f"var(--s{i})" for i in range(1, 7)]
PALETTE_HEX = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]


def _fmt_elapsed(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def _nice_ticks(vmax):
    if vmax <= 0:
        return [0, 1]
    raw = vmax / 4
    mag = 10 ** math.floor(math.log10(raw))
    for step in (1, 2, 5, 10):
        if raw <= step * mag:
            step *= mag
            break
    ticks, v = [], 0.0
    while v < vmax * 1.0001 + step * 0.999:
        ticks.append(round(v, 6))
        v += step
        if len(ticks) > 8:
            break
    return ticks


def _time_tick_step(total_s):
    for step in (10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200):
        if total_s / step <= 8:
            return step
    return 14400


def _downsample(ts, values_list, max_points=1400):
    """Bucket to min+max per bucket so spikes/dips survive."""
    n = len(ts)
    if n <= max_points:
        return ts, values_list
    nbuckets = max_points // 2
    out_t, outs = [], [[] for _ in values_list]
    for b in range(nbuckets):
        lo = b * n // nbuckets
        hi = max(lo + 1, (b + 1) * n // nbuckets)
        # pick per-bucket min and max positions using the first series
        # that has data; keep row alignment across series
        ref = None
        for vs in values_list:
            seg = [(v, i) for i, v in enumerate(vs[lo:hi]) if v is not None]
            if seg:
                ref = seg
                break
        if ref is None:
            picks = [lo]
        else:
            imin = min(ref)[1] + lo
            imax = max(ref)[1] + lo
            picks = sorted({imin, imax})
        for i in picks:
            out_t.append(ts[i])
            for k, vs in enumerate(values_list):
                outs[k].append(vs[i])
    return out_t, outs


def svg_chart(title, caption, ts, series, unit, phases=None, colors=None,
              w=W, h=H, time_fmt=None):
    """series: list of (name, [float|None,...]); ts: elapsed seconds."""
    if not ts:
        return ""
    fmt = time_fmt or _fmt_elapsed
    colors = colors or PALETTE_VARS
    ts, vals = _downsample(ts, [v for _, v in series])
    series = [(series[i][0], vals[i]) for i in range(len(series))]

    allv = [v for _, vs in series for v in vs if v is not None]
    vmax = max(allv) if allv else 1.0
    ticks = _nice_ticks(vmax)
    vmax = ticks[-1]
    x0, x1 = ML, w - MR
    y0, y1 = MT, h - MB
    t0, t1 = ts[0], ts[-1]
    tspan = max(t1 - t0, 1)

    def X(t):
        return x0 + (t - t0) / tspan * (x1 - x0)

    def Y(v):
        return y1 - v / vmax * (y1 - y0)

    parts = []
    # phase bands (baseline shaded)
    for ph in phases or []:
        if ph["phase"] == "baseline":
            parts.append(
                f'<rect x="{X(ph["start"]):.1f}" y="{y0}" '
                f'width="{max(X(ph["end"]) - X(ph["start"]), 1):.1f}" '
                f'height="{y1 - y0}" fill="var(--band)"/>'
            )
    for ph in phases or []:
        label = PHASE_LABELS.get(ph["phase"], ph["phase"])
        cx = (X(ph["start"]) + X(ph["end"])) / 2
        if X(ph["end"]) - X(ph["start"]) > 60:
            parts.append(
                f'<text x="{cx:.1f}" y="{y0 + 12}" font-size="11" '
                f'fill="var(--muted)" text-anchor="middle">{label}</text>'
            )
    # gridlines + y labels
    for tv in ticks:
        yy = Y(tv)
        parts.append(
            f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"/>'
        )
        label = f"{tv:g}"
        parts.append(
            f'<text x="{x0 - 8}" y="{yy + 4:.1f}" font-size="11" '
            f'fill="var(--muted)" text-anchor="end" '
            f'style="font-variant-numeric:tabular-nums">{label}</text>'
        )
    # x ticks
    step = _time_tick_step(tspan)
    tt = math.ceil(t0 / step) * step
    while tt <= t1:
        xx = X(tt)
        parts.append(
            f'<line x1="{xx:.1f}" y1="{y1}" x2="{xx:.1f}" y2="{y1 + 4}" '
            f'stroke="var(--axis)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{xx:.1f}" y="{y1 + 17}" font-size="11" fill="var(--muted)" '
            f'text-anchor="middle" '
            f'style="font-variant-numeric:tabular-nums">{fmt(tt)}</text>'
        )
        tt += step
    # baseline axis
    parts.append(
        f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" '
        f'stroke="var(--axis)" stroke-width="1"/>'
    )
    # series lines (gaps where value is None)
    for k, (name, vs) in enumerate(series):
        color = colors[k % len(colors)]
        d, pen = [], False
        for i, v in enumerate(vs):
            if v is None:
                pen = False
                continue
            cmd = "L" if pen else "M"
            d.append(f"{cmd}{X(ts[i]):.1f} {Y(v):.1f}")
            pen = True
        if d:
            parts.append(
                f'<path d="{" ".join(d)}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            )
    # hover chrome
    parts.append(
        f'<line class="cross" x1="0" y1="{y0}" x2="0" y2="{y1}" '
        f'stroke="var(--axis)" stroke-width="1" style="display:none"/>'
    )
    for k in range(len(series)):
        color = colors[k % len(colors)]
        parts.append(
            f'<circle class="hoverdot" r="4" fill="{color}" '
            f'stroke="var(--surface)" stroke-width="2" style="display:none"/>'
        )

    data = {
        "x0": x0, "x1": x1, "y0": y0, "y1": y1,
        "vmin": 0, "vmax": vmax, "unit": unit,
        "t": [fmt(t) for t in ts],
        "series": [
            {
                "n": name,
                "c": PALETTE_HEX[k % len(PALETTE_HEX)],
                "v": [None if v is None else round(v, 2) for v in vs],
            }
            for k, (name, vs) in enumerate(series)
        ],
    }
    legend = ""
    if len(series) > 1:
        items = "".join(
            f'<span><span class="sw" style="background:{colors[k % len(colors)]}">'
            f"</span>{html.escape(name)}</span>"
            for k, (name, _) in enumerate(series)
        )
        legend = f'<div class="legend">{items}</div>'
    return (
        f'<div class="card"><h2>{html.escape(title)}</h2>'
        f'<div class="cap">{html.escape(caption)}</div>{legend}'
        f'<svg viewBox="0 0 {w} {h}" '
        f"data-chart='{json.dumps(data, ensure_ascii=False)}'>"
        f'{"".join(parts)}</svg></div>'
    )


def _read_csv(path):
    path = Path(path)
    if path.suffix == ".gz":
        import gzip

        with gzip.open(path, "rt", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _find_csv(rundir, name):
    """Return <name>.csv or its .gz variant, whichever exists."""
    for candidate in (Path(rundir) / name, Path(rundir) / (name + ".gz")):
        if candidate.exists():
            return candidate
    return None


def _f(row, key):
    v = row.get(key, "")
    if v in ("", None):
        return None
    if v in ("True", "true"):
        return 1.0
    if v in ("False", "false"):
        return 0.0
    try:
        f = float(v)
    except ValueError:
        return None
    # the dish reports NaN for statistics it has no data for yet; a single
    # NaN would poison a chart's axis range
    return None if math.isnan(f) or math.isinf(f) else f


def _phases_from_rows(rows, t0):
    phases, cur = [], None
    for r in rows:
        ph = r.get("phase", "")
        t = _f(r, "epoch")
        if t is None:
            continue
        t -= t0
        if cur is None or cur["phase"] != ph:
            if cur is not None:
                phases.append(cur)
            cur = {"phase": ph, "start": t, "end": t}
        else:
            cur["end"] = t
    if cur is not None:
        phases.append(cur)
    return [p for p in phases if p["phase"] in PHASE_LABELS]


def _pick_columns(rows, max_cols=8):
    """Numeric columns that actually vary, most-varying first (relative)."""
    skip = {"timestamp_utc", "epoch", "error", "notes"}
    scores = []
    for col in rows[0].keys():
        if col in skip or col.endswith("._error"):
            continue
        vals = [_f(r, col) for r in rows]
        vals = [v for v in vals if v is not None]
        if len(vals) < 2:
            continue
        vmin, vmax = min(vals), max(vals)
        if vmax == vmin:
            continue
        mean = sum(vals) / len(vals)
        spread = (vmax - vmin) / (abs(mean) + 1e-9)
        scores.append((spread, col))
    scores.sort(reverse=True)
    return [c for _, c in scores[:max_cols]]


# field names as reported by a real Starlink Mini (sw 2026.07.09); the
# obstruction fields differ between firmware versions, so list both
STARLINK_PRIORITY = [
    "pop_ping_latency_ms",
    "pop_ping_drop_rate",
    "downlink_throughput_bps",
    "uplink_throughput_bps",
    "obstruction_stats.time_obstructed",
    "obstruction_stats.fraction_obstructed",
    "obstruction_stats.currently_obstructed",
    "boresight_azimuth_deg",
    "boresight_elevation_deg",
    "is_snr_above_noise_floor",
    "gps_stats.gps_sats",
]


def _status_charts(rows, source_name):
    t0 = _f(rows[0], "epoch")
    ts = []
    ok_rows = []
    for r in rows:
        t = _f(r, "epoch")
        if t is None:
            continue
        ts.append(t - t0)
        ok_rows.append(r)
    if not ok_rows:
        return ""

    if source_name == "starlink":
        cols = [c for c in STARLINK_PRIORITY if c in ok_rows[0]]
        cols += [c for c in _pick_columns(ok_rows) if c not in cols]
        cols = cols[:8]
    else:
        cols = _pick_columns(ok_rows)
    charts = []
    for col in cols:
        vals = [_f(r, col) for r in ok_rows]
        unit, name, scale = "", col, 1.0
        if col.endswith("_bps"):
            unit, name, scale = "Mbps", col[:-4] + " (Mbps)", 1e-6
        elif col.endswith("_ms"):
            unit = "ms"
        vals = [None if v is None else v * scale for v in vals]
        charts.append(
            svg_chart(name, f"1秒ごとの {col}", ts, [(name, vals)], unit,
                      w=560, h=220)
        )
    if not charts:
        return ""
    title = {"starlink": "Starlink アンテナ情報", "kymeta": "Kymeta アンテナ情報"}.get(
        source_name, source_name
    )
    return f"<h1>{html.escape(title)}</h1>\n" + f'<div class="grid2">{"".join(charts)}</div>'


# --- cross-antenna comparison -------------------------------------------
#
# Starlink and Kymeta expose overlapping information under different field
# names. Each entry maps a canonical metric to the column patterns that
# carry it, so both antennas can be drawn on one time axis.
COMMON_METRICS = [
    ("down_mbps", "下り スループット", "Mbps",
     [r"^mbps_down$", r"downlink_throughput_bps$"]),
    ("up_mbps", "上り スループット", "Mbps",
     [r"^mbps_up$", r"uplink_throughput_bps$"]),
    ("rtt_ms", "応答時間 (RTT)", "ms",
     [r"^latency_ms$", r"pop_ping_latency_ms$", r"ping.*latency.*ms$"]),
    ("loss", "パケットロス率", "",
     [r"pop_ping_drop_rate$", r"drop_rate$", r"loss"]),
    ("snr_db", "信号品質 (SNR / SINR / CNR)", "dB",
     [r"sinr[_ ]?db$", r"\bsinr$", r"snr_db$", r"cnr[_ ]?db$", r"\bcnr$"]),
    ("elevation", "仰角", "°",
     [r"look[-_ ]?angle\.elevation$", r"\belevation$",
      r"boresight_elevation_deg$"]),
    ("azimuth", "方位角", "°",
     [r"look[-_ ]?angle\.azimuth$", r"\bazimuth$", r"boresight_azimuth_deg$"]),
    ("obstruction", "遮蔽 / 障害",  "",
     [r"fraction_obstructed$", r"time_obstructed$", r"currently_obstructed$"]),
]


def _match_column(rows, patterns):
    """First column matching any pattern, with a unit scale factor."""
    import re

    cols = list(rows[0].keys())
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for c in cols:
            if rx.search(c):
                vals = [_f(r, c) for r in rows]
                if any(v is not None for v in vals):
                    return c, (1e-6 if c.endswith("_bps") else 1.0)
    return None, 1.0


def _source_series(rundir):
    """Canonical metric -> (label, epochs, values) for one run directory."""
    rundir = Path(rundir)
    label = rundir.name
    summary_path = rundir / "summary.json"
    if summary_path.exists():
        try:
            label = json.loads(summary_path.read_text(encoding="utf-8")).get(
                "label", label
            )
        except Exception:
            pass

    out = {}
    sources = [("測定", _find_csv(rundir, "throughput.csv"))]
    for antenna in ("starlink", "kymeta"):
        sources.append(
            (antenna.capitalize(), _find_csv(rundir, f"{antenna}_status.csv"))
        )
    for src_name, path in sources:
        if path is None:
            continue
        rows = _read_csv(path)
        if not rows:
            continue
        epochs = [_f(r, "epoch") for r in rows]
        for key, _title, _unit, patterns in COMMON_METRICS:
            col, scale = _match_column(rows, patterns)
            if col is None:
                continue
            vals = [
                None if _f(r, col) is None else _f(r, col) * scale for r in rows
            ]
            if not any(v is not None for v in vals):
                continue
            name = f"{src_name} ({label})" if label else src_name
            out.setdefault(key, []).append((name, epochs, vals, col))
    return label, out


def _align(series_list):
    """Resample series onto a shared 1-second grid keyed by epoch."""
    starts = [min(e for e in eps if e) for _n, eps, _v, _c in series_list]
    ends = [max(e for e in eps if e) for _n, eps, _v, _c in series_list]
    t0, t1 = min(starts), max(ends)
    n = int(t1 - t0) + 1
    if n > 200000:  # safety for very long captures
        n = 200000
    grid = [t0 + i for i in range(n)]
    aligned = []
    for name, eps, vals, _col in series_list:
        buf = [None] * n
        for e, v in zip(eps, vals):
            if e is None or v is None:
                continue
            i = int(round(e - t0))
            if 0 <= i < n:
                buf[i] = v
        aligned.append((name, buf))
    return grid, aligned, t0


def generate_compare_report(rundirs, out_path=None) -> Path:
    """One page comparing common metrics across runs/antennas by clock time."""
    from datetime import datetime, timezone

    per_metric = {}
    labels = []
    for d in rundirs:
        label, metrics = _source_series(d)
        labels.append(label)
        for key, entries in metrics.items():
            per_metric.setdefault(key, []).extend(entries)

    body = [
        "<h1>アンテナ比較レポート</h1>",
        f'<div class="sub">対象: {html.escape(", ".join(labels))} — '
        "同じ時刻軸で共通項目を重ねて表示しています</div>",
    ]
    charts = 0
    for key, title, unit, _pat in COMMON_METRICS:
        entries = per_metric.get(key)
        if not entries or len(entries) < 1:
            continue
        grid, aligned, t0 = _align(entries)
        ts = [t - t0 for t in grid]

        def clock(sec, base=t0):
            return datetime.fromtimestamp(base + sec, timezone.utc).strftime(
                "%H:%M:%S"
            )

        cols = ", ".join(sorted({e[3] for e in entries}))
        charts += 1
        body.append(
            svg_chart(
                title,
                f"元データ列: {cols}(時刻はUTC)",
                ts,
                aligned,
                unit,
                time_fmt=clock,
            )
        )
    if not charts:
        body.append(
            '<div class="card">比較できる共通項目が見つかりませんでした。</div>'
        )

    out = Path(out_path) if out_path else Path(rundirs[0]).parent / "compare.html"
    doc = (
        "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
        "<title>アンテナ比較レポート</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{CSS}</style></head><body>"
        + "\n".join(body)
        + f"<script>{JS}</script></body></html>"
    )
    out.write_text(doc, encoding="utf-8")
    return out


def _tile(k, v, u=""):
    return (
        f'<div class="tile"><div class="k">{html.escape(k)}</div>'
        f'<div class="v">{v}<span class="u">{u}</span></div></div>'
    )


def generate_report(rundir) -> Path:
    rundir = Path(rundir)
    out = rundir / "report.html"
    body = []

    summary = {}
    if (rundir / "summary.json").exists():
        summary = json.loads((rundir / "summary.json").read_text(encoding="utf-8"))

    label = summary.get("label", rundir.name)
    started = summary.get("started_utc", "")
    body.append(f"<h1>LEO回線測定レポート — {html.escape(str(label))}</h1>")
    body.append(
        f'<div class="sub">開始 {html.escape(str(started))} / '
        f"フォルダ {html.escape(rundir.name)}</div>"
    )

    tiles = []
    dl = summary.get("download_mbps", {})
    ul = summary.get("upload_mbps", {})
    lat = summary.get("latency_ms", {})
    if dl:
        tiles.append(_tile("下り 平均", dl.get("avg", "-"), "Mbps"))
        tiles.append(_tile("下り 最大", dl.get("max", "-"), "Mbps"))
    if ul:
        tiles.append(_tile("上り 平均", ul.get("avg", "-"), "Mbps"))
        tiles.append(_tile("上り 最大", ul.get("max", "-"), "Mbps"))
    if lat.get("idle"):
        tiles.append(_tile("アイドルRTT 平均", lat["idle"].get("avg", "-"), "ms"))
        tiles.append(
            _tile("疎通不能", lat["idle"].get("unreachable_s", 0), "秒(無負荷中)")
        )
    if tiles:
        body.append(f'<div class="tiles">{"".join(tiles)}</div>')

    tp = _find_csv(rundir, "throughput.csv")
    if tp is not None:
        rows = _read_csv(tp)
        if rows:
            t0 = _f(rows[0], "epoch")
            ts = [(_f(r, "epoch") or t0) - t0 for r in rows]
            phases = _phases_from_rows(rows, t0)
            down = [_f(r, "mbps_down") for r in rows]
            up = [_f(r, "mbps_up") for r in rows]
            latv = [_f(r, "latency_ms") for r in rows]
            body.append(
                svg_chart(
                    "スループット",
                    "1秒ごとの実効速度。薄い網掛けは無負荷(アイドルRTT測定)区間",
                    ts,
                    [("下り", down), ("上り", up)],
                    "Mbps",
                    phases=phases,
                )
            )
            n_unreach = sum(
                1
                for r in rows
                if r.get("phase") in PHASE_LABELS and _f(r, "latency_ms") is None
            )
            body.append(
                svg_chart(
                    "応答時間(RTT)",
                    "線が途切れている箇所は疎通が取れなかった秒"
                    f"(計 {n_unreach} 秒)。無負荷区間の値がアイドルRTT",
                    ts,
                    [("RTT", latv)],
                    "ms",
                    phases=phases,
                )
            )

    # cross-source view: same metric from the measurement and the antenna
    # on one time axis (e.g. measured RTT vs the dish's own ping latency)
    _label, metrics = _source_series(rundir)
    combined = []
    for key, title, unit, _pat in COMMON_METRICS:
        entries = metrics.get(key) or []
        if len(entries) < 2:
            continue  # only interesting when two sources report it
        grid, aligned, t0 = _align(entries)
        combined.append(
            svg_chart(
                title,
                "測定値とアンテナ内部値の比較(元データ列: "
                + ", ".join(sorted({e[3] for e in entries}))
                + ")",
                [t - t0 for t in grid],
                aligned,
                unit,
            )
        )
    if combined:
        body.append("<h1>測定値とアンテナ情報の比較</h1>")
        body.extend(combined)

    for name in ("starlink", "kymeta"):
        p = _find_csv(rundir, f"{name}_status.csv")
        if p is not None:
            rows = _read_csv(p)
            if rows:
                body.append(_status_charts(rows, name))

    doc = (
        "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
        f"<title>LEO測定レポート {html.escape(str(label))}</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{CSS}</style></head><body>"
        + "\n".join(body)
        + f"<script>{JS}</script></body></html>"
    )
    out.write_text(doc, encoding="utf-8")
    return out
