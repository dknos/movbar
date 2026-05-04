"""Stremio addon: meta-override that replaces the background art with a movie barcode.

Endpoints:
  GET /manifest.json
  GET /meta/<type>/<id>.json     -> proxies Cinemeta + mutates background if barcode cached
  GET /barcode/<id>_<mode>.png   -> serves rendered barcode
  GET /trigger/<imdb_id>?mode=slice|avg  -> kick off async render (debug)
  GET /                          -> simple index/status

Run:  python3 server.py [PORT]
"""
import json, os, sys, threading, time, traceback, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import worker

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9450
HOST_FOR_URLS = os.environ.get("MOVBAR_PUBLIC_HOST", f"127.0.0.1:{PORT}")
HOST_SCHEME = os.environ.get("MOVBAR_SCHEME", "http")
CINEMETA = "https://v3-cinemeta.strem.io"

# In-flight set so we don't kick off two ffmpegs for the same id concurrently
_inflight_lock = threading.Lock()
_inflight = set()

MANIFEST = {
    "id": "org.movbar.barcode",
    "version": "0.0.1",
    "name": "Movie Barcode",
    "description": "Replaces background art with a barcode of the movie (every Nth frame stitched into a 1920-column image). Async render — first watch keeps default backdrop, refresh to see the barcode.",
    "resources": ["meta"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "behaviorHints": {"adult": False, "p2p": False, "configurable": False},
}


def cinemeta_meta(type_, id_):
    url = f"{CINEMETA}/meta/{type_}/{id_}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Stremio/1.9.12"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def kick_off(imdb_id, mode="slice", season=None, episode=None):
    key = (imdb_id, mode, season, episode)
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)

    def run():
        try:
            print(f"[render] start {key}")
            t0 = time.time()
            out, status = worker.generate(imdb_id, mode=mode, season=season, episode=episode)
            print(f"[render] done {key} in {time.time()-t0:.1f}s status={status} -> {out}")
        except Exception:
            traceback.print_exc()
        finally:
            with _inflight_lock:
                _inflight.discard(key)

    threading.Thread(target=run, daemon=True).start()
    return True


def barcode_url_if_ready(imdb_id, mode="slice"):
    out = worker.CACHE / f"{imdb_id}_{mode}.png"
    if out.exists() and out.stat().st_size > 0:
        return f"{HOST_SCHEME}://{HOST_FOR_URLS}/barcode/{imdb_id}_{mode}.png"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "movbar/0.0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.address_string()}] {fmt % args}\n")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_png(self, path):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, status=200, ct="text/plain"):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            return self._route()
        except Exception:
            traceback.print_exc()
            self._send_text("internal error\n", status=500)

    def _route(self):
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]

        if path == "/" or path == "":
            cached = sorted(p.name for p in worker.CACHE.glob("*.png"))
            inflight = sorted(map(str, _inflight))
            return self._send_text(
                f"movbar 0.0.1\n\nManifest: /manifest.json\n"
                f"Cached barcodes ({len(cached)}):\n" + "\n".join(cached) +
                f"\n\nInflight: {inflight}\n"
            )

        if path == "/manifest.json":
            return self._send_json(MANIFEST)

        if len(parts) == 3 and parts[0] == "meta":
            return self._handle_meta(parts[1], parts[2].rstrip(".json"))

        if len(parts) == 2 and parts[0] == "barcode":
            fn = parts[1]
            f = worker.CACHE / fn
            if f.exists() and fn.endswith(".png"):
                return self._send_png(f)
            return self._send_text("not ready\n", status=404)

        if len(parts) == 2 and parts[0] == "trigger":
            imdb_id = parts[1]
            mode = self.path.split("mode=", 1)[-1].split("&")[0] if "mode=" in self.path else "slice"
            if mode not in ("slice", "avg"):
                mode = "slice"
            kicked = kick_off(imdb_id, mode=mode)
            return self._send_json({"imdb": imdb_id, "mode": mode, "kicked": kicked})

        return self._send_text("not found\n", status=404)

    def _handle_meta(self, type_, id_):
        # Strip trailing .json that wasn't caught by the rstrip (in case id has dots)
        if id_.endswith(".json"):
            id_ = id_[:-5]

        # Series episode form: tt12345:1:1 — for now, barcode the whole show
        # by stripping season/episode (per-episode barcodes can come later)
        bare_id = id_.split(":")[0]

        try:
            meta = cinemeta_meta(type_, id_)
        except Exception:
            traceback.print_exc()
            # Without Cinemeta we can't return useful meta; let other addons handle it
            return self._send_json({"meta": None}, status=200)

        # Try the slice barcode by default; if not ready, kick off render and return original meta
        url = barcode_url_if_ready(bare_id, mode="slice")
        if url:
            meta.setdefault("meta", {})["background"] = url
        else:
            kick_off(bare_id, mode="slice")

        return self._send_json(meta)


if __name__ == "__main__":
    print(f"movbar listening on http://0.0.0.0:{PORT}  (manifest URL: {HOST_SCHEME}://{HOST_FOR_URLS}/manifest.json)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
