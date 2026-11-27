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
COPY pyproject.toml README.md requirements-ci.txt ./
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
#
# FESTE VERSIONEN (seit 2026-11-27). ``requirements-ci.txt`` geht als
# Constraints (``-c``) in die Installation: Das Image enthaelt genau die
# Versionen, die die CI geprueft hat. Vorher loeste der Bau frei auf -- am
# 2026-10-09 lag so SQLAlchemy 2.1.4 im Image, waehrend die CI 2.1.0 pruefte,
# und unter PostgreSQL hing jede Anlage (``rowcount == -1``). Der Preis: Ein
# Sicherheitsfix einer Bibliothek kommt nicht mehr von selbst, sondern ueber
# die Sperrdatei (Dependabot-Sicherheitsupdates sind aktiv).
#
# ``-c`` installiert nichts zusaetzlich, es legt nur die Versionen fest --
# Test-Werkzeuge aus der Datei landen also nicht im Image. Ein Paket, das die
# Datei gar nicht nennt, liesse ``-c`` aber frei aufloesen. Deshalb bricht der
# Bau ab, wenn danach irgendein installiertes Paket nicht in der Sperrdatei
# steht (dann die Datei neu erzeugen, Rezept in ihrem Kopf).
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -c requirements-ci.txt ".[postgres]" \
    && /opt/venv/bin/pip check \
    && /opt/venv/bin/python -c "import importlib.metadata as m, re, sys; n = lambda s: re.sub(r'[-_.]+', '-', s).lower(); pinned = {n(l.split('==')[0]) for l in open('requirements-ci.txt') if '==' in l and not l.startswith('#')}; loose = sorted({n(d.metadata['Name']) for d in m.distributions()} - pinned - {'pip', 'procworks'}); sys.exit('nicht in requirements-ci.txt: ' + ', '.join(loose)) if loose else None" \
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

# Verfuegbare Sicherheitsupdates des Basis-Image einspielen -- dasselbe Vorgehen
# wie im Web-Image (``apk --no-cache -U upgrade``), nur fuer Debian. Noetig, weil
# ``python:3.12-slim`` zwischen zwei Neubauten hinter den Debian-Sicherheits-
# aktualisierungen zurueckbleibt: Am 2026-08-30 meldete Trivy zehn HIGH-Funde in
# ``util-linux`` (CVE-2026-53612..53615) und ``openssl`` (CVE-2026-14456) des
# Basis-Image, fuer die Debian laengst gepatchte Pakete ausliefert.
#
# Anders als bei Sprachpaketen (siehe die Begruendung des zweistufigen Baus oben)
# wirkt ein In-Place-Upgrade hier auch fuer den Scanner: Trivy liest fuer
# OS-Pakete die dpkg-Datenbank des *Endimage*, nicht die Schichthistorie.
# Verifiziert am 2026-08-30 -- vor dieser Schicht 40 Funde, danach 0.
#
# ``--no-install-recommends`` haelt das Image schlank; die Paketlisten werden in
# derselben Schicht wieder entfernt, damit sie nicht im Endimage landen.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# VIER STAMMZERTIFIKATE AUSDRUECKLICH NICHT VERTRAUEN (seit 2026-11-27).
# emSign Root CA G1/C1 und emSign ECC Root CA G3/C3 gelten als unsicher: Ihre
# Zwischenzertifikate haben kompromittierte Schluessel, ``certifi`` hat sie mit
# 2026.10.18 entfernt. Debians ``ca-certificates`` (20250419+deb13u1, zu
# diesem Zeitpunkt die neueste Fassung) liefert drei davon noch aus, und ein
# ``apt-get upgrade`` aendert daran nichts. Ausgehende TLS-Verbindungen der API
# (Webhooks, Push, OIDC-Schluesselabruf, OData) nutzen genau diesen Speicher.
#
# Der vorgesehene Debian-Weg: den Eintrag in /etc/ca-certificates.conf mit
# ``!`` abwaehlen und den Speicher neu erzeugen. Danach prueft Python per
# SHA-256-Fingerabdruck, dass keines der vier Zertifikate mehr geladen wird --
# und dass ueberhaupt ein Speicher geladen wurde (mehr als 100 Zertifikate).
# ENTFERNEN, sobald Debian die Zertifikate selbst nicht mehr ausliefert (die
# Pruefung bleibt dann sinnvoll, der sed wird wirkungslos).
RUN sed -i -E 's#^(mozilla/emSign_(Root_CA_-_G1|Root_CA_-_C1|ECC_Root_CA_-_C3|ECC_Root_CA_-_G3)\.crt)$#!\1#' /etc/ca-certificates.conf \
    && update-ca-certificates --fresh > /dev/null \
    && python -c "import hashlib, ssl, sys; loaded = {hashlib.sha256(c).hexdigest() for c in ssl.create_default_context().get_ca_certs(binary_form=True)}; distrusted = {'bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3', '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b', '125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f', '40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367'}; left = sorted(distrusted & loaded); sys.exit('weiterhin vertrauenswuerdig: ' + ', '.join(left)) if left else None; sys.exit('Zertifikatsspeicher nicht geladen') if len(loaded) <= 100 else None"

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
