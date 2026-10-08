# Moteur AIOBot : world model, pilote et API FastAPI.
# Image de production sans code source : seul le bytecode Python (.pyc) est livré.
#
#   docker build --target test engine     tests lancés sur le bytecode, sans les sources
#   docker build engine                   image d'exécution (cible par défaut)

# --- Dépendances (communes à toutes les étapes) -----------------------------
FROM python:3.12-slim AS deps
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# --- Compilation : sources → bytecode, puis destruction des sources ---------
FROM deps AS compile
WORKDIR /build
COPY aiobot/ ./aiobot/
# -b : chaque module.py devient module.pyc à côté de lui (importable sans la source).
RUN python -m compileall -b -q . \
    && find . -name "*.py" -delete \
    && find . -name "__pycache__" -type d -prune -exec rm -rf {} + \
    && test -z "$(find . -name '*.py')"

# --- Tests : exécutés contre le bytecode seul -------------------------------
FROM deps AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY --from=compile /build/aiobot/ ./aiobot/
COPY models/ ./models/
COPY reports/ ./reports/
COPY tests/ ./tests/
RUN test -z "$(find aiobot -name '*.py')" && python -m pytest -p no:cacheprovider

# --- Image d'exécution (cible par défaut) -----------------------------------
FROM deps AS runtime
COPY --from=compile /build/aiobot/ ./aiobot/
COPY models/ ./models/
COPY reports/ ./reports/
RUN test -z "$(find /app -name '*.py')" \
    && useradd --create-home --uid 10001 aiobot \
    && mkdir -p /data/metrics /data/ledger \
    && touch /data/metrics/.volume /data/ledger/.volume \
    && chown -R aiobot /data
USER aiobot
ENV AIOBOT_METRICS_DIR=/data/metrics
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1
CMD ["uvicorn", "aiobot.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
