# SPDX-License-Identifier: BUSL-1.1
"""Verhaltensproben fuer ``web/app.js`` in einem Node-``vm`` mit Mini-DOM.

Die Web-Waechter (``test_web_render.py``) lesen den Quelltext per Regex und
sichern damit Zusagen, kein Laufzeitverhalten. Wo es auf das Verhalten
ankommt (welche Felder ein Abmelden wirklich leert, was bei einem 404 auf dem
Schirm bleibt), fuehrt dieses Modul ``web/app.js`` tatsaechlich aus:

* wie der Browser: jedes eigene Skript aus ``web/index.html``
  (``web_source.app_scripts``) als **eigenes** Skript, in Ladereihenfolge, im
  selben Kontext. Klassische Skripte teilen die globale lexikalische Umgebung,
  aber Funktionsdeklarationen werden nur innerhalb ihres Skripts vorgezogen --
  ruft ein Skript beim Laden etwas aus einem spaeter geladenen auf, scheitert
  das hier wie im Browser (aneinandergehaengt bliebe der Fehler unsichtbar),
* ohne die drei Startzeilen am Ende des letzten Skripts ``app.js``
  (``wireNav(); watchInert(); boot();``), damit kein Netz- und kein
  Ereignis-Code anlaeuft,
* in einem ``vm``-Kontext mit einem kleinen DOM-Ersatz (Elemente mit Kindern,
  Klassen, Attributen und Text; ``getElementById`` legt Wurzeln bei Bedarf an),
  speicherfaehigem ``localStorage``/``sessionStorage`` und einem ``fetch``,
  das ohne Szenario-Antwort scheitert -- **kein Test erreicht ein Netz**.

Ein Szenario ist der Rumpf einer ``async``-Funktion. Es sieht alle
Deklarationen der Client-Skripte direkt (auch ``const state`` und
``let``-Variablen, weil alle Skripte dieselbe globale lexikalische Umgebung
teilen) und dazu:

* ``respond(fn)`` -- setzt die API-Antworten: ``fn(path, method, body)``
  liefert ``{status, body}`` (``status`` Standard 200),
* ``textOf(node)`` -- der gesamte Text eines Teilbaums,
* ``calls`` -- Liste der bisherigen API-Aufrufe als ``"METHOD path"``.

Elemente merken sich ``addEventListener``; ``el.click()`` ruft die
Klick-Handler auf und liefert ein Promise, das auf asynchrone Handler wartet
(``await knopf.click()``); ein Knopf mit ``disabled`` tut nichts.
``querySelector[All]`` versteht nur ``tag``, ``.klasse``, ``tag.klasse``,
``[attribut]``, ``tag:not([attribut])`` und Listen davon (``a, b``);
auf ``document`` sucht es unter ``document.body``. Auch ``document`` merkt sich
Lauscher; ``document.dispatch("keydown", {key: "Escape"})`` loest sie aus
(etwa nach ``wireNav()``, das die globalen Tastenwege anlegt).

Sein ``return``-Wert kommt als JSON zurueck. Verwendet von den Testmodulen,
die Web-Verhalten pruefen; flach importieren (``from web_vm import ...``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from web_source import INDEX_HTML, WEB_DIR, app_scripts

#: Das letzte eigene Skript des Clients (traegt die Startzeilen); auch der
#: Anker fuer Nachbardateien wie ``APP_JS.parent / "styles.css"``.
APP_JS = WEB_DIR / "app.js"

#: Node.js-Programm; ``None`` -> Verhaltensproben werden uebersprungen.
NODE = shutil.which("node")

#: Markierung fuer Tests, die Node.js brauchen.
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js not available")

_MARK = "@@WEBVM@@"

_HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const nodePath = require("path");
const [indexPath, scenarioPath, ...scriptPaths] = process.argv.slice(2);
if (scriptPaths.length === 0) throw new Error("keine Client-Skripte uebergeben");
function mkStore() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
    key: (i) => [...m.keys()][i] ?? null,
    get length() { return m.size; },
  };
}
function mkClassList() {
  const s = new Set();
  return {
    _s: s,
    add: (...c) => c.forEach((x) => s.add(x)),
    remove: (...c) => c.forEach((x) => s.delete(x)),
    toggle: (c, f) => {
      const on = f === undefined ? !s.has(c) : !!f;
      if (on) s.add(c); else s.delete(c);
      return on;
    },
    contains: (c) => s.has(c),
  };
}
const asNode = (c) => (typeof c === "object" ? c : textNode(c));
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this._text = "";
    this.parentNode = null;
    this._value = undefined;
    this.classList = mkClassList();
    this._ls = {};
  }
  // Wie im Browser: ein select ohne gesetzten Wert hat den der gewaehlten bzw.
  // ersten Option; ein input den seines value-Attributs.
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tagName === "SELECT") {
      const opts = this._all().filter((x) => x.tagName === "OPTION");
      const pick = opts.find((o) => o.selected || o.hasAttribute("selected")) || opts[0];
      return pick ? (pick.getAttribute("value") ?? pick.textContent) : "";
    }
    return this.getAttribute("value") ?? "";
  }
  set value(v) { this._value = v == null ? "" : String(v); }
  get className() { return [...this.classList._s].join(" "); }
  set className(v) {
    this.classList._s.clear();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList._s.add(c));
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(v) { this.children = []; this._text = v == null ? "" : String(v); }
  appendChild(c) {
    if (c && typeof c === "object") {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = this;
      this.children.push(c);
    }
    return c;
  }
  append(...cs) { cs.forEach((c) => this.appendChild(asNode(c))); }
  prepend(...cs) {
    cs.reverse().forEach((c) => {
      const n = asNode(c);
      n.parentNode = this;
      this.children.unshift(n);
    });
  }
  insertBefore(n, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) return this.appendChild(n);
    n.parentNode = this;
    this.children.splice(i, 0, n);
    return n;
  }
  replaceChildren(...cs) { this.children = []; this._text = ""; this.append(...cs); }
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c);
    c.parentNode = null;
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get selectedOptions() {
    return this._all().filter(
      (x) => x.tagName === "OPTION" && (x.selected || x.hasAttribute("selected")));
  }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === document.body || n === document.documentElement;
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "class") this.className = v; }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  hasAttribute(k) { return k in this.attributes; }
  addEventListener(type, fn) { (this._ls[type] = this._ls[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this._ls[type] = (this._ls[type] || []).filter((f) => f !== fn);
  }
  dispatch(type, extra) {
    const ev = Object.assign({ type, target: this, currentTarget: this,
      preventDefault() {}, stopPropagation() {} }, extra || {});
    return Promise.all((this._ls[type] || []).map((f) => f(ev)));
  }
  _all() { return this.children.flatMap((c) => [c, ...c._all()]); }
  querySelectorAll(sel) {
    if (sel.includes(",")) {
      // Selektorliste: Treffer aller Teile, in Dokumentreihenfolge.
      const parts = sel.split(",").map((x) => x.trim());
      return this._all().filter((n) => parts.some((p) => this._matches(n, p)));
    }
    return this._all().filter((n) => this._matches(n, sel));
  }
  _matches(n, sel) {
    const neg = /^(.*):not\(\[([\w-]+)\]\)$/.exec(sel.trim());
    if (neg) return this._matches(n, neg[1] || "*") && !n.hasAttribute(neg[2]);
    if (sel.trim() === "*") return true;
    const attr = /^\[([\w-]+)\]$/.exec(sel.trim());
    if (attr) return n.hasAttribute(attr[1]);
    const m = /^([a-z0-9]*)((?:\.[\w-]+)*)$/i.exec(sel.trim());
    if (!m) return false;
    const tag = m[1].toUpperCase();
    const cls = m[2].split(".").filter(Boolean);
    return (!tag || n.tagName === tag) && cls.every((c) => n.classList.contains(c));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  contains(x) {
    for (let n = x; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  focus() { document.activeElement = this; }
  blur() {}
  click() { return this.hasAttribute("disabled") ? Promise.resolve([]) : this.dispatch("click"); }
  scrollIntoView() {}
  select() {}
  getBoundingClientRect() {
    return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
  }
}
function textNode(t) { const n = new El("#text"); n._text = String(t); return n; }
const ids = new Map();
const pageIds = new Set(
  [...fs.readFileSync(indexPath, "utf8")
    .matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
const document = {
  documentElement: new El("html"),
  body: new El("body"),
  activeElement: null,
  hidden: false,
  createElement: (t) => new El(t),
  createElementNS: (_n, t) => new El(t),
  createTextNode: textNode,
  createDocumentFragment: () => new El("#fragment"),
  // Wie im Browser: zuerst im Baum suchen; die festen Wurzeln aus index.html
  // (content, modal-root, toast-root …) entstehen beim ersten Zugriff; jede
  // andere unbekannte Kennung liefert null -- sonst griffe Code wie
  // byId("card-name-input") auf ein Phantom statt auf nichts.
  getElementById: (id) => {
    const hit = document.body._all().find((n) => n.attributes.id === id);
    if (hit) return hit;
    if (!pageIds.has(id)) return null;
    if (!ids.has(id)) {
      const root = new El("div");
      root.attributes.id = id;
      ids.set(id, root);
    }
    return ids.get(id);
  },
  querySelector: (sel) => document.body.querySelector(sel),
  querySelectorAll: (sel) => document.body.querySelectorAll(sel),
  execCommand: () => false,
  _ls: {},
  addEventListener(type, fn) { (this._ls[type] = this._ls[type] || []).push(fn); },
  removeEventListener(type, fn) {
    this._ls[type] = (this._ls[type] || []).filter((f) => f !== fn);
  },
  dispatch(type, extra) {
    const ev = Object.assign({ type, target: document.activeElement || document.body,
      preventDefault() {}, stopPropagation() {} }, extra || {});
    return Promise.all((this._ls[type] || []).map((f) => f(ev)));
  },
};
const calls = [];
let responder = null;
async function fakeFetch(url, opts) {
  const o = opts || {};
  const method = o.method || "GET";
  const path = String(url).replace(/^https?:\/\/[^/]+(\/api)?/, "");
  calls.push(method + " " + path);
  if (!responder) throw new Error("kein Szenario-fetch");
  const r = responder(path, method, o.body ? JSON.parse(o.body) : undefined) || {};
  const status = r.status || 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (r.body === undefined ? "" : JSON.stringify(r.body)),
    json: async () => r.body,
  };
}
const ctx = {
  console, document, localStorage: mkStore(), sessionStorage: mkStore(),
  location: {
    href: "http://test.invalid/", origin: "http://test.invalid",
    search: "", hash: "", pathname: "/",
  },
  navigator: { clipboard: { writeText: async () => {} } },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame: (f) => setTimeout(f, 0),
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  getComputedStyle: () => ({}),
  MutationObserver: class { observe() {} disconnect() {} },
  Node: El, HTMLElement: El,
  URL, URLSearchParams, fetch: fakeFetch, history: { replaceState() {}, pushState() {} },
  addEventListener() {}, removeEventListener() {}, innerWidth: 1400, innerHeight: 900,
  respond: (fn) => { responder = fn; },
  calls,
  textOf: (n) => (n ? n.textContent : ""),
};
ctx.window = ctx;
vm.createContext(ctx);
// Jedes Skript einzeln wie im Browser; die Startzeilen nur vom letzten (app.js)
// abschneiden. Ein Fehler beim Laden nennt die Skriptdatei.
scriptPaths.forEach((scriptPath, i) => {
  const name = nodePath.relative(nodePath.dirname(indexPath), scriptPath);
  let src = fs.readFileSync(scriptPath, "utf8");
  if (i === scriptPaths.length - 1) {
    const cut = src.lastIndexOf("wireNav();");
    if (cut < 0) throw new Error("Startzeilen von " + name + " nicht gefunden");
    src = src.slice(0, cut);
  }
  try {
    vm.runInContext(src, ctx, { filename: name });
  } catch (e) {
    process.stderr.write("Fehler beim Laden von " + name + ": "
      + (e && e.stack ? e.stack : String(e)) + "\n");
    process.exit(4);
  }
});
const scenario = fs.readFileSync(scenarioPath, "utf8");
const wrapped = "(async () => {\n" + scenario + "\n})()";
const run = vm.runInContext(wrapped, ctx, { filename: "scenario.js" });
Promise.resolve(run).then(
  (v) => {
    const out = JSON.stringify(v === undefined ? null : v);
    // Sofort beenden: offene Timer der App (z. B. Ablauf von Meldungen)
    // hielten den Prozess sonst sekundenlang am Leben.
    process.stdout.write("\n" + MARK + out + "\n", () => process.exit(0));
  },
  (e) => {
    const msg = e && e.stack ? e.stack : JSON.stringify(e);
    process.stderr.write("Szenario-Fehler: " + msg + "\n");
    process.exit(3);
  });
""".replace("MARK", json.dumps(_MARK))


