# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build the wheel first so the final image does not need a compiler toolchain.
COPY pyproject.toml README.md LICENSE ./
COPY astrbot_auto_market_push ./astrbot_auto_market_push
RUN pip install --no-cache-dir .

# Run as an unprivileged user with a writable data dir for config + state.
RUN useradd --create-home --uid 10001 amp \
    && mkdir -p /data \
    && chown -R amp:amp /data

COPY docker/config.docker.yaml /data/config.example.yaml

USER amp
WORKDIR /data

ENV AMP_CONFIG=/data/config.yaml
VOLUME ["/data"]

# Default to the long running watcher; override with `ampush check` / `once`.
ENTRYPOINT ["ampush"]
CMD ["run"]
