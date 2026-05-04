# movbar — Stremio movie-barcode addon (spike)

## What works
- `worker.py` queries Torrentio with embedded RD config, picks a stream (prefers H.264 1080p non-Remux non-HEVC-HDR), runs ffmpeg with a `fps=1/period,scale,tile=Wx1` filter chain to produce a single PNG.
- `server.py` is a Stremio addon: `/manifest.json`, `/meta/<type>/<id>.json` (proxies Cinemeta + overrides `background`), `/barcode/<id>_<mode>.png`. Async render queue with in-flight dedup.
- Cloudflared quick tunnel exposes `http://localhost:9450` as a public HTTPS URL for Stremio to reach (no admin/portproxy needed).
- Verified pipeline on a synthetic 10-min lavfi gradient source: **10 seconds wall-clock** to render two 1920×1080 PNGs (avg + slice). Output in `cache/long_*.png`.

## What's blocked
- ffmpeg streaming directly from RD URLs stalls at ~2KB/s after filling its initial 1MB buffer. Confirmed via `/proc/$pid/io` rchar: only 633KB read after 110s of CPU time on Inception.
- Diagnosis: RD's HTTP server appears to throttle non-Range sequential reads heavily. A normal `curl --range 0-200000` against the same URL completes instantly with 17GB content-length and Accept-Ranges set. ffmpeg by default doesn't issue Range requests for sparse `fps=1/N` sampling — it walks the file linearly waiting for keyframes, exposing the throttle.
- `-multiple_requests 1 -reconnect_streamed 1 -rw_timeout` flags didn't fix it.

## Next iteration to unblock
Three options, ranked by likely-effort:

1. **Per-sample seek**: replace the single tile-filter pass with N invocations of `ffmpeg -ss <t> -i URL -frames:v 1 -vf scale=1:1080`, then PIL-stitch. Each invocation issues a Range request for the keyframe near `t`. Reliable; ~5-10s per sample × 1920 samples = ~3hr. **Cut BAR_W to 256** and it's ~10-20min per movie — visually fine, still distinctive.

2. **Pre-download via aria2c** with `-x 16 --min-split-size 1M`: parallel byte-range downloads bypass RD's drip-feed for sequential reads. 17GB Inception in ~5min on a fast connection. Then ffmpeg the local file (10s). Total ~6min/movie + 17GB disk per movie. Disk-hungry but fast.

3. **Range-friendly demuxer flags**: more research into ffmpeg's HTTP demuxer — there may be flags or AVOption combos that force Range-based seeking even for sequential `fps=1/N` extraction. Lowest effort if it exists; unknown if it does.

Recommend (1) with BAR_W=256 for v0.0.2 of this addon. The visual quality of 256-col barcodes is still good (you can see act structure, opening/credits, mood shifts) and it keeps the per-movie cost predictable.

## Files
- `worker.py` — Torrentio resolver + ffmpeg renderer
- `server.py` — addon HTTP server
- `cache/` — rendered PNGs, keyed by `<imdb_id>_<mode>.png`
- `NOTES.md` — this file

## Running
```
python3 server.py 9450
~/bin/cloudflared tunnel --url http://localhost:9450  # for public HTTPS
```

To register with the slopfactory9000 Stremio account, mutate `/tmp/stremio-install/safen_torrentio.py` pattern to push a manifest entry pointing at the cloudflared URL `+/manifest.json`.
