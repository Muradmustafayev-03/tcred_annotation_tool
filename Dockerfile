FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    TCRED_ALLOW_NON_LOOPBACK=1 \
    TCRED_ASSIGNMENT_DIR=/app/data/assignments \
    TCRED_DEPLOYMENT_MANIFEST=/app/data/deployment_manifest.json \
    TCRED_DROP_PRIVILEGES=1 \
    TCRED_RUNTIME_UID=10001 \
    TCRED_RUNTIME_GID=10001

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes --requirement requirements.lock

COPY src ./src
COPY data ./data

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8770') + '/api/health', timeout=3).read()"

CMD ["python", "-m", "tcred_annotation_tool", "serve"]
