"""PLATEAU 3D city model -> compact building set, and line-of-sight maths.

The viewer needs building outlines with heights, not a full CityGML tree,
so this reduces LOD1 (or LOD0 footprint) geometry to one polygon and one
height per building, clipped to the area a measurement run actually
covers. The result is a few hundred KB of JSON the browser can hold.

Vertical datum: PLATEAU heights are orthometric. The camera is placed at
the local ground taken from the buildings themselves plus the configured
antenna height, so the antenna's own altitude column (whose datum we
cannot verify) never enters the calculation.
"""

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

# Matched on local names only. PLATEAU's newer product specifications move
# to CityGML 3.0 / GML 3.2, which changes every namespace URI but keeps the
# element names, so pinning namespaces would break on the newest data.
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
DEFAULT_MARGIN_M = 500.0     # how far beyond the track to keep buildings
DEFAULT_RADIUS_M = 400.0     # how far to look when testing a line of sight
DEFAULT_ANTENNA_H = 2.6      # metres above local ground
M_PER_DEG_LAT = 111320.0


def m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


# --- CityGML ------------------------------------------------------------

def _pos_lists(element):
    """Every gml:posList under an element, as (lat, lon, z) triples."""
    for node in element.iter():
        if _local(node.tag) != "posList":
            continue
        raw = (node.text or "").split()
        dim = int(node.get("srsDimension", "3"))
        if dim != 3 or len(raw) < 9:
            continue
        try:
            nums = [float(v) for v in raw]
        except ValueError:
            continue
        # PLATEAU uses EPSG:6697, i.e. latitude first
        yield [(nums[i], nums[i + 1], nums[i + 2]) for i in range(0, len(nums) - 2, 3)]


def _building(element):
    """One building as (polygon lat/lon, ground z, height) or None."""
    rings = [r for r in _pos_lists(element) if len(r) >= 4]
    if not rings:
        return None
    # the ground face is the ring sitting lowest; roofs and walls sit above
    ground = min(rings, key=lambda r: sum(p[2] for p in r) / len(r))
    z0 = sum(p[2] for p in ground) / len(ground)
    top = max(p[2] for r in rings for p in r)

    # CityGML 2.0 has bldg:measuredHeight; 3.0 moves it under a Height
    # object as con:value. Fall back to the geometry when neither is usable.
    height = None
    for node in element.iter():
        if _local(node.tag) in ("measuredHeight", "value") and node.text:
            try:
                height = float(node.text)
            except ValueError:
                continue
            if height > 0:
                break
    if not height or height <= 0:
        height = top - z0
    if height <= 0:
        return None

    poly = [(round(p[0], 7), round(p[1], 7)) for p in ground]
    if poly[0] == poly[-1]:
        poly.pop()
    if len(poly) < 3:
        return None
    return poly, z0, height


def mesh_bounds(code: str):
    """Lat/lon box of a standard 3rd-order (1 km) grid square code.

    PLATEAU names every CityGML file after the mesh it covers, so a file
    whose mesh misses the track can be skipped without opening it.
    """
    if len(code) != 8 or not code.isdigit():
        return None
    p, u = int(code[0:2]), int(code[2:4])
    q, v = int(code[4]), int(code[5])
    r, w = int(code[6]), int(code[7])
    lat = (p + q / 8 + r / 80) / 1.5
    lon = 100 + u + v / 8 + w / 80
    return lat, lon, lat + 1 / 120, lon + 1 / 80


def mesh_of(name: str):
    """The leading 8-digit mesh code of a PLATEAU file name, if any."""
    head = Path(name).name.split("_", 1)[0]
    return head if len(head) == 8 and head.isdigit() else None


