# syntax=docker/dockerfile:1

# Container image for the web GUI + reverse proxy (roadmap step 16).
# It bundles the static SPA assets into a Caddy image that also terminates TLS
# and reverse-proxies /api/* to the API server (see deploy/Caddyfile).
#
# Build context is the repository root so both web/ and deploy/ are available:
#   docker build -f web/Dockerfile -t procworks-web .
#
# ---------------------------------------------------------------------------
# Warum wir Caddy selbst uebersetzen (Entscheidung vom 2026-08-30)
# ---------------------------------------------------------------------------
# Frueher lag hier schlicht ``FROM caddy:2-alpine``. Das offizielle Laufzeit-
# Image bringt aber ein *fertig kompiliertes* /usr/bin/caddy mit, und dessen
# Go-Version altert zwischen zwei Caddy-Releases weiter. Am 2026-08-30 hing
# caddy:2-alpine seit dem 24.06. auf Go 1.26.3 fest; Trivy meldete 14 HIGH-Funde
# im Binary -- darunter DoS-Luecken in net/http (HTTP/2) und net/url, also genau
# im oeffentlich angebotenen Stack. Wir konnten daran nichts aendern und mussten
# sie in .trivyignore aussitzen; die Liste war so auf 13 Eintraege gewachsen und
# haette den naechsten Release blockiert.
#
# Das Bau-Image caddy:2-builder-alpine wird unabhaengig davon aktualisiert und
# trug zu diesem Zeitpunkt bereits Go 1.26.7. Ein Neu-Uebersetzen **derselben**
# Caddy-Version mit der neueren Toolchain raeumt alle stdlib-Funde ab, ohne am
# Verhalten irgendetwas zu aendern. Der Preis ist bewusst in Kauf genommen: Wir
# verantworten diesen Build jetzt selbst.
#
# Ergebnis, lokal verifiziert (nie ueber einen Release-Tag erproben!):
#   vorher  14 Funde (10 stdlib + 3 Module + 1 doppelt), 13 .trivyignore-Eintraege
#   nachher  0 Funde, .trivyignore leer
# Dazu: caddy version -> v2.11.4 (unveraendert), ``caddy validate`` gruen, SPA,
# Deep-Link-Fallback, /api-Weiterleitung und Sicherheits-Header im laufenden
# Container geprueft.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stufe 1: Caddy uebersetzen.
# ---------------------------------------------------------------------------
# ``--platform=$BUILDPLATFORM`` haelt diese Stufe auf der Architektur der
# Bau-Maschine, auch wenn fuer eine andere gebaut wird: Go **kreuz-uebersetzt**
# (GOOS/GOARCH), das braucht keine Emulation. Wuerde die Stufe stattdessen unter
# QEMU laufen, daempfte das den Go-Bau um ein Vielfaches -- bei einem Multi-Arch-
# Release also fuer jede zusaetzliche Architektur.
#
# TARGETOS/TARGETARCH setzt buildx automatisch; ohne Angabe baut ``go build``
# nativ, ein Ein-Architektur-Bau funktioniert also unveraendert.
#
# ACHTUNG: ``$BUILDPLATFORM`` kennt nur buildx. Der alte, abgekuendigte Bauer
# (``DOCKER_BUILDKIT=0``) bricht hier mit "invalid OS component" ab. Im CI liefert
# ``docker/setup-buildx-action`` es mit; lokal auf macOS: ``brew install
# docker-buildx`` (Homebrew legt das Plugin nach
# /opt/homebrew/lib/docker/cli-plugins, das ueber ``cliPluginsExtraDirs`` in
# ~/.docker/config.json gefunden wird). Danach baut ein einfaches ``docker build``
# wieder; fuer zwei Architekturen auf einmal braucht es einen Builder mit
# container-Treiber, siehe das Rezept in .trivyignore.
FROM --platform=$BUILDPLATFORM caddy:2-builder-alpine AS builder

ARG TARGETOS=linux
ARG TARGETARCH

WORKDIR /build

