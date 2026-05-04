#!/usr/bin/env node
/**
 * movbar — Stremio addon that overrides movie/series background art with a
 * "movie barcode": ~320 frames evenly sampled from a Real-Debrid–cached
 * stream, stitched into a 1920×1080 image.
 *
 * Each user installs the addon with their OWN Real-Debrid API token via the
 * /configure page. Tokens are passed in the manifest URL config segment per
 * the Stremio SDK convention; they're never logged or persisted.
 *
 * Architecture:
 *   addon.js (this file) — Stremio SDK addon, meta handler, render queue
 *   worker.py            — Torrentio resolver + parallel ffmpeg seek + PIL stitch
 *
 * Renders are async: the first time a meta is requested we kick off worker.py
 * in the background and return Cinemeta's default background. On the next
 * request the cached PNG is served and Stremio shows the barcode. Cached
 * barcodes are shared across users (the rendered image is identical
 * regardless of whose token initiated the render).
 */

const express = require('express')
const path = require('path')
const fs = require('fs')
const { spawn } = require('child_process')
const { addonBuilder, getRouter } = require('stremio-addon-sdk')

const PORT = parseInt(process.env.PORT || '9450', 10)
const PUBLIC_HOST = process.env.MOVBAR_PUBLIC_HOST || `127.0.0.1:${PORT}`
const SCHEME = process.env.MOVBAR_SCHEME || 'http'
const ROOT = __dirname
const CACHE = path.join(ROOT, 'cache')
const PYTHON = process.env.PYTHON || 'python3'
const WORKER = path.join(ROOT, 'worker.py')
const CINEMETA = 'https://v3-cinemeta.strem.io'
const MAX_INFLIGHT = parseInt(process.env.MOVBAR_MAX_INFLIGHT || '3', 10)

if (!fs.existsSync(CACHE)) fs.mkdirSync(CACHE, { recursive: true })

const manifest = {
  id: 'org.movbar.barcode',
  version: '0.1.0',
  name: 'Movie Barcode',
  description:
    'Replaces movie & series backgrounds with a barcode of the film — ' +
    '320 evenly-sampled frame slices stitched into a 1920×1080 image. ' +
    'Renders in the background using your Real-Debrid + Torrentio cached ' +
    'streams. Refresh the detail page to see the barcode once it\'s ready ' +
    '(~5–10 minutes per movie).',
  resources: ['meta'],
  types: ['movie', 'series'],
  idPrefixes: ['tt'],
  catalogs: [],
  config: [
    {
      key: 'rdt',
      type: 'password',
      title: 'Real-Debrid API Token (https://real-debrid.com/apitoken)',
      required: true,
    },
    {
      key: 'mode',
      type: 'select',
      title: 'Render mode',
      options: ['slice', 'avg'],
      default: 'slice',
    },
  ],
  behaviorHints: {
    adult: false,
    p2p: false,
    configurable: true,
    configurationRequired: true,
  },
}

const builder = new addonBuilder(manifest)

// ---- token validation ----
// Real-Debrid API tokens are 52-character uppercase alphanumeric strings.
// Reject anything else before it gets near the spawn call or Torrentio URL.
function isValidRdToken(t) {
  return typeof t === 'string' && /^[A-Z0-9]{52}$/.test(t)
}

function maskToken(t) {
  if (!t || t.length < 8) return '<empty>'
  return `${t.slice(0, 4)}…${t.slice(-2)} (${t.length}ch)`
}

// ---- render queue ----
const inflight = new Map()

function renderKey(imdbId, mode) {
  return `${imdbId}_${mode}`
}

function cachedPath(imdbId, mode) {
  return path.join(CACHE, `${imdbId}_${mode}.png`)
}

function isCached(imdbId, mode) {
  try {
    const st = fs.statSync(cachedPath(imdbId, mode))
    return st.size > 0
  } catch {
    return false
  }
}