def _overlaps(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _bbox_of(poly):
    lats = [p[0] for p in poly]
    lons = [p[1] for p in poly]
    return min(lats), min(lons), max(lats), max(lons)


def parse_citygml(sources, bbox=None, progress=None):
    """Read CityGML files into [{poly, z0, h}], keeping only bbox hits.

    bbox is (min_lat, min_lon, max_lat, max_lon); None keeps everything.
    Parsed incrementally: a PLATEAU mesh file can be hundreds of MB.
    """
    files = []
    for src in sources:
        path = Path(src).expanduser()
        if path.is_dir():
            files.extend(sorted(path.rglob("*.gml")))
        elif path.suffix.lower() == ".gml":
            files.append(path)
    if not files:
        raise FileNotFoundError("CityGML(.gml)ファイルが見つかりませんでした")

    if bbox:   # a whole ward is hundreds of files; most miss the track
        keep = []
        for f in files:
            code = mesh_of(f.name)
            box = mesh_bounds(code) if code else None
            if box is None or _overlaps(box, bbox):
                keep.append(f)
        if progress and len(keep) < len(files):
            progress(f"  {len(files)} ファイル中 {len(keep)} ファイルが走行範囲に該当"
                     f"(残りはメッシュ番号で読み飛ばし)")
        files = keep

    out, seen, skipped, broken = [], 0, 0, []
    for file in files:
        kept_here = 0
        try:
            for _event, element in ET.iterparse(file, events=("end",)):
                if _local(element.tag) != "Building":
                    continue
                seen += 1
                parsed = _building(element)
                element.clear()
                if parsed is None:
                    skipped += 1
                    continue
                poly, z0, height = parsed
                if bbox:
                    b = _bbox_of(poly)
                    if (b[2] < bbox[0] or b[0] > bbox[2]
                            or b[3] < bbox[1] or b[1] > bbox[3]):
                        continue
                out.append({"poly": poly, "z0": round(z0, 2), "h": round(height, 2)})
                kept_here += 1
        except ET.ParseError as e:
            # a truncated or malformed file must not lose the other wards;
            # whatever parsed before the break is still usable
            broken.append(file.name)
            if progress:
                progress(f"  {file.name}: 壊れています({e})。この分は飛ばします")
        if progress:
            progress(f"  {file.name}: {kept_here} 棟を採用")
    return out, {"scanned": seen, "unusable": skipped, "broken": broken}


# --- line of sight ------------------------------------------------------

class Skyline:
    """Buildings around one point, in local metres, for occlusion tests."""

    def __init__(self, buildings, lat, lon, antenna_h=DEFAULT_ANTENNA_H,
                 radius=DEFAULT_RADIUS_M):
        self.lat, self.lon = lat, lon
        mlat, mlon = M_PER_DEG_LAT, m_per_deg_lon(lat)
        self.near = []
        grounds = []
        for b in buildings:
            pts = [((p[1] - lon) * mlon, (p[0] - lat) * mlat) for p in b["poly"]]
            if min(math.hypot(x, y) for x, y in pts) > radius:
                continue
            self.near.append({"pts": pts, "z0": b["z0"], "h": b["h"]})
            grounds.append(b["z0"])
        grounds.sort()
        # local ground from the buildings themselves, so the antenna height
        # control means exactly what it says
        self.ground = grounds[len(grounds) // 2] if grounds else 0.0
        self.cam_z = self.ground + antenna_h

    def __len__(self):
        return len(self.near)

    def elevation_at(self, azimuth_deg: float):
        """Skyline elevation along one azimuth, and what causes it."""
        a = math.radians(azimuth_deg)
        dx, dy = math.sin(a), math.cos(a)   # east, north
        best, best_b = 0.0, None
        for b in self.near:
            top = b["z0"] + b["h"] - self.cam_z
            if top <= 0:
                continue
            dist = _ray_polygon_distance(dx, dy, b["pts"])
            if dist is None or dist <= 0.5:
                continue
            el = math.degrees(math.atan2(top, dist))
            if el > best:
                best, best_b = el, b
        return best, best_b

    def clearance(self, azimuth_deg: float, elevation_deg: float):
        """Beam elevation minus skyline elevation; negative means blocked."""
        sky, _ = self.elevation_at(azimuth_deg)
        return elevation_deg - sky, sky


def _ray_polygon_distance(dx, dy, pts):
    """Distance from the origin to the nearest crossing of the outline."""
    best = None
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        ex, ey = x2 - x1, y2 - y1
        den = dx * ey - dy * ex
        if abs(den) < 1e-12:
            continue
        t = (x1 * ey - y1 * ex) / den          # along the ray
        s = (x1 * dy - y1 * dx) / den          # along the edge
        if t > 0 and 0.0 <= s <= 1.0 and (best is None or t < best):
            best = t
    if best is None and _inside(0.0, 0.0, pts):
        best = 0.0                              # standing inside the outline
    return best


def _inside(x, y, pts):
    hit = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xx = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xx:
                hit = not hit
    return hit


class SkylineIndex:
    """Grid index so a per-second sweep does not rescan every building."""

    CELL_M = 250.0

    def __init__(self, buildings, radius=DEFAULT_RADIUS_M):
        self.radius = radius
        self.buildings = buildings
        self.cells = {}
        if not buildings:
            self.lat0 = self.lon0 = 0.0
            return
        self.lat0 = sum(p[0] for b in buildings for p in b["poly"]) / sum(
            len(b["poly"]) for b in buildings)
        self.lon0 = sum(p[1] for b in buildings for p in b["poly"]) / sum(
            len(b["poly"]) for b in buildings)
        self.mlon = m_per_deg_lon(self.lat0)
        for i, b in enumerate(buildings):
            xs = [(p[1] - self.lon0) * self.mlon for p in b["poly"]]
            ys = [(p[0] - self.lat0) * M_PER_DEG_LAT for p in b["poly"]]
            for cx in range(int(min(xs) // self.CELL_M), int(max(xs) // self.CELL_M) + 1):
                for cy in range(int(min(ys) // self.CELL_M),
                                int(max(ys) // self.CELL_M) + 1):
                    self.cells.setdefault((cx, cy), []).append(i)

    def near(self, lat, lon):
        if not self.buildings:
            return []
        x = (lon - self.lon0) * self.mlon
        y = (lat - self.lat0) * M_PER_DEG_LAT
        reach = int(self.radius // self.CELL_M) + 1
        cx, cy = int(x // self.CELL_M), int(y // self.CELL_M)
        picked = set()
        for i in range(cx - reach, cx + reach + 1):
            for j in range(cy - reach, cy + reach + 1):
                picked.update(self.cells.get((i, j), ()))
        return [self.buildings[i] for i in picked]

    def at(self, lat, lon, antenna_h=DEFAULT_ANTENNA_H):
        return Skyline(self.near(lat, lon), lat, lon, antenna_h, self.radius)


def interpolate_track(times, lats, lons):
    """Fill the 2-second position staircase with per-second positions.

    Kymeta refreshes position at about 0.5 Hz, so at 46 km/h the reported
    fix can be 25 m behind. Each distinct fix is treated as arriving at the
    first second it appears, and the seconds between are interpolated.
    """
    knots = []
    for t, la, lo in zip(times, lats, lons):
        if la is None or lo is None:
            continue
        if not knots or (la, lo) != (knots[-1][1], knots[-1][2]):
            knots.append((t, la, lo))
    if len(knots) < 2:
        return [(la, lo) if la is not None else None
                for la, lo in zip(lats, lons)]
    out, k = [], 0
    for t in times:
        # never extrapolate: other collectors may span a wider time range
        if t < knots[0][0] or t > knots[-1][0]:
            out.append(None)
            continue
        while k + 2 < len(knots) and knots[k + 1][0] <= t:
            k += 1
        t0, la0, lo0 = knots[k]
        t1, la1, lo1 = knots[k + 1]
        if t1 == t0:
            out.append((la0, lo0))
            continue
        f = max(0.0, min(1.0, (t - t0) / (t1 - t0)))
        out.append((la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f))
    return out


# --- run-level helpers --------------------------------------------------

def track_bbox(times_latlon, margin_m=DEFAULT_MARGIN_M):
    lats = [p[0] for p in times_latlon]
    lons = [p[1] for p in times_latlon]
    mid = sum(lats) / len(lats)
    dlat = margin_m / M_PER_DEG_LAT
    dlon = margin_m / m_per_deg_lon(mid)
    return (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)


def write_buildings(path: Path, buildings, meta=None):
    payload = {"count": len(buildings), "meta": meta or {}, "buildings": buildings}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_buildings(path: Path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("buildings", []), data.get("meta", {})
