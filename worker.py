"""Barcode worker: IMDB ID -> resolve RD stream via Torrentio -> ffmpeg -> PNG.

Two render paths:
- HTTP URL: parallel per-sample seek (issues Range requests via -ss before -i).
  Avoids RD's sequential-read throttle. ~16 ffmpeg calls in flight.
- file:// or local path: single-pass tile filter (fast, used for synth/local).
"""
import io, json, os, re, subprocess, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image


_URL_REDACT_RE = re.compile(r"https?://[^\s'\"]+", re.IGNORECASE)


def _redact(s):
    """Strip URLs from log output. RD/Torrentio resolve URLs contain per-session
    auth tokens — never print them. Bare error messages without URLs are fine."""
    if isinstance(s, bytes):
        try:
            s = s.decode(errors="replace")
        except Exception:
            return "<undecodable>"
    return _URL_REDACT_RE.sub("<url>", str(s))

ROOT = Path(__file__).parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)

# Use system ffmpeg/ffprobe — the static build at ~/.local/bin segfaults on HTTPS
FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

def _load_rd_token():
    """Token resolution order:
    1. MOVBAR_RD_TOKEN env var (set by addon.js when serving a configured user)
    2. REALDEBRID_TOKEN env var
    3. ~/.nemoclaw_env REALDEBRID_TOKEN= line (legacy local CLI use)
    Never logged in any path.
    """
    t = os.environ.get("MOVBAR_RD_TOKEN") or os.environ.get("REALDEBRID_TOKEN")
    if t:
        return t.strip()
    env_file = Path.home() / ".nemoclaw_env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("REALDEBRID_TOKEN="):
                return line.partition("=")[2].strip()
    raise SystemExit(
        "no Real-Debrid token: set MOVBAR_RD_TOKEN or REALDEBRID_TOKEN env var"
    )


RD = _load_rd_token()
TORRENTIO_OPTS = "|".join([
    "providers=yts,eztv,rarbg,1337x,thepiratebay,kickasstorrents,torrentgalaxy,magnetdl",
    "sort=qualitysize",
    "limit=5",
    f"realdebrid={RD}",
])

BAR_W = 1920
BAR_H = 1080
SAMPLES = 320          # actual frames pulled from the stream
PARALLEL = 12          # concurrent ffmpeg seeks
SEEK_TIMEOUT = 90      # per-sample timeout (seconds)
HEAD_TRIM = 0.04       # skip first 4% (logos/leader) — keeps barcode interesting
TAIL_TRIM = 0.06       # skip last 6% (credits) — credits are mostly black bars
UA = "Stremio/1.9.12"


