FROM python:3.15-slim

# Set by docker buildx (bake / publish workflow) from the release tag.
ARG VERSION=dev
LABEL org.opencontainers.image.version="${VERSION}"

# Build identity served by GET /api/version. APP_VERSION empty -> the runtime
# falls back to version.txt (release-please 'simple' keeps it bumped).
ARG APP_VERSION=
ARG GIT_SHA=unknown
ENV APP_VERSION="${APP_VERSION}" GIT_SHA="${GIT_SHA}"

# Refresh Debian packages: the slim base digest can lag security uploads
# (e.g. zlib1g CVEs); keeps the Trivy CRITICAL gate meaningful.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser

WORKDIR /app

COPY requirements-mcp.txt .
# pip is not needed at runtime; its vendored msgpack/urllib3/pkg_resources
# are the image's only HIGH CVEs in Trivy
RUN pip install --no-cache-dir -r requirements-mcp.txt \
    && pip uninstall -y pip

COPY version.txt ./
COPY src/*.py ./src/
COPY src/scripts/*.py ./src/scripts/

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

EXPOSE 8080

CMD ["python", "src/main.py"]
