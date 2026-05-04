#!/usr/bin/env node
/**
 * movbar — Stremio addon that overrides movie/series background art with a
 * "movie barcode": ~320 frames evenly sampled from the cached Real-Debrid
 * stream, stitched into a 1920×1080 image.
 *
 * Architecture:
 *   addon.js (this file) — Stremio SDK addon, meta handler, render queue
 *   worker.py            — Torrentio resolver + parallel ffmpeg seek + PIL stitch
 *
 * Renders are async: the first time a meta is requested we kick off worker.py
 * in the background and return Cinemeta's default background. On the next
 * request the cached PNG is served and Stremio shows the barcode.
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
const DEFAULT_MODE = process.env.MOVBAR_MODE || 'slice'
const CINEMETA = 'https://v3-cinemeta.strem.io'

if (!fs.existsSync(CACHE)) fs.mkdirSync(CACHE, { recursive: true })

const manifest = {
  id: 'org.movbar.barcode',
  version: '0.0.3',
  name: 'Movie Barcode',
  description:
    'Replaces movie & series backgrounds with a "barcode" of the film — ' +
    '320 evenly-sampled 1px frame slices stitched into a 1920×1080 image. ' +
    'Renders in the background on first view via your already-configured ' +
    'Real-Debrid + Torrentio cached streams. Refresh the detail page to see ' +
    'the barcode once it\'s ready (~5–10 minutes per movie).',
  resources: ['meta'],
  types: ['movie', 'series'],
  idPrefixes: ['tt'],
  catalogs: [],
  behaviorHints: { adult: false, p2p: false, configurable: false },
}

const builder = new addonBuilder(manifest)

// ---- render queue ----
const inflight = new Map() // key -> { startedAt, mode }

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

function kickRender(imdbId, mode = DEFAULT_MODE) {
  const key = renderKey(imdbId, mode)
  if (inflight.has(key)) return false
  if (isCached(imdbId, mode)) return false

  inflight.set(key, { startedAt: Date.now(), mode })
  console.log(`[render] start ${key}`)

  const child = spawn(PYTHON, [WORKER, imdbId, '--mode', mode], {
    cwd: ROOT,
    stdio: ['ignore', 'inherit', 'inherit'],
    env: process.env,
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

builder.defineMetaHandler(async ({ type, id }) => {
  const bareId = String(id).split(':')[0]
  let cm
  try {
    cm = await fetchCinemeta(type, id)
  } catch (e) {
    console.error(`[meta] cinemeta fail for ${type}/${id}: ${e.message}`)
    return { meta: null }
  }
  const meta = cm.meta || null
  if (!meta) return { meta: null }

  if (isCached(bareId, DEFAULT_MODE)) {
    meta.background = `${SCHEME}://${PUBLIC_HOST}/barcode/${bareId}_${DEFAULT_MODE}.png`
    console.log(`[meta] ${bareId} HIT -> background overridden`)
  } else {
    const kicked = kickRender(bareId, DEFAULT_MODE)
    console.log(`[meta] ${bareId} MISS kicked=${kicked}`)
  }
  return { meta }
})

// ---- HTTP server ----
const app = express()

app.use((_, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*')
  res.setHeader('Access-Control-Allow-Headers', '*')
  next()
})

app.use(getRouter(builder.getInterface()))

app.use(
  '/barcode',
  express.static(CACHE, {
    maxAge: '1d',
    setHeaders: (res) => {
      res.setHeader('Access-Control-Allow-Origin', '*')
    },
  })
)

app.get('/', (_, res) => {
  const cached = fs.readdirSync(CACHE).filter((f) => f.endsWith('.png'))
  res
    .type('text/plain')
    .send(
      `movbar v${manifest.version}\n\n` +
        `Manifest:    /manifest.json\n` +
        `Barcode dir: /barcode/<imdbId>_<mode>.png\n` +
        `Trigger:     /trigger/<imdbId>?mode=slice|avg\n\n` +
        `Cached (${cached.length}):\n  ${cached.join('\n  ') || '<none>'}\n\n` +
        `In-flight (${inflight.size}):\n  ${
          [...inflight.entries()]
            .map(([k, v]) => `${k} (${Math.round((Date.now() - v.startedAt) / 1000)}s)`)
            .join('\n  ') || '<none>'
        }\n`
    )
})

app.get('/trigger/:id', (req, res) => {
  const mode = req.query.mode === 'avg' ? 'avg' : 'slice'
  const kicked = kickRender(req.params.id, mode)
  res.json({ id: req.params.id, mode, kicked, cached: isCached(req.params.id, mode) })
})

app.listen(PORT, '0.0.0.0', () => {
  console.log(`movbar v${manifest.version} listening on http://0.0.0.0:${PORT}`)
  console.log(`manifest: ${SCHEME}://${PUBLIC_HOST}/manifest.json`)
})
