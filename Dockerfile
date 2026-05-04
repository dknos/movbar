# movbar — multi-runtime container: Node (addon.js) + Python (worker.py) + ffmpeg.
# No secrets are baked in. Each user's RD token arrives per-request via the
# Stremio manifest config path segment.

FROM node:20-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pillow \
        ffmpeg \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY addon.js worker.py ./

RUN mkdir -p cache

ENV PORT=7000 \
    MOVBAR_SCHEME=https \
    PYTHON=python3

EXPOSE 7000

CMD ["node", "addon.js"]
