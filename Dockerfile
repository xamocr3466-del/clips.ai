FROM node:22-bookworm-slim AS node_runtime

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    CLIPSTUDIO_DATA=/var/lib/clipstudio \
    HF_HOME=/var/lib/clipstudio-models

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.19 \
    && useradd --uid 10001 --create-home clipstudio \
    && mkdir -p /opt/clipstudio /var/lib/clipstudio /var/lib/clipstudio-models \
    && chown -R clipstudio:clipstudio /opt/clipstudio /var/lib/clipstudio /var/lib/clipstudio-models

WORKDIR /opt/clipstudio
COPY --chown=clipstudio:clipstudio pyproject.toml uv.lock .python-version ./
USER clipstudio
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=clipstudio:clipstudio app ./app

EXPOSE 8000
CMD ["uv", "run", "--frozen", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
