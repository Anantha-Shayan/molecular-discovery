# syntax=docker/dockerfile:1

# ---- build stage: resolve and install Python dependencies into a venv -------
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied on their own so this layer is only rebuilt when dependencies change.
COPY requirements.txt constraints.txt ./
RUN pip install -c constraints.txt -r requirements.txt


# ---- runtime stage: slim image, non-root user, no build tooling -------------
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=demo \
    PORT=8008 \
    DATA_DIR=/var/lib/mdp

# Native libraries RDKit's drawing module (rdMolDraw2D, used for 2D structure
# depiction) links against. Found by running `ldd` on the installed wheel in a
# clean slim image; the wheel does not bundle them. Kept minimal, lists removed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libxrender1 libx11-6 libxext6 libexpat1 \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user. DATA_DIR is the only writable location the app needs;
# mount a volume there to persist runs and uploaded structures.
RUN groupadd --system --gid 10001 mdp \
 && useradd  --system --uid 10001 --gid mdp --home-dir /app --shell /usr/sbin/nologin mdp \
 && mkdir -p /var/lib/mdp \
 && chown mdp:mdp /var/lib/mdp

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv

# Fail the BUILD, not the running container, if a native dependency is missing.
RUN python -c "from rdkit import Chem; from rdkit.Chem.Draw import rdMolDraw2D; from rdkit.Chem import RDConfig; import psycopg; print('native imports OK')"

# Application code, migrations, UI, and the bundled demo fixture. Runtime data
# (database files, runs, uploads) is deliberately NOT part of the image.
COPY alembic.ini ./
COPY alembic ./alembic
COPY backend ./backend
COPY frontend ./frontend
COPY data/fixtures ./data/fixtures

USER mdp
EXPOSE 8008

# Liveness only (process serving requests). Readiness, which also checks
# PostgreSQL and the schema, is /ready.
HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8008'), timeout=2)"]

CMD ["python", "-m", "backend.entrypoint"]
