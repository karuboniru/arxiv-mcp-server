FROM docker.io/library/debian:bookworm-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /usr/local/bin/uv

# Match the Debian Python 3.11 interpreter and ABI in the distroless runtime.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        python3 python3-venv ca-certificates libstdc++6 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3 UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --extra pdf --no-dev --no-editable --no-cache \
    && mkdir -p /runtime-libs /data/papers \
    && cp -L /usr/lib/*-linux-gnu/libstdc++.so.6 \
        /usr/lib/*-linux-gnu/libgcc_s.so.1 \
        /usr/lib/*-linux-gnu/libgomp.so.1 /runtime-libs/

FROM gcr.io/distroless/python3-debian12:nonroot
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# Native PDF/ONNX dependencies; the runtime has no package manager or shell.
COPY --from=builder /runtime-libs/ /usr/lib/
COPY --from=builder --chown=65532:65532 /data /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8000
USER 65532:65532
EXPOSE 8000
VOLUME ["/data"]

# Read the optional bearer key at runtime, without embedding it in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os, urllib.request; key = os.environ.get('BEARER_KEY', ''); headers = {'Authorization': 'Bearer ' + key} if key else {}; request = urllib.request.Request('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', headers=headers); response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3); assert response.status == 200 and response.read() == b'ok'"]

ENTRYPOINT ["/app/.venv/bin/python", "-m", "arxiv_mcp_server"]
CMD ["--storage-path", "/data/papers"]
