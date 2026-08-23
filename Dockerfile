# Two runtimes in one image: gkeepapi (Python) is the only mature Google Keep
# client, and `anylist` (Node) the best-maintained AnyList one. They talk over
# loopback, so the AnyList facade never binds a public interface.

FROM node:22-slim AS node-deps
WORKDIR /app/anylist-api
COPY anylist-api/package.json ./
RUN npm install --omit=dev --no-audit --no-fund

FROM python:3.12-slim

# Node runtime, copied from the official image rather than installed from
# Debian, which ships a much older version.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN apt-get update \
 && apt-get install -y --no-install-recommends supervisor tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bridge/pyproject.toml bridge/pyproject.toml
COPY bridge/src bridge/src
RUN pip install --no-cache-dir ./bridge

COPY anylist-api/server.js anylist-api/package.json anylist-api/
COPY --from=node-deps /app/anylist-api/node_modules anylist-api/node_modules
COPY deploy/supervisord.conf /etc/supervisor/supervisord.conf

# Shadow state and the cached credentials live here; mount a volume on it or a
# restart re-syncs from scratch and re-authenticates.
RUN mkdir -p /data
VOLUME ["/data"]

ENV PYTHONUNBUFFERED=1 \
    ANYLIST_API_HOST=127.0.0.1 \
    ANYLIST_API_PORT=3000 \
    ANYLIST_API_URL=http://127.0.0.1:3000 \
    ANYLIST_CREDENTIALS_FILE=/data/.anylist_credentials \
    STATE_PATH=/data/state.sqlite \
    KEEP_STATE_PATH=/data/keep_state.json \
    HTTP_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
