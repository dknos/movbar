"""Barcode worker: IMDB ID -> resolve RD stream via Torrentio -> ffmpeg -> PNG."""
import json, os, subprocess, sys, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)

# Use system ffmpeg/ffprobe — the static build at ~/.local/bin segfaults on HTTPS
FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

# Load env once
ENV = {}
for line in Path.home().joinpath(".nemoclaw_env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        ENV[k.strip()] = v.strip()

RD = ENV["REALDEBRID_TOKEN"]
TORRENTIO_OPTS = "|".join([
    "providers=yts,eztv,rarbg,1337x,thepiratebay,kickasstorrents,torrentgalaxy,magnetdl",
    "sort=qualitysize",
    "limit=5",
    f"realdebrid={RD}",
])

# Output dimensions
BAR_W = 1920
BAR_H = 1080
UA = "Stremio/1.9.12"


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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
        """HEVC + HDR/DV streams are slow to decode initial buffer (we sample sparsely so
        the heavy decode for I-frame parsing dominates). Prefer x264/H.264."""
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
    # Sort by codec preference first, then quality
    candidates.sort(key=lambda s: (codec_rank(s), quality_rank(s)))
    pick = candidates[0]
    if not pick.get("url"):
        raise RuntimeError(f"stream has no url field: {pick}")
    return pick["url"], pick.get("name", ""), pick.get("title", "")


def probe_duration(url):
    out = subprocess.check_output([
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-user_agent", UA,
        url,
    ], text=True, timeout=30)
    return float(out.strip())


def render_barcode(url, out_path, mode="slice"):
    """Render barcode.

    mode='slice'  → 1px center column from each sampled frame, scaled to BAR_H
    mode='avg'    → average color (1px solid) per sample, stretched to BAR_H
    """
    duration = probe_duration(url)
    period = max(0.5, duration / BAR_W)
    fps_filter = f"fps=1/{period:.4f}"

    if mode == "slice":
        per_frame = "crop=2:in_h:in_w/2:0,scale=1:1080:flags=lanczos"
    else:
        per_frame = "scale=1:1:flags=area,scale=1:1080:flags=neighbor"

    vf = f"{fps_filter},{per_frame},tile=layout={BAR_W}x1:nb_frames={BAR_W}"
    is_http = url.startswith(("http://", "https://"))
    http_flags = []
    if is_http:
        http_flags = [
            "-user_agent", UA,
            "-multiple_requests", "1",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "30000000",
        ]
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        *http_flags,
        "-i", url,
        "-vf", vf,
        "-frames:v", "1",
        "-pix_fmt", "rgb24",
        "-progress", "pipe:2",
        str(out_path),
    ]
    print(f"[ffmpeg] duration={duration:.1f}s period={period:.3f}s mode={mode}", flush=True)
    print(f"[ffmpeg] vf={vf}", flush=True)
    print(f"[ffmpeg] cmd: {' '.join(cmd[:8])} ...", flush=True)
    # Stream stderr so we see progress in the log instead of waiting
    proc = subprocess.Popen(cmd, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, text=True, bufsize=1)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            print(f"[ff] {line}", flush=True)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {proc.returncode}")
    return out_path


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
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("imdb_id")
    p.add_argument("--mode", choices=["slice", "avg"], default="slice")
    p.add_argument("--season", type=int)
    p.add_argument("--episode", type=int)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    out, status = generate(args.imdb_id, args.mode, args.season, args.episode, args.force)
    sz = out.stat().st_size
    print(f"[done] {status}: {out} ({sz/1024:.1f} KB)")
