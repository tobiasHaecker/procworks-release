# syntax=docker/dockerfile:1

# Container image for the procworks API server (roadmap step 16).
# Build context is the core/ directory:
#   docker build -t procworks-api core/
#
# The image is stateless; point it at PostgreSQL via DATABASE_URL. On start the
# entrypoint applies Alembic migrations (when DATABASE_URL is set) and then
# launches Uvicorn. Without DATABASE_URL the API runs against the in-memory
# stores (demo only, no durability).
#
# ZWEISTUFIGER BAU (seit 2026-08-02). Warum der Aufwand:
#
# Das frühere einstufige Image liess das gesamte Bau-Werkzeug im Laufzeit-Layer
# liegen -- pip, setuptools und alles, was pip zum Installieren kurzzeitig
# anlegt. Genutzt wird davon zur Laufzeit nichts: der Container startet Alembic
# und Uvicorn, sonst nichts. Es war damit reine Angriffsfläche, und der
# Release-Scan meldete daraus HIGH-Funde (CVE-2025-47273 in setuptools,
# GHSA-6v7p-g79w-8964 im pip-eigenen msgpack), die sich einstufig nicht
# abstellen liessen: Ein Loeschen in einer spaeteren Schicht half nicht, weil
# der Scanner das Paket aus der frueheren Schicht weitertraegt (in v1.9.2
# nachgewiesen -- im Dateisystem lag nur die gepatchte Fassung, gemeldet wurde
# trotzdem die alte). Beide Funde sind mit diesem Umbau verschwunden, ihre
# Ausnahmen in .trivyignore wurden deshalb geloescht.
#
# Genau das loest die Zweistufigkeit: Was in der Bau-Stufe entsteht und dort
# wieder verschwindet, ist in **keiner** Schicht des Endimage enthalten -- die
# Bau-Stufe wird komplett verworfen, kopiert wird nur das fertige venv. Das ist
# der Unterschied zwischen "geloescht" und "nie dagewesen".

# ---------------------------------------------------------------------------
# Stufe 1: Bauen. Alles, was zum Installieren noetig ist, bleibt hier.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Erst die Metadaten, dann die Quellen -- so bleibt die teure Installations-
# schicht im Cache, solange sich nur der Code aendert.
COPY pyproject.toml README.md ./
COPY src ./src

# In ein eigenstaendiges venv installieren, damit Stufe 2 genau ein Verzeichnis
# uebernehmen muss.
#
# ``setuptools`` wird bewusst *nicht* installiert: Seit Python 3.12 legt ``venv``
# es nicht mehr von selbst an, und gebraucht wird es hier nicht -- das Paket
# selbst baut mit hatchling in einer isolierten Bau-Umgebung, alle Abhaengig-
# keiten kommen als Wheel. Nicht installieren ist besser als nachtraeglich
# loeschen: ein ``rm`` auf das Verzeichnis liesse ``distutils-precedence.pth``
# zurueck, die beim Interpreterstart ``_distutils_hack`` laedt und dann ins
# Leere greift.
#
# ``pip`` wird gebraucht (zum Installieren) und danach entfernt -- per ``rm``
# statt ``pip uninstall pip``, weil die Selbst-Deinstallation nicht ueber alle
# pip-Versionen zugesichert ist und ein Fehlschlag hier den Bau abbraeche.
#
# Belegt vor dem Umbau: nach dem Import saemtlicher Laufzeitmodule (fastapi,
# uvicorn, sqlalchemy, alembic, psycopg, pydantic, networkx) ist weder
# ``setuptools`` noch ``pkg_resources`` geladen; click nennt pkg_resources nur
# noch im Docstring und nutzt seit 8.0 importlib.metadata. Wer eine
# Abhaengigkeit ergaenzt, die pkg_resources *importiert*, muss das erneut
# pruefen -- der Fehler faellt sonst erst beim Start des Containers auf.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[postgres]" \
    && rm -rf /opt/venv/lib/python*/site-packages/pip \
              /opt/venv/lib/python*/site-packages/pip-*.dist-info \
              /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.*

# ---------------------------------------------------------------------------
# Stufe 2: Laufzeit. Enthaelt den Interpreter, das fertige venv und die App.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Do not buffer stdout/stderr; no .pyc files in the container layer.
# PATH stellt das venv voran, damit ``python``, ``uvicorn`` und ``alembic``
# ohne absolute Pfade greifen (der Entrypoint ruft ``alembic upgrade head``).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Das Bau-Werkzeug des Basis-Image selbst: pip liegt dort aus ``--with-ensurepip``
# und wird zur Laufzeit ebenso wenig gebraucht. Entfernen ist Haertung -- nach
# einer Codeausfuehrungsluecke laesst sich nichts mehr nachinstallieren.
#
# Anders als beim frueheren einstufigen Bau wirkt die Loeschung hier auch fuer
# den Scanner: verifiziert am 2026-08-02 mit ``trivy image --severity
# HIGH,CRITICAL --ignore-unfixed`` gegen das lokal gebaute Image -- 0 Funde, und
# als python-pkg-Ziele erscheinen ausschliesslich die venv-Pakete. Weder
# ``site-packages/pip`` noch das darin gebuendelte ``msgpack`` tauchen ueberhaupt
# noch auf. Der Unterschied zum Vorgaengerversuch: dort lag die alte Fassung in
# einer *frueheren* Schicht derselben Stufe, hier existiert sie im Endimage nie.
RUN rm -rf /usr/local/lib/python*/site-packages/pip \
           /usr/local/lib/python*/site-packages/pip-*.dist-info \
           /usr/local/lib/python*/ensurepip \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
           /root/.cache

COPY --from=builder /opt/venv /opt/venv

# Migrations + Alembic config are needed at runtime for the entrypoint.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Lightweight liveness probe against the public /health endpoint.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "procworks.api:app", "--host", "0.0.0.0", "--port", "8000"]