def run_app_js(scenario: str, tmp_path: Path, *, timeout: float = 60.0) -> Any:
    """Fuehrt die Client-Skripte und danach ``scenario`` in Node aus.

    Die Skripte kommen in Ladereihenfolge aus ``web/index.html``
    (``web_source.app_scripts``); jedes laeuft als eigenes Skript im selben
    Kontext, vom letzten (``app.js``) ohne die Startzeilen.

    :param scenario: Rumpf einer ``async``-Funktion (JavaScript); ihr
        ``return``-Wert muss JSON-serialisierbar sein.
    :param tmp_path: Arbeitsordner fuer die beiden Hilfsdateien (pytest-Fixture).
    :param timeout: Obergrenze in Sekunden fuer den Node-Lauf.
    :returns: der ``return``-Wert des Szenarios (``None`` ohne ``return``).
    :raises AssertionError: wenn Node mit Fehler endet (Syntax- oder
        Ladefehler in einem Skript -- die Meldung nennt die Datei --, Ausnahme
        im Szenario) -- mit Node-Ausgabe im Text.

    Nur mit Node.js aufrufbar; Tests markieren sich mit :data:`needs_node`.
    """
    assert NODE is not None, "Node.js not available"
    harness = tmp_path / "webvm_harness.js"
    scen = tmp_path / "webvm_scenario.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    scen.write_text(scenario, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - feste Argumente, Dateien aus diesem Test
        [NODE, str(harness), str(INDEX_HTML), str(scen), *map(str, app_scripts())],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"Node-Lauf fehlgeschlagen:\n{result.stderr}\n{result.stdout[-2000:]}"
    )
    line = next(
        (ln for ln in reversed(result.stdout.splitlines()) if ln.startswith(_MARK)), None
    )
    assert line is not None, f"kein Ergebnis vom Szenario:\n{result.stdout[-2000:]}"
    return json.loads(line[len(_MARK):])
