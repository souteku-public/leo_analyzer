"""Local web server for the data viewer.

Runs on the measurement PC; the UI is a single page served from here.
Map tiles and weather/TLE lookups are proxied through this process so
the browser never needs direct cross-origin access.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import data as data_mod
from . import sky, weather

HERE = Path(__file__).resolve().parent
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_tile_cache = {}
_tile_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console readable
        pass

    # --- helpers ---------------------------------------------------------

    def _send(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        self._send(
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    # --- routes ----------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        def arg(name, default=None):
            return query.get(name, [default])[0]

        try:
            if route in ("/", "/index.html"):
                self._send(
                    (HERE / "app.html").read_bytes(), "text/html; charset=utf-8"
                )
            elif route == "/api/browse":
                self._json(data_mod.list_dir(arg("path", str(Path.cwd()))))
            elif route == "/api/load":
                self._json(
                    data_mod.load_run(arg("path"), step=max(1, int(arg("step", "1"))))
                )
            elif route == "/api/sats":
                self._json(self._sats(arg))
            elif route == "/api/rain":
                self._json(
                    weather.rain_grid(
                        float(arg("lat")), float(arg("lon")), float(arg("t"))
                    )
                )
            elif route == "/favicon.ico":
                self._send(b"", "image/x-icon", 200)
            elif route == "/api/tile":
                self._tile(int(arg("z")), int(arg("x")), int(arg("y")))
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            self._json({"error": f"ファイルが見つかりません: {e}"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _sats(self, arg):
        groups = (arg("groups", "starlink,oneweb") or "").split(",")
        lat, lon = float(arg("lat")), float(arg("lon"))
        epoch = float(arg("t"))
        min_el = float(arg("min_el", "10"))
        tle_file = arg("tle") or None
        out, notes = {}, {}
        for group in [g for g in groups if g in sky.GROUPS]:
            try:
                constellation = sky.constellation(group, tle_file)
                out[group] = constellation.look_angles(
                    epoch, lat, lon, min_elevation=min_el
                )
                notes[group] = {
                    "count": len(constellation),
                    "tle_age_days": round(sky.tle_age_days(group), 2),
                }
            except Exception as e:
                out[group] = []
                notes[group] = {"error": f"{type(e).__name__}: {e}"}
        return {"sats": out, "info": notes}

    def _tile(self, z: int, x: int, y: int):
        key = (z, x, y)
        with _tile_lock:
            hit = _tile_cache.get(key)
        if hit is None:
            request = urllib.request.Request(
                TILE_URL.format(z=z, x=x, y=y),
                headers={"User-Agent": "leo_analyzer-viewer/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    hit = response.read()
            except Exception:
                hit = b""
            with _tile_lock:
                if len(_tile_cache) > 4000:
                    _tile_cache.clear()
                _tile_cache[key] = hit
        if not hit:
            self._json({"error": "tile unavailable"}, 502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(hit)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(hit)


def serve(port: int = 8765, open_browser: bool = True, start_path: str = None):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    if start_path:
        url += "?path=" + urllib.parse.quote(str(Path(start_path).resolve()))
    print(f"データビューアを起動しました: {url}")
    print("終了するには Ctrl+C を押してください")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        server.server_close()