def fetch_json(url, timeout=60, retries=2):
    """Fetch JSON with retry. Beamup's container occasionally times out on the
    first SSL handshake to Torrentio — a single retry usually clears it."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
    raise RuntimeError(f"fetch_json failed after {retries + 1} attempts: {type(last).__name__}: {_redact(str(last))}")


def resolve_rd_url(imdb_id, season=None, episode=None):
    """Query Torrentio and return the first RD-cached stream URL."""
    if season is not None and episode is not None:
        path = f"series/{imdb_id}:{season}:{episode}.json"
    else:
        path = f"movie/{imdb_id}.json"
    url = f"https://torrentio.strem.fun/{TORRENTIO_OPTS}/stream/{path}"
    data = fetch_json(url)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"no streams for {imdb_id}")

    def is_cached(s):
        return "RD+" in (s.get("name", "") + s.get("title", ""))

    def quality_rank(s):
        blob = (s.get("name", "") + s.get("title", "")).lower()
        if "1080p" in blob: return 0
        if "720p" in blob: return 1
        if "2160p" in blob or "4k" in blob: return 2
        if "480p" in blob: return 3
        return 4

    def is_remux(s):
        return "remux" in (s.get("name", "") + s.get("title", "")).lower()

    def is_hevc_hdr(s):
        blob = (s.get("name", "") + s.get("title", "")).lower()
        return any(t in blob for t in ("x265", "h265", "hevc")) and any(
            t in blob for t in ("hdr", "dv ", "dolby vision", "dolbyvision")
        )

    def codec_rank(s):
        blob = (s.get("name", "") + s.get("title", "")).lower()
        if "x264" in blob or "h.264" in blob or "avc" in blob: return 0
        if ("x265" in blob or "h265" in blob or "hevc" in blob) and not is_hevc_hdr(s): return 1
        return 2

    candidates = [s for s in streams if is_cached(s) and not is_remux(s) and not is_hevc_hdr(s)]
    if not candidates:
        candidates = [s for s in streams if is_cached(s) and not is_remux(s)]
    if not candidates:
        candidates = [s for s in streams if is_cached(s)]
    if not candidates:
        candidates = streams
    candidates.sort(key=lambda s: (codec_rank(s), quality_rank(s)))
    pick = candidates[0]
    if not pick.get("url"):
        raise RuntimeError(f"stream has no url field for {imdb_id}")
    return pick["url"], pick.get("name", ""), pick.get("title", "")


def probe_duration(url):
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-user_agent", UA,
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe timeout") from None
    if proc.returncode != 0:
        # Never let raw subprocess error bubble — process.args contains the URL.
        raise RuntimeError(
            f"ffprobe rc={proc.returncode}: {_redact(proc.stderr[-200:])}"
        )
    return float(proc.stdout.strip())


def _per_frame_filter(mode):
    if mode == "slice":
        # crop=2 in middle, scale to 1×BAR_H
        return f"crop=2:in_h:(in_w-2)/2:0,scale=1:{BAR_H}:flags=lanczos"
    # avg: collapse to 1×1 (area-average), then stretch to 1×BAR_H
    return f"scale=1:1:flags=area,scale=1:{BAR_H}:flags=neighbor"


def _http_flags():
    return [
        "-user_agent", UA,
        "-multiple_requests", "1",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-rw_timeout", "60000000",
        "-seekable", "1",
    ]


def _seek_one(url, t, mode, idx, total):
    """Seek to time t, decode 1 frame, apply per-frame filter, return PNG bytes."""
    vf = _per_frame_filter(mode)
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-noaccurate_seek",
        *_http_flags(),
        "-ss", f"{t:.3f}",
        "-i", url,
        "-frames:v", "1",
        "-vf", vf,
        "-pix_fmt", "rgb24",
        "-f", "image2pipe",
        "-c:v", "png",
        "-",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=SEEK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return idx, None, f"timeout@{t:.0f}s"
    elapsed = time.time() - t0
    if proc.returncode != 0 or not proc.stdout:
        return idx, None, f"rc={proc.returncode} {_redact(proc.stderr[-200:])!r}"
    return idx, proc.stdout, f"ok {elapsed:.1f}s"


def _render_streamed(url, out_path, mode):
    """HTTP path: parallel per-sample seek + PIL stitch."""
    duration = probe_duration(url)
    print(f"[seek] duration={duration:.1f}s samples={SAMPLES} mode={mode} parallel={PARALLEL}", flush=True)

    head = duration * HEAD_TRIM
    tail = duration * (1 - TAIL_TRIM)
    times = [head + (tail - head) * (i + 0.5) / SAMPLES for i in range(SAMPLES)]

    columns = [None] * SAMPLES
    failures = []
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futs = [ex.submit(_seek_one, url, t, mode, i, SAMPLES) for i, t in enumerate(times)]
        for fut in as_completed(futs):
            idx, png, status = fut.result()
            done += 1
            if png is None:
                failures.append((idx, status))
                if len(failures) <= 3:
                    print(f"[seek] {idx}/{SAMPLES} FAIL {status}", flush=True)
            else:
                columns[idx] = png
            if done % 32 == 0 or done == SAMPLES:
                ok = sum(1 for c in columns if c is not None)
                print(f"[seek] {done}/{SAMPLES} done ok={ok} fails={len(failures)} t={time.time()-t0:.0f}s", flush=True)

    ok_count = sum(1 for c in columns if c is not None)
    if ok_count < SAMPLES * 0.5:
        raise RuntimeError(f"too many seek failures: {ok_count}/{SAMPLES}")

    # Forward-fill missing columns from neighbors (rare misses)
    last_good = None
    for i in range(SAMPLES):
        if columns[i] is not None:
            last_good = columns[i]
        elif last_good is not None:
            columns[i] = last_good
    for i in range(SAMPLES - 1, -1, -1):
        if columns[i] is not None:
            last_good = columns[i]
        elif last_good is not None:
            columns[i] = last_good

    # Stitch: SAMPLES wide → BAR_W wide via NEAREST resize (preserves bar boundaries)
    canvas = Image.new("RGB", (SAMPLES, BAR_H))
    for i, png in enumerate(columns):
        if png is None:
            continue
        col = Image.open(io.BytesIO(png))
        canvas.paste(col, (i, 0))
    final = canvas.resize((BAR_W, BAR_H), Image.NEAREST)
    final.save(out_path, format="PNG", optimize=True)
    print(f"[seek] stitched {ok_count}/{SAMPLES} ok in {time.time()-t0:.0f}s -> {out_path}", flush=True)
    return out_path


def _render_local(url, out_path, mode):
    """Local file or file://: single-pass tile filter (fast, used for synth/local)."""
    duration = probe_duration(url)
    period = max(0.5, duration / BAR_W)
    fps_filter = f"fps=1/{period:.4f}"
    per_frame = _per_frame_filter(mode)
    vf = f"{fps_filter},{per_frame},tile=layout={BAR_W}x1:nb_frames={BAR_W}"
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-i", url,
        "-vf", vf,
        "-frames:v", "1",
        "-pix_fmt", "rgb24",
        str(out_path),
    ]
    print(f"[local] duration={duration:.1f}s period={period:.3f}s mode={mode}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {proc.returncode}: {_redact(proc.stderr[-400:])}")
    return out_path


