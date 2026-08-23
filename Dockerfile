# Two runtimes in one image: gkeepapi (Python) is the only mature Google Keep
# client, and `anylist` (Node) the best-maintained AnyList one. They talk over
# loopback, so the AnyList facade never binds a public interface.
#
# Node is the base and Python comes from Debian, rather than the reverse --
# copying a Node binary into a slim Python image relies on shared libraries
# that image is not guaranteed to carry.

FROM node:22-bookworm-slim AS node-deps
WORKDIR /app/anylist-api
COPY anylist-api/package.json ./
RUN npm install --omit=dev --no-audit --no-fund


FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv supervisor tini \
 && rm -rf /var/lib/apt/lists/*

# A venv rather than the system interpreter: Debian marks its Python as
# externally managed, and this keeps the CLI on PATH without fighting that.
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

COPY bridge/pyproject.toml bridge/pyproject.toml
COPY bridge/src bridge/src
RUN pip install --no-cache-dir ./bridge

COPY anylist-api/server.js anylist-api/package.json anylist-api/
COPY --from=node-deps /app/anylist-api/node_modules anylist-api/node_modules
COPY deploy/supervisord.conf /etc/supervisor/supervisord.conf

# Shadow state and cached credentials live here. Mount a volume on it, or a
# restart re-authenticates and treats both lists as new.
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

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