# main.go ist woertlich Caddys eigener Einstiegspunkt (cmd/caddy/main.go): das
# Kommando plus die Standard-Modulsammlung. Es entsteht dieselbe Binaerdatei wie
# im offiziellen Image -- keine Plugins, keine Abweichung im Funktionsumfang.
#
# Warum nicht ``xcaddy build``? Das waere der uebliche Weg, kann aber die
# Abhaengigkeits-Versionen nicht anheben: ``--with modul@version`` importiert das
# Modul zusaetzlich als *Plugin*, und golang.org/x/net hat gar kein Package im
# Wurzelpfad -- der Bau bricht mit "cannot find module providing package" ab
# (am 2026-08-30 genau so ausprobiert). Mit einem eigenen go.mod koennen wir
# ``go get`` benutzen.
#
# Die Caddy-Version kommt aus ``CADDY_VERSION`` des Bau-Image, ist also an
# dessen Tag gebunden. Beide Stufen muessen deshalb denselben Tag benutzen
# (2-builder-alpine / 2-alpine) -- sonst laege ein anderes Caddy im Image als
# das Laufzeit-Image erwartet.
#
# Die drei ``go get``-Zeilen setzen **Untergrenzen** fuer Abhaengigkeiten, deren
# von Caddy gepinnte Version eine gemeldete Luecke hat (x/net, x/text, grpc).
# Das ist gefahrlos und pflegeleicht: Gos Versionsauswahl nimmt stets das
# **Maximum** aller Anforderungen, eine Untergrenze kann also nie herabstufen.
# Verlangt ein kuenftiges Caddy eine neuere Version, gewinnt dessen Angabe und
# unsere Zeile wird wirkungslos. Sie darf dann ersatzlos entfallen.
#
# Die Schlusszeile prueft die Bau-Information des Ergebnisses statt es
# auszufuehren -- beim Kreuz-Uebersetzen laesst sich das Binary hier gar nicht
# starten. Sie schlaegt fehl, wenn eine andere als die erwartete Caddy-Version
# eingebaut wurde, und faengt damit genau den Fall ab, in dem die Tags der
# beiden Stufen auseinanderlaufen.
RUN printf '%s\n' \
      'package main' \
      '' \
      'import (' \
      '	caddycmd "github.com/caddyserver/caddy/v2/cmd"' \
      '' \
      '	// Die Standard-Modulsammlung -- identisch zum offiziellen Image.' \
      '	_ "github.com/caddyserver/caddy/v2/modules/standard"' \
      ')' \
      '' \
      'func main() { caddycmd.Main() }' > main.go \
    && go mod init procworks/caddy \
    && go get "github.com/caddyserver/caddy/v2@${CADDY_VERSION}" \
    && go mod tidy \
    && go get golang.org/x/net@v0.56.0 \
              golang.org/x/text@v0.39.0 \
              google.golang.org/grpc@v1.82.1 \
    && GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" go build -trimpath -o /usr/bin/caddy . \
    && go version -m /usr/bin/caddy \
         | grep -q "github.com/caddyserver/caddy/v2[[:space:]]\+${CADDY_VERSION}\b" 

# ---------------------------------------------------------------------------
# Stufe 2: Laufzeit.
# ---------------------------------------------------------------------------
# Basis bleibt das offizielle Laufzeit-Image: Es bringt Verzeichnisse (/config,
# /data), Umgebungsvariablen, Ports, CMD und Labels mit, die wir sonst von Hand
# nachbauen muessten. Ersetzt wird nur das Binary.
FROM caddy:2-alpine

# Apply available OS security patches on top of the pinned base image
# (e.g. OpenSSL CVE fixes that ship ahead of a new caddy image).
RUN apk --no-cache -U upgrade

# Das selbst uebersetzte Binary ueberschreibt das mitgelieferte -- am **selben
# Pfad**. Das ist wichtig: Ein *geloeschtes* Sprachpaket wuerde der Scanner
# weitertragen (siehe die Begruendung des zweistufigen API-Bau in
# core/Dockerfile), ein an derselben Stelle ersetztes Binary dagegen nicht --
# Trivy findet genau ein gobinary-Ziel und liest dessen Build-Info. Am
# 2026-08-30 verifiziert.
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# Static single-page app assets.
COPY web/ /srv/

# Reverse-proxy + TLS + static-serving configuration.
COPY deploy/Caddyfile /etc/caddy/Caddyfile

EXPOSE 80 443