def render_barcode(url, out_path, mode="slice"):
    if url.startswith(("http://", "https://")):
        return _render_streamed(url, out_path, mode)
    return _render_local(url, out_path, mode)


def generate(imdb_id, mode="slice", season=None, episode=None, force=False):
    suffix = f"_{season}_{episode}" if season is not None else ""
    out = CACHE / f"{imdb_id}{suffix}_{mode}.png"
    if out.exists() and not force:
        return out, "cached"
    rd_url, name, title = resolve_rd_url(imdb_id, season, episode)
    print(f"[resolve] {imdb_id} -> {name.splitlines()[0] if name else '?'}")
    render_barcode(rd_url, out, mode=mode)
    return out, "rendered"


if __name__ == "__main__":
    import argparse, traceback
    p = argparse.ArgumentParser()
    p.add_argument("imdb_id")
    p.add_argument("--mode", choices=["slice", "avg"], default="slice")
    p.add_argument("--season", type=int)
    p.add_argument("--episode", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--samples", type=int, help="override SAMPLES")
    p.add_argument("--parallel", type=int, help="override PARALLEL")
    args = p.parse_args()
    if args.samples:
        SAMPLES = args.samples
    if args.parallel:
        PARALLEL = args.parallel
    # Redact any URL-bearing exception before it reaches stderr — uncaught Python
    # tracebacks include subprocess args + variable reprs that may contain URLs.
    try:
        out, status = generate(
            args.imdb_id, args.mode, args.season, args.episode, args.force
        )
    except Exception as e:
        msg = _redact(traceback.format_exc())
        sys.stderr.write(f"[error] {args.imdb_id} mode={args.mode}\n{msg}\n")
        sys.exit(1)
    sz = out.stat().st_size
    print(f"[done] {status}: {out} ({sz/1024:.1f} KB)")