function kickRender(imdbId, mode, rdt) {
  const key = renderKey(imdbId, mode)
  if (inflight.has(key)) return false
  if (isCached(imdbId, mode)) return false
  if (inflight.size >= MAX_INFLIGHT) return false
  if (!isValidRdToken(rdt)) return false

  inflight.set(key, { startedAt: Date.now(), mode })
  console.log(`[render] start ${key} token=${maskToken(rdt)}`)

  const env = { ...process.env, MOVBAR_RD_TOKEN: rdt }
  const child = spawn(PYTHON, [WORKER, imdbId, '--mode', mode], {
    cwd: ROOT,
    stdio: ['ignore', 'inherit', 'inherit'],
    env,
  })
  child.on('exit', (code) => {
    const dt = Math.round((Date.now() - inflight.get(key).startedAt) / 1000)
    console.log(`[render] done ${key} exit=${code} ${dt}s`)
    inflight.delete(key)
  })
  child.on('error', (err) => {
    console.error(`[render] spawn error ${key}: ${err.message}`)
    inflight.delete(key)
  })
  return true
}

// ---- meta handler ----
async function fetchCinemeta(type, id) {
  const url = `${CINEMETA}/meta/${type}/${id}.json`
  const r = await fetch(url, { headers: { 'User-Agent': 'Stremio/1.9.12' } })
  if (!r.ok) throw new Error(`cinemeta ${r.status}`)
  return r.json()
}

builder.defineMetaHandler(async ({ type, id, config }) => {
  const rdt = config && typeof config === 'object' ? config.rdt : ''
  const mode = (config && config.mode) || 'slice'
  const bareId = String(id).split(':')[0]

  let cm
  try {
    cm = await fetchCinemeta(type, id)
  } catch (e) {
    console.error(`[meta] cinemeta fail ${type}/${id}: ${e.message}`)
    return { meta: null }
  }
  const meta = cm.meta || null
  if (!meta) return { meta: null }

  if (isCached(bareId, mode)) {
    meta.background = `${SCHEME}://${PUBLIC_HOST}/barcode/${bareId}_${mode}.png`
    console.log(`[meta] ${bareId} HIT mode=${mode}`)
  } else if (isValidRdToken(rdt)) {
    const kicked = kickRender(bareId, mode, rdt)
    console.log(`[meta] ${bareId} MISS mode=${mode} kicked=${kicked} token=${maskToken(rdt)}`)
  } else {
    console.log(`[meta] ${bareId} MISS no/invalid token, skipping render`)
  }
  return { meta }
})

// ---- HTTP server ----
const app = express()
app.disable('x-powered-by')

app.use((_, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*')
  res.setHeader('Access-Control-Allow-Headers', '*')
  next()
})

// Custom /configure page (overrides the SDK's auto-generated landing).
// Mounted BEFORE the SDK router so it wins.
app.get(['/', '/configure'], (req, res) => {
  res.type('html').send(CONFIGURE_HTML)
})

app.use(getRouter(builder.getInterface()))

// Static cache. Barcode PNGs are NOT user-private — they're identical regardless
// of which token initiated the render — so it's safe to serve them without auth.
app.use(
  '/barcode',
  express.static(CACHE, {
    maxAge: '1d',
    setHeaders: (res) => {
      res.setHeader('Access-Control-Allow-Origin', '*')
    },
  })
)

app.get('/healthz', (_, res) => {
  res.json({
    ok: true,
    version: manifest.version,
    cached: fs.readdirSync(CACHE).filter((f) => f.endsWith('.png')).length,
    inflight: inflight.size,
  })
})

app.listen(PORT, '0.0.0.0', () => {
  console.log(`movbar v${manifest.version} listening on http://0.0.0.0:${PORT}`)
  console.log(`configure: ${SCHEME}://${PUBLIC_HOST}/configure`)
})

// ---- /configure HTML ----
// Pure client-side: takes RD token + mode, encodes as a JSON path segment, builds
// the install URL. The token never leaves the user's browser until they tap
// Install — at which point it's transmitted over HTTPS to this addon only.
const CONFIGURE_HTML = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Movie Barcode — Configure</title>
  <style>
    :root {
      --bg: #0a0a0e;
      --card: #14141b;
      --fg: #e9e9ee;
      --muted: #8a8a98;
      --accent: #7b5cff;
      --accent2: #ff4d8d;
      --border: #25252f;
    }
    * { box-sizing: border-box }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--fg);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .barcode-bg {
      position: fixed; inset: 0;
      background: repeating-linear-gradient(
        90deg,
        #2c1810 0 4px, #4a2820 4px 8px, #1a1a2a 8px 12px,
        #3a2e1a 12px 16px, #14141b 16px 20px, #2a1f3a 20px 24px,
        #422018 24px 28px, #1f2a40 28px 32px, #20141a 32px 36px,
        #5a3a25 36px 40px, #1a2030 40px 44px, #2a2030 44px 48px
      );
      opacity: 0.35;
      pointer-events: none;
    }
    .card {
      position: relative;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 32px;
      max-width: 540px;
      width: 100%;
      box-shadow: 0 20px 60px rgba(0,0,0,.5);
    }
    h1 {
      margin: 0 0 4px;
      font-size: 22px;
      letter-spacing: -.01em;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }
    .sub {
      color: var(--muted);
      margin: 0 0 22px;
      font-size: 13px;
    }
    label {
      display: block;
      margin-top: 16px;
      font-size: 12px;
      letter-spacing: .04em;
      text-transform: uppercase;
      color: var(--muted);
    }
    input[type="password"], input[type="text"], select {
      width: 100%;
      margin-top: 6px;
      padding: 12px 14px;
      background: #0c0c12;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--fg);
      font: inherit;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 13px;
    }
    input:focus, select:focus {
      outline: none;
      border-color: var(--accent);
    }
    .hint {
      font-size: 12px;
      color: var(--muted);
      margin-top: 6px;
    }
    .hint a { color: var(--accent); text-decoration: none }
    .hint a:hover { text-decoration: underline }
    button {
      margin-top: 22px;
      width: 100%;
      padding: 14px;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      border: none;
      border-radius: 8px;
      color: white;
      font: inherit;
      font-weight: 600;
      letter-spacing: .02em;
      cursor: pointer;
      transition: transform .1s, opacity .15s;
    }
    button:hover { opacity: .9 }
    button:active { transform: translateY(1px) }
    button:disabled { opacity: .4; cursor: not-allowed }
    .row { display: flex; gap: 12px }
    .row > * { flex: 1 }
    .err {
      margin-top: 12px;
      padding: 10px 12px;
      background: rgba(255,77,141,.1);
      border: 1px solid rgba(255,77,141,.3);
      border-radius: 6px;
      color: #ff8aa8;
      font-size: 13px;
      display: none;
    }
    .err.show { display: block }
    .footer {
      margin-top: 26px;
      font-size: 12px;
      color: var(--muted);
      text-align: center;
    }
    .footer a { color: var(--accent); text-decoration: none }
  </style>
</head>
<body>
  <div class="barcode-bg"></div>
  <div class="card">
    <h1>Movie Barcode</h1>
    <p class="sub">Replaces Stremio movie &amp; series backgrounds with a barcode of every frame in the film, rendered against your Real-Debrid + Torrentio cache.</p>

    <label for="rdt">Real-Debrid API Token</label>
    <input id="rdt" type="password" autocomplete="off" spellcheck="false" placeholder="52-character uppercase alphanumeric">
    <div class="hint">Get yours at <a href="https://real-debrid.com/apitoken" target="_blank" rel="noreferrer">real-debrid.com/apitoken</a> — kept in your browser, sent only to this addon over HTTPS.</div>

    <label for="mode">Render mode</label>
    <select id="mode">
      <option value="slice">slice — 1px column from each frame (filmic)</option>
      <option value="avg">avg — average colour per frame (clean)</option>
    </select>

    <div id="err" class="err"></div>

    <button id="install" disabled>Install</button>

    <div class="footer">
      <a href="https://github.com/dknos/movbar" target="_blank" rel="noreferrer">github.com/dknos/movbar</a> · MIT
    </div>
  </div>
  <script>
    const rdt = document.getElementById('rdt')
    const mode = document.getElementById('mode')
    const btn = document.getElementById('install')
    const err = document.getElementById('err')

    function tokenValid(t) { return /^[A-Z0-9]{52}$/.test(t) }
    function update() {
      btn.disabled = !tokenValid(rdt.value.trim())
      err.classList.remove('show')
    }
    rdt.addEventListener('input', update)
    rdt.addEventListener('paste', () => setTimeout(update, 0))

    btn.addEventListener('click', () => {
      const token = rdt.value.trim()
      if (!tokenValid(token)) {
        err.textContent = 'Token must be 52 uppercase letters/digits.'
        err.classList.add('show')
        return
      }
      const cfg = { rdt: token, mode: mode.value }
      const cfgStr = encodeURIComponent(JSON.stringify(cfg))
      const host = location.host
      const url = 'stremio://' + host + '/' + cfgStr + '/manifest.json'
      window.location.href = url
    })
  </script>
</body>
</html>
`
