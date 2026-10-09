// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Graph.
 *
 * Layout und Darstellung des Prozessgraphen: Schichtung des
 * blockstrukturierten Modells, Knotendarstellung, Datenherkunft,
 * Befund-Markierungen, Verschieben und Zoomen.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// Graph-Layout (Longest-Path-Layering, blockstrukturierter DAG)
// --------------------------------------------------------------------------

// Chip-Metrik der Knoten-Badges. Wird von renderNodeBadges (Zeichnen) UND von
// layoutSchema (Platz reservieren) verwendet, damit der reservierte vertikale
// Platz exakt zur gezeichneten Badge-Hoehe passt.
const CHIP_H = 16, CHIP_GAP = 3, CHIP_TOP = 5, CHIP_BOTTOM = 4, CHIP_PADX = 7, CHIP_CH = 6;

// Reine Chip-Modelle eines Knotens: je Datenbindung ein benannter Chip plus –
// falls vorhanden – ein Bearbeiter-Chip. Ab dem dritten Datenelement wird zu
// einem Sammel-Chip verdichtet. Von Zeichnung und Layout gemeinsam genutzt,
// damit beide dieselbe Anzahl/Reihenfolge sehen (kein Auseinanderdriften).
function nodeChipModels(schema, node) {
  if (node.type !== NODE_TYPE.ACTIVITY && node.type !== NODE_TYPE.SUBPROCESS) return [];
  const accesses = (schema.data_accesses || []).filter((a) => a.node_id === node.id);
  const rule = (schema.staff_rules || {})[node.id];
  const sym = (mode) => (mode === "WRITE" ? "✎" : mode === "READ_WRITE" ? "⇄" : "◉");
  const chips = [];
  if (accesses.length) {
    const named = accesses.slice(0, accesses.length <= 3 ? 3 : 2);
    named.forEach((a) => {
      const e = schema.data_elements[a.element_id];
      const name = e ? e.name : a.element_id;
      chips.push({
        kind: "data",
        label: sym(a.mode) + " " + truncate(name, 16),
        title: name + " (" + accessModeLabel(a.mode) + (a.mandatory ? ", Pflicht" : ", optional") + ")",
      });
    });
    const rest = accesses.length - named.length;
    if (rest > 0) {
      const detail = accesses.slice(named.length).map((a) => {
        const e = schema.data_elements[a.element_id];
        return (e ? e.name : a.element_id) + " (" + accessModeLabel(a.mode) + ")";
      }).join(", ");
      chips.push({ kind: "data", label: "+" + rest + " Daten", title: "Weitere Datenbindungen: " + detail });
    }
  }
  if (rule) chips.push({ kind: "staff", label: "Bearbeiter", title: describeRule(rule) });
  return chips;
}

// Vertikaler Platz, den der untereinander gestapelte Badge-Block eines Knotens
// unter seinem Rechteck einnimmt (0 ohne Chips). layoutSchema addiert dies auf
// die Knotenhoehe, damit Badges nie in den naechsten Knoten ragen.
function nodeBadgeStackHeight(schema, node) {
  const n = nodeChipModels(schema, node).length;
  return n ? CHIP_TOP + n * CHIP_H + (n - 1) * CHIP_GAP + CHIP_BOTTOM : 0;
}

// Geometrie-Konstanten des Kontrollfluss-Layouts (von beiden Layout-Varianten
// geteilt): Knotenbreite/-hoehe, horizontaler/vertikaler Abstand, Rand.
const LAYOUT_NW = 144, LAYOUT_NH = 56, LAYOUT_HGAP = 74, LAYOUT_VGAP = 26, LAYOUT_PAD = 32;
// Zusatzabstand zwischen zwei Geschwister-Aesten einer Verzweigung, in
// Lane-Einheiten (0 = Aeste nur um eine Bahn getrennt).
const LAYOUT_BRANCH_GAP = 0.25;

// Bevorzugtes Layout: horizontale "Spine" mit symmetrischen Aesten
// (layoutSchemaSpine). Schlaegt die Ableitung aus der Blockstruktur fehl (nicht
// wohlgeformte Kanten, Zyklus, Ueberlappung), faellt es verlustfrei auf das
// bisherige, robuste gestapelte Layout (layoutSchemaStacked) zurueck.
// Stabilitaet vor Optik: eine fehlerhafte Zeichnung darf nie entstehen.
// K4: SYNC-Kanten sind reine Warte-Beziehungen (Ziel wartet, bis die Quelle
// abgeschlossen oder abgewählt ist) und für JEDE Strukturlogik unsichtbar –
// Layout, Zweig-/Schleifenerkennung, Verschiebe-Ziele und Nachbarschaft
// arbeiten ausschließlich auf controlEdges. Gezeichnet werden Sync-Kanten
// separat (renderGraph, gestrichelt).
function controlEdges(schema) {
  return (schema.edges || []).filter((e) => e.type !== "SYNC");
}
function syncEdges(schema) {
  return (schema.edges || []).filter((e) => e.type === "SYNC");
}

function layoutSchema(schema) {
  try {
    return layoutSchemaSpine(schema);
  } catch (_e) {
    return layoutSchemaStacked(schema);
  }
}

// Spine-Layout (Nutzeranforderung): Alle Aktivitaeten liegen auf einer
// gemeinsamen horizontalen Achse; Datenobjekte/Bearbeiter haengen als Badges
// darunter. Der Hauptfluss verlaeuft moeglichst gerade horizontal von links
// nach rechts; an Verzweigungen fanen die Aeste symmetrisch nach oben/unten aus
// und laufen am zugehoerigen Join wieder auf der Achse zusammen. Das Modell ist
// block-strukturiert (garantiert durch die K-Regeln des Kerns), daher laesst
// sich die Lane (vertikale Bahn) je Knoten rekursiv aus der Blockstruktur
// ableiten:
//  * Spalte  = Laengster-Pfad-Tiefe (Fluss schreitet nach rechts fort).
//  * Lane 0  = Spine; jeder Knoten mit genau einem Vorgaenger erbt dessen Lane
//             (der Hauptpfad bleibt gerade).
//  * Split   = seine Aeste werden symmetrisch um die Split-Lane verteilt, die
//             Bandbreite je Ast aus dessen gemessener Hoehe.
//  * Join    = kehrt auf die Split-Lane (Spine) zurueck.
// Wirft bei nicht wohlgeformter Struktur -> Fallback greift.
function layoutSchemaSpine(schema) {
  const nodes = schema.nodes || {};
  const ids = Object.keys(nodes);
  if (!ids.length) return { pos: {}, edges: controlEdges(schema), width: 560, height: 160 };

  const out = {}, inc = {};
  ids.forEach((id) => { out[id] = []; inc[id] = []; });
  controlEdges(schema).forEach((e) => {
    if (out[e.source] && nodes[e.target]) out[e.source].push(e.target);
    if (inc[e.target] && nodes[e.source]) inc[e.target].push(e.source);
  });

  // Spalte = Laengster-Pfad-Tiefe (wie bisher). Ein Zyklus ist in einem
  // block-strukturierten Prozess ausgeschlossen -> wirft (Fallback).
  const depth = {}, visiting = {};
  function d(id) {
    if (depth[id] !== undefined) return depth[id];
    if (visiting[id]) throw new Error("cycle");
    visiting[id] = true;
    let m = 0;
    inc[id].forEach((p) => { m = Math.max(m, d(p) + 1); });
    visiting[id] = false;
    return (depth[id] = m);
  }
  ids.forEach(d);

  // Genau eine Wurzel (START) erwartet; sonst ist der Fluss nicht eindeutig
  // horizontal auffaedelbar -> Fallback.
  const roots = ids.filter((id) => inc[id].length === 0);
  if (roots.length !== 1) throw new Error("not single-rooted");

  // Passender Join eines Splits = gemeinsamer Nachfahre aller Split-Kinder mit
  // kleinster Tiefe (dort schliesst der Block). Memoisiert.
  const joinCache = {};
  function descendants(id) {
    const seen = new Set(), stack = [id];
    while (stack.length) {
      const n = stack.pop();
      out[n].forEach((s) => { if (!seen.has(s)) { seen.add(s); stack.push(s); } });
    }
    return seen;
  }
  function matchingJoin(splitId) {
    if (joinCache[splitId]) return joinCache[splitId];
    let common = null;
    out[splitId].forEach((k) => {
      const ds = descendants(k);
      ds.add(k); // leerer Ast: das Kind kann selbst schon der Join sein
      common = common === null ? ds : new Set([...common].filter((x) => ds.has(x)));
    });
    if (!common || !common.size) throw new Error("no matching join");
    let best = null;
    common.forEach((c) => { if (best === null || depth[c] < depth[best]) best = c; });
    return (joinCache[splitId] = best);
  }

  // Einzigen Nachfolger eines Joins liefern (oder die Sequenz-Grenze/das Ende).
  const onlySucc = (j, stop) => (j === stop ? stop : (out[j].length ? out[j][0] : null));

  // Vertikale Ausdehnung (Lane-Einheiten) der Sequenz [start .. stop): linearer
  // Knoten = 1; ein verschachtelter Split traegt die Summe seiner Ast-Hoehen.
  // Entlang der Sequenz zaehlt das Maximum (Segmente liegen horizontal
  // hintereinander, nicht uebereinander).
  function measure(start, stop) {
    let node = start, span = 1, guard = 0;
    while (node !== stop && node != null) {
      if (guard++ > ids.length + 2) throw new Error("runaway measure");
      const succ = out[node];
      if (succ.length > 1) {
        const j = matchingJoin(node);
        let block = LAYOUT_BRANCH_GAP * (succ.length - 1);
        succ.forEach((c) => { block += measure(c, j); });
        span = Math.max(span, block);
        node = onlySucc(j, stop);
      } else {
        node = succ.length === 1 ? succ[0] : null;
      }
    }
    return span;
  }

  // Sequenz [start .. stop) auf Mittellinie ``center`` platzieren; Splits fanen
  // ihre Aeste symmetrisch um ``center`` aus (Block als Ganzes zentriert).
  const lane = {}, placed = new Set(), emptyLane = {};
  function place(start, center, stop) {
    let node = start, guard = 0;
    while (node !== stop && node != null) {
      if (guard++ > ids.length + 2) throw new Error("runaway place");
      if (placed.has(node)) throw new Error("revisit");
      placed.add(node);
      lane[node] = center;
      const succ = out[node];
      if (succ.length > 1) {
        const j = matchingJoin(node);
        const heights = succ.map((c) => measure(c, j));
        const total = heights.reduce((a, b) => a + b, 0) + LAYOUT_BRANCH_GAP * (succ.length - 1);
        let cursor = center - total / 2;
        succ.forEach((c, i) => {
          // Leerer Zweig (direkte Kante Split -> Join): die reservierte Bahn
          // merken, sonst zeichnete renderGraph die Kante gerade auf der
          // Mittellinie -- quer durch den Knoten eines anderen Zweigs.
          if (c === j) emptyLane[`${node}->${j}`] = cursor + heights[i] / 2;
          place(c, cursor + heights[i] / 2, j);
          cursor += heights[i] + LAYOUT_BRANCH_GAP;
        });
        if (j === stop) { node = stop; }        // Grenz-Join gehoert dem Aufrufer
        else { lane[j] = center; placed.add(j); node = onlySucc(j, stop); }
      } else {
        node = succ.length === 1 ? succ[0] : null;
      }
    }
  }
  place(roots[0], 0, null);
  if (ids.some((id) => lane[id] === undefined)) throw new Error("unplaced nodes");

  // Lane -> Pixel. Einheitliche Bahn-Hoehe = Knotenhoehe + hoechster Badge-
  // Stapel + Abstand. Dadurch beruehrt ein oberer Knoten (Badges haengen nach
  // unten) nie den in derselben Spalte darunter liegenden Knoten, egal welche
  // Lanes benachbart sind.
  let maxBadge = 0;
  ids.forEach((id) => { maxBadge = Math.max(maxBadge, nodeBadgeStackHeight(schema, nodes[id])); });
  const rowPitch = LAYOUT_NH + maxBadge + LAYOUT_VGAP;

  // Vertikalen Ursprung so waehlen, dass der oberste Knoten PAD Abstand hat.
  // Auch die Bahnen leerer Zweige zaehlen: liegt eine ganz oben oder unten,
  // muss die Zeichenflaeche sie noch enthalten.
  let minCenter = Infinity;
  ids.forEach((id) => { minCenter = Math.min(minCenter, lane[id] * rowPitch); });
  Object.values(emptyLane).forEach((ln) => { minCenter = Math.min(minCenter, ln * rowPitch); });
  const originY = LAYOUT_PAD + LAYOUT_NH / 2 - minCenter;

  const pos = {};
  let maxBottom = 0, maxCol = 0;
  ids.forEach((id) => {
    const yc = originY + lane[id] * rowPitch;
    pos[id] = { x: LAYOUT_PAD + depth[id] * (LAYOUT_NW + LAYOUT_HGAP), y: yc - LAYOUT_NH / 2, w: LAYOUT_NW, h: LAYOUT_NH };
    maxCol = Math.max(maxCol, depth[id]);
    maxBottom = Math.max(maxBottom, pos[id].y + LAYOUT_NH + nodeBadgeStackHeight(schema, nodes[id]));
  });

  // Sicherheitsnetz: ueberlappt trotz allem ein Knotenpaar (inkl. Badge-
  // Stapel), lieber das robuste gestapelte Layout nehmen.
  if (layoutHasOverlap(pos, schema)) throw new Error("overlap");

  // Bahn-Mitte (Pixel) je leerem Zweig, Schluessel "split->join".
  const edgeLanes = {};
  Object.entries(emptyLane).forEach(([key, ln]) => {
    edgeLanes[key] = originY + ln * rowPitch;
    // Platz fuer Bedingung oben und „+“ unten an der Bahn.
    maxBottom = Math.max(maxBottom, edgeLanes[key] + LAYOUT_NH / 2);
  });
  const width = LAYOUT_PAD * 2 + (maxCol + 1) * LAYOUT_NW + maxCol * LAYOUT_HGAP;
  const height = maxBottom + LAYOUT_PAD;
  return { pos, edges: controlEdges(schema), edgeLanes,
    width: Math.max(width, 560), height: Math.max(height, 160) };
}

// Prueft, ob sich zwei Knoten-Kaesten (Rechteck inkl. darunter haengendem
// Badge-Stapel) ueberlappen. Rein defensiv fuer das Spine-Layout.
function layoutHasOverlap(pos, schema) {
  const ids = Object.keys(pos);
  const box = (id) => {
    const p = pos[id];
    return { x1: p.x, y1: p.y, x2: p.x + p.w, y2: p.y + p.h + nodeBadgeStackHeight(schema, schema.nodes[id]) };
  };
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      const a = box(ids[i]), b = box(ids[j]);
      if (a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2) return true;
    }
  }
  return false;
}

// Fallback-Layout: das bisherige spaltenweise gestapelte, je Spalte vertikal
// zentrierte Layout. Robust fuer beliebige (auch nicht sauber block-
// strukturierte) Kantenlagen; kommt zum Zug, wenn layoutSchemaSpine wirft.
function layoutSchemaStacked(schema) {
  const ids = Object.keys(schema.nodes);
  const inc = {}, out = {};
  ids.forEach((id) => { inc[id] = []; out[id] = []; });
  controlEdges(schema).forEach((e) => {
    if (out[e.source]) out[e.source].push(e);
    if (inc[e.target]) inc[e.target].push(e);
  });
  const depth = {}, visiting = {};
  function d(id) {
    if (depth[id] !== undefined) return depth[id];
    if (visiting[id]) return 0;
    visiting[id] = true;
    let m = 0;
    inc[id].forEach((e) => { m = Math.max(m, d(e.source) + 1); });
    visiting[id] = false;
    return (depth[id] = m);
  }
  ids.forEach(d);
  const cols = {};
  ids.forEach((id) => { (cols[depth[id]] = cols[depth[id]] || []).push(id); });
  const colKeys = Object.keys(cols).map(Number).sort((a, b) => a - b);
  const NW = LAYOUT_NW, NH = LAYOUT_NH, HGAP = LAYOUT_HGAP, VGAP = LAYOUT_VGAP, PAD = LAYOUT_PAD;
  // Effektive Hoehe je Knoten = Rechteck + darunter gestapelter Badge-Block.
  // Dadurch wird eine Spalte bei Bedarf auseinandergezogen, sodass die
  // Datenbindungs-/Bearbeiter-Chips nie den naechsten Knoten ueberlagern.
  const effH = {};
  ids.forEach((id) => { effH[id] = NH + nodeBadgeStackHeight(schema, schema.nodes[id]); });
  const colTotal = {};
  colKeys.forEach((c) => {
    const list = cols[c];
    colTotal[c] = list.reduce((s, id) => s + effH[id], 0) + VGAP * Math.max(0, list.length - 1);
  });
  const height = PAD * 2 + Math.max(NH, ...colKeys.map((c) => colTotal[c]));
  const pos = {};
  colKeys.forEach((c, ci) => {
    const list = cols[c];
    let y = PAD + (height - PAD * 2 - colTotal[c]) / 2;
    list.forEach((id) => {
      pos[id] = { x: PAD + ci * (NW + HGAP), y, w: NW, h: NH };
      y += effH[id] + VGAP;
    });
  });
  const width = PAD * 2 + colKeys.length * NW + (colKeys.length - 1) * HGAP;
  return { pos, edges: controlEdges(schema), width: Math.max(width, 560), height: Math.max(height, 160) };
}

function nodeClass(node, instance) {
  if (instance && instance.node_states && instance.node_states[node.id]) {
    return "gnode s-" + instance.node_states[node.id];
  }
  if (node.type === NODE_TYPE.START || node.type === NODE_TYPE.END) return "gnode nstart";
  if (node.type === NODE_TYPE.SUBPROCESS) return "gnode nsub";
  if (GATEWAYS.has(node.type)) return "gnode ngateway";
  return "gnode ndefault";
}

/** Benennung unbenannter Verzweigungsknoten (fuer nodeCaptionInContext). */
const GATEWAY_KIND_NAMES = {
  XOR_SPLIT: "Entscheidung", XOR_JOIN: "Ende der Entscheidung",
  AND_SPLIT: "Parallele Zweige", AND_JOIN: "Ende der parallelen Zweige",
};

/**
 * Beschriftung eines Knotens mit Zusammenhang, wo die Kurzform nicht
 * unterscheidet: Mehrere unbenannte Verzweigungen hiessen alle „XOR ▶“ bzw.
 * „▶ UND“ (Ad-hoc-Auswahl, Simulation). Hier heissen sie nach dem Schritt
 * davor: „Entscheidung nach „Betrag erfassen““. Benannte Knoten bleiben, wie
 * sie sind.
 * @param {object} schema Schema
 * @param {object} node Knoten
 * @returns {string}
 */
function nodeCaptionInContext(schema, node) {
  const unnamedStep = node && !node.label && (node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS);
  if (!node || node.label || (!GATEWAYS.has(node.type) && !unnamedStep)) return node ? nodeCaption(node) : "";
  const predsOf = (id) => controlEdges(schema).filter((e) => e.target === id).map((e) => schema.nodes[e.source]);
  // Nach dem naechsten benannten Vorgaenger benennen und die unbenannten
  // Knoten dazwischen zaehlen („2. Schritt ohne Bezeichnung nach …“) -- ohne
  // Rekursion, sonst verschachtelten sich Ketten unbenannter Knoten.
  const kindKey = (x) => (x.type === NODE_TYPE.SUBPROCESS ? NODE_TYPE.ACTIVITY : x.type);
  let steps = 1, before = null, cur = node;
  for (let guard = 0; guard < 200; guard++) {
    const preds = predsOf(cur.id).filter(Boolean);
    if (!preds.length) break;
    const named = preds.find((p) => p.label);
    if (named) { before = nodeCaption(named); break; }
    if (preds.length > 1) {
      // Mehrere unbenannte Vorgaenger: Steht der Lauf schon an einem anderen
      // Knoten (Ende einer Verzweigung), ist dieser der Bezug; am Ausgangsknoten
      // selbst gibt es keinen („Ende der Entscheidung“ ohne „nach …“).
      if (cur !== node) before = GATEWAY_KIND_NAMES[cur.type] || nodeCaption(cur);
      break;
    }
    const p = preds[0];
    if (p.type === NODE_TYPE.START) { before = nodeCaption(p); break; }
    // Gleiche Bezeichnung = gleiche Zaehlung: Aktivitaet und Teilprozess
    // heissen beide „Schritt ohne Bezeichnung“.
    if (kindKey(p) === kindKey(node)) steps++;
    cur = p;
  }
  const kind = (steps > 1 ? `${steps}. ` : "")
    + (unnamedStep ? "Schritt ohne Bezeichnung" : GATEWAY_KIND_NAMES[node.type] || nodeCaption(node));
  return before ? `${kind} nach \u201E${before}\u201C` : kind;
}

/**
 * Beschriftung eines Zweigs (Kante Split -> erster Knoten): der erste Schritt,
 * bei einem leeren Zweig (Kante direkt zum Join) „leerer Zweig“ -- jeweils mit
 * Bedingung, falls vorhanden.
 * @param {object} schema Schema
 * @param {string} splitId Split
 * @param {string} targetId erster Knoten bzw. Join
 * @returns {string}
 */
function branchCaption(schema, splitId, targetId) {
  const edge = controlEdges(schema).find((e) => e.source === splitId && e.target === targetId);
  const target = schema.nodes[targetId];
  const empty = target && (target.type === NODE_TYPE.XOR_JOIN || target.type === NODE_TYPE.AND_JOIN);
  const name = empty ? "leerer Zweig" : nodeCaption(target || { type: "", label: targetId });
  const cond = edge && edge.condition ? conditionCaption(edge.condition) : "";
  return cond ? `${name} (${cond})` : name;
}

function nodeCaption(node) {
  if (node.label) return node.label;
  return { START: "Start", END: "Ende", AND_SPLIT: "UND \u25B6", AND_JOIN: "\u25B6 UND",
    XOR_SPLIT: "XOR \u25B6", XOR_JOIN: "\u25B6 XOR", SUBPROCESS: "Teilprozess",
    LOOP_START: "\u21BB Wiederholen", LOOP_END: "Bis erf\u00FCllt \u21BB" }[node.type] || nodeTypeLabel(node.type);
}

// Berechnet die Datenherkunft-Linien (Schreib- -> Lese-Knoten) fuer die
// gestrichelte Ueberlagerung im Kontrollfluss. ``focus`` steuert den Umfang:
//  * ``dataElemFocus`` gesetzt -> alle Lesestellen genau dieses Datenelements
//    werden mit ihren Schreibquellen verbunden (Palette-Klick).
//  * sonst ``selectedNode`` gesetzt -> es werden nur die vom gewaehlten Knoten
//    *gelesenen* Groessen zu ihren Schreibquellen aufgeloest.
// Rueckgabe: Liste eindeutiger {from, to, label}. Rein lesend; veraendert nie
// Modell oder Backend (die Korrektheit von D1 stellt der Kern sicher).
function computeProvenance(schema, focus) {
  focus = focus || {};
  const accesses = schema.data_accesses || [];
  const isRead = (m) => m === "READ" || m === "READ_WRITE";
  const isWrite = (m) => m === "WRITE" || m === "READ_WRITE";
  // Zu erklaerende (Element, Lese-Knoten)-Paare bestimmen.
  let reads = [];
  if (focus.dataElemFocus) {
    reads = accesses
      .filter((a) => a.element_id === focus.dataElemFocus && isRead(a.mode))
      .map((a) => ({ element_id: a.element_id, node_id: a.node_id }));
  } else if (focus.selectedNode) {
    reads = accesses
      .filter((a) => a.node_id === focus.selectedNode && isRead(a.mode))
      .map((a) => ({ element_id: a.element_id, node_id: a.node_id }));
  }
  const seen = new Set();
  const lines = [];
  reads.forEach((r) => {
    const elem = schema.data_elements[r.element_id];
    const label = elem ? elem.name : r.element_id;
    accesses
      .filter((w) => w.element_id === r.element_id && isWrite(w.mode) && w.node_id !== r.node_id)
      .forEach((w) => {
        const key = `${w.node_id}->${r.node_id}:${r.element_id}`;
        if (seen.has(key)) return;
        seen.add(key);
        lines.push({ from: w.node_id, to: r.node_id, label: truncate(label, 14) });
      });
  });
  return lines;
}

// Paare (LOOP_START -> LOOP_END) samt Rumpfmenge, per Vorwärtslauf mit
// Tiefenzählung – der Spiegel von model.loop_block im Kern (K6a garantiert die
// saubere Paarung; ein unpaariger Start kann gar nicht erst entstehen). Rein
// lesend, wird für den gezeichneten Rücksprung-Bogen gebraucht, denn die
// Rücksprungkante existiert bewusst nicht als Datum.
function loopPairsOf(schema) {
  const out = {};
  controlEdges(schema).forEach((e) => { (out[e.source] = out[e.source] || []).push(e.target); });
  const pairs = [];
  Object.values(schema.nodes || {}).forEach((n) => {
    if (n.type !== NODE_TYPE.LOOP_START) return;
    const body = new Set();
    const stack = (out[n.id] || []).map((t) => [t, 0]);
    let end = null;
    while (stack.length) {
      const [id, depth] = stack.pop();
      const node = schema.nodes[id];
      if (!node || body.has(id)) continue;
      if (node.type === NODE_TYPE.LOOP_END && depth === 0) { end = id; continue; }
      body.add(id);
      let d = depth;
      if (node.type === NODE_TYPE.LOOP_START) d += 1;
      else if (node.type === NODE_TYPE.LOOP_END) d -= 1;
      (out[id] || []).forEach((t) => stack.push([t, d]));
    }
    if (end) pairs.push({ start: n.id, end, body });
  });
  return pairs;
}

// Deutsche Kurzbeschreibung der Wiederhol-Bedingung einer LoopDecision –
// die EINE geteilte Quelle für den Rücksprung-Bogen (renderGraph) und das
// Schleifen-Panel (loopNodePanel), damit die Oberflächen nie driften.
// Boolesche Kurzform (S1): „X“ = wahr/falsch. Partition (S3): die
// Wiederhol-Zellen werden als kompakte Wertbereiche aufgezählt (THRESHOLD als
// [untere–obere) Bereiche, ENUM als Wertemenge bzw. „sonst“).
function loopConditionCaption(schema, d, maxLen) {
  if (!d) return null;
  const elem = (schema.data_elements || {})[d.discriminator];
  const name = truncate(elem ? elem.name : d.discriminator, maxLen || 14);
  if (!d.cells || !d.cells.length) {
    return `„${name}“ = ${d.repeat_value ? "wahr" : "falsch"}`;
  }
  if (d.kind === "THRESHOLD") {
    const parts = [];
    let lower = null;
    d.cells.forEach((c) => {
      if (c.repeat) {
        if (lower === null) parts.push(`< ${c.upper}`);
        else if (c.upper == null) parts.push(`≥ ${lower}`);
        else parts.push(`${lower} – ${c.upper}`);
      }
      lower = c.upper;
    });
    return `„${name}“ ${parts.join(" oder ")}`;
  }
  if (d.kind === "BOOLEAN") {
    const rep = d.cells.filter((c) => c.repeat).map((c) => (c.bool_value ? "wahr" : "falsch"));
    return `„${name}“ = ${rep.join("/")}`;
  }
  // ENUM: aufgezählte Wiederhol-Werte; wiederholt der Sonst-Zweig, steht das dabei.
  const rep = d.cells.filter((c) => c.repeat);
  const named = rep.filter((c) => !c.is_else).flatMap((c) => c.values || []);
  const bits = [];
  if (named.length) bits.push(`∈ {${named.map((v) => truncate(v, 10)).join(", ")}}`);
  if (rep.some((c) => c.is_else)) bits.push("sonst");
  return `„${name}“ ${bits.join(" oder ")}`;
}

/**
 * Zeichnet eine Kontrollfluss-Kante in den Graphen von ``renderGraph``: den
 * Pfad, ihre Bedingung bzw. „leerer Zweig“ und -- wenn die Sicht es anbietet --
 * den Einfuege-Knopf „+“ auf der Kante.
 *
 * Ein leerer Zweig laeuft ueber seine eigene Bahn aus dem Layout
 * (``L.edgeLanes``), damit Beschriftung und „+“ nicht in einem fremden Knoten
 * landen. Rein darstellend; Reihenfolge der angehaengten Elemente: Pfad,
 * Beschriftung, „+“.
 *
 * @param {SVGElement} root das ``<svg>``, an das angehaengt wird
 * @param {object} L Ergebnis von ``layoutSchema`` (``pos``, ``edgeLanes``)
 * @param {object} schema das gezeichnete Schema (fuer die Kollisionspruefung
 *   der Beschriftung)
 * @param {{source: string, target: string, condition: ?object}} e die Kante
 * @param {object} opts Optionen von ``renderGraph`` (``instance`` faerbt
 *   signalisierte Kanten, ``onPlus`` blendet den Einfuege-Knopf ein)
 * @returns {void} Fehlt die Position eines Endknotens, wird nichts gezeichnet.
 */
function renderControlEdge(root, L, schema, e, opts) {
  const a = L.pos[e.source], b = L.pos[e.target];
  if (!a || !b) return;
  const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
  const mx = (x1 + x2) / 2;
  // Leerer Zweig: ueber seine eigene Bahn fuehren (aus dem Layout), mit
  // Bedingung und „+“ auf der Bahn statt in einem fremden Knoten.
  const laneY = L.edgeLanes && L.edgeLanes[`${e.source}->${e.target}`];
  const emptyBranch = laneY !== undefined && Math.abs(laneY - y1) > 1;
  const ly = emptyBranch ? laneY : (y1 + y2) / 2;
  let cls = "gedge";
  if (opts.instance && opts.instance.edge_states) {
    const st = opts.instance.edge_states[`${e.source}->${e.target}`];
    if (st === "TRUE_SIGNALED") cls += " gedge-true";
    else if (st === "FALSE_SIGNALED") cls += " gedge-false";
  }
  const k = Math.min(40, (x2 - x1) / 4);
  const d = emptyBranch
    ? `M ${x1} ${y1} C ${x1 + k} ${y1}, ${x1 + k} ${ly}, ${x1 + 2 * k} ${ly} L ${x2 - 2 * k} ${ly} C ${x2 - k} ${ly}, ${x2 - k} ${y2}, ${x2} ${y2}`
    : `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
  root.appendChild(svg("path", { class: cls, "marker-end": "url(#arrow)", d,
    "data-edge": `${e.source}->${e.target}` }));
  if (e.condition || emptyBranch) {
    const caption = (emptyBranch ? "leerer Zweig" : "")
      + (e.condition ? (emptyBranch ? ": " : "") + conditionCaption(e.condition) : "");
    // Volle Beschriftung, solange sie keinen Knoten (samt Badges) schneidet
    // -- bei schraegen Zweigkanten liegt sie zwischen den Bahnen im Freien.
    // Sonst in die Luecke zwischen den Knoten einpassen: bis zu zwei Zeilen,
    // danach gekuerzt; der volle Text steht im Tooltip.
    const fullW = caption.length * 5.5;
    const fullBox = { x0: mx - fullW / 2, x1: mx + fullW / 2, y0: ly - 16, y1: ly - 4 };
    const lines = labelHitsNode(L, schema, fullBox) ? fitCaption(caption, x2 - x1 - 8) : [caption];
    const t = svg("text", { class: "gcond", x: mx, y: ly - 6 - (lines.length - 1) * 11, "text-anchor": "middle" });
    lines.forEach((line, i) => t.appendChild(
      svg("tspan", { x: mx, dy: i ? 11 : 0 }, document.createTextNode(line))));
    if (lines.join(" ") !== caption) t.appendChild(svg("title", null, document.createTextNode(caption)));
    root.appendChild(t);
  }
  if (opts.onPlus) {
    // data-tour/-src: Anker der gefuehrten Tour. Sie zeigt gezielt auf das
    // "+" EINER bestimmten Kante, deshalb reist die Quellknoten-Id mit; das
    // Ziel braucht der Einfuegedialog, wenn die Quelle mehrere Ausgaenge hat
    // (Anfang eines Zweigs).
    const g = svg("g", { class: "gplus-wrap", style: "cursor:pointer",
      "data-tour": "model.plus", "data-tour-src": e.source,
      onClick: () => opts.onPlus(e.source, e.target) });
    g.appendChild(svg("circle", { class: "gplus", cx: mx, cy: ly + 10, r: 10 }));
    g.appendChild(svg("text", { class: "gplus-txt", x: mx, y: ly + 14, "text-anchor": "middle" }, document.createTextNode("+")));
    root.appendChild(g);
  }
}

/**
 * Geometrie der gestrichelten Datenherkunft: je Eintrag ein Bogen vom
 * Schreib- zum Lese-Knoten, leicht nach oben gehoben, damit er nicht mit den
 * Kontrollflusskanten zusammenfaellt (mehr Abstand bei weiter Strecke).
 *
 * @param {object} L Ergebnis von ``layoutSchema``
 * @param {?Array<{from: string, to: string, label: ?string}>} provenance
 *   Herkunftseintraege aus ``computeProvenance``; fehlt die Angabe, gibt es
 *   keine Boegen
 * @returns {object[]} je Bogen Endpunkte (``x1,y1,x2,y2``), Scheitel ``my``,
 *   die beiden Knotenrechtecke (``from``/``to``) und die Beschriftung mit
 *   Startposition (``lx``/``ly``). Eintraege, deren Knoten nicht gezeichnet
 *   sind, fallen weg.
 */
function provenanceArcs(L, provenance) {
  return (provenance || []).map((pv) => {
    const a = L.pos[pv.from], b = L.pos[pv.to];
    if (!a || !b) return null;
    const x1 = a.x + a.w / 2, y1 = a.y, x2 = b.x + b.w / 2, y2 = b.y;
    const lift = 34 + Math.min(60, Math.abs(x2 - x1) * 0.12);
    const my = Math.min(y1, y2) - lift;
    // ``a``/``b`` reisen mit, damit unten der Bereich bestimmt werden kann, den
    // die Herkunft insgesamt einnimmt (Bogen *und* beide beteiligten Knoten).
    return { x1, y1, x2, y2, my, from: a, to: b,
      label: pv.label, lx: (x1 + x2) / 2, ly: my + 4 };
  }).filter(Boolean);
}

/**
 * Staffelt die Beschriftungen der Herkunftsboegen senkrecht, damit keine
 * eine andere ueberdeckt -- etwa wenn mehrere gelesene Groessen am selben
 * Leseknoten enden.
 *
 * Vorgehen: nach Bogenspitze (oben zuerst) sortieren und
 * jedes Label so weit nach oben schieben, dass es kein bereits platziertes
 * Label in der Naehe (gleicher horizontaler Bereich) mehr ueberdeckt.
 *
 * @param {object[]} provItems Boegen aus ``provenanceArcs``; ``ly`` wird an
 *   Ort und Stelle verschoben (nur Eintraege mit Beschriftung)
 * @param {number} lineHeight Zeilenhoehe einer Beschriftung -- um sie wird
 *   ein kollidierendes Label nach oben versetzt
 * @returns {void} Hoechstens 50 Verschiebungen je Label, damit die Schleife
 *   auch bei vielen Labels sicher endet.
 */
function staggerProvenanceLabels(provItems, lineHeight) {
  const placed = [];
  provItems.slice().sort((p, q) => p.ly - q.ly).forEach((pv) => {
    if (!pv.label) return;
    let guard = 0;
    let hit = true;
    while (hit && guard++ < 50) {
      hit = false;
      for (const q of placed) {
        if (Math.abs(q.lx - pv.lx) < 64 && Math.abs(q.ly - pv.ly) < lineHeight) {
          pv.ly = q.ly - lineHeight; hit = true; break;
        }
      }
    }
    placed.push(pv);
  });
}

function renderGraph(schema, opts) {
  opts = opts || {};
  const L = layoutSchema(schema);
  const root = svg("svg", { class: "graph", width: L.width, height: L.height, viewBox: `0 0 ${L.width} ${L.height}` });
  const defs = svg("defs", null,
    svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
      svg("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#6b7794" })),
    // Eigener (bernsteinfarbener) Pfeilkopf fuer die gestrichelten
    // Datenherkunft-Linien (Schreib- -> Lese-Knoten), damit sie sich klar vom
    // Kontrollfluss abheben.
    svg("marker", { id: "arrow-prov", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
      svg("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#e0a03a" })));
  root.appendChild(defs);

  // Kanten
  L.edges.forEach((e) => renderControlEdge(root, L, schema, e, opts));

  // Rücksprung-Bögen der Schleifen (K6): Die Rücksprungkante ist bewusst nie
  // gespeichert (der Graph bleibt azyklisch) – gezeichnet wird sie aus der
  // LOOP_START/LOOP_END-Paarung: vom Unterrand des Schleifenendes unter dem
  // Rumpf hindurch zurück an den Unterrand des Schleifenanfangs. Der Bogen
  // taucht unter die tiefste Bahn des Blocks (dort hängen auch die Badges);
  // ragt er unter die viewBox hinaus, wird sie nach unten erweitert –
  // das Spiegelbild der Herkunfts-Erweiterung nach oben (s. u.).
  // K4: Sync-Kanten gestrichelt zwischen den Zweigen zeichnen (Warte-
  // Beziehung; deutlich unterschieden von Kontrollfluss und Datenherkunft).
  syncEdges(schema).forEach((e) => {
    const ps = L.pos[e.source], pt = L.pos[e.target];
    if (!ps || !pt) return;
    const down = pt.y >= ps.y + ps.h;
    const x1 = ps.x + ps.w / 2, y1 = down ? ps.y + ps.h : ps.y;
    const x2 = pt.x + pt.w / 2, y2 = down ? pt.y : pt.y + pt.h;
    const ym = (y1 + y2) / 2;
    root.appendChild(svg("path", { class: "gsyncedge", "marker-end": "url(#arrow)",
      d: `M ${x1} ${y1} C ${x1} ${ym}, ${x2} ${ym}, ${x2} ${y2}` }));
  });

  let vbBottom = L.height;
  loopPairsOf(schema).forEach(({ start, end, body }) => {
    const ps = L.pos[start], pe = L.pos[end];
    if (!ps || !pe) return;
    // Unterkante inklusive der Badge-Stapel unter den Knoten: sonst lag die
    // Bogenbeschriftung („↻ solange …“) auf Daten-/Bearbeiter-Badges.
    const bottomOf = (id, p) => p.y + p.h + nodeBadgeStackHeight(schema, schema.nodes[id]);
    let low = bottomOf(start, ps);
    body.forEach((id) => { const p = L.pos[id]; if (p) low = Math.max(low, bottomOf(id, p)); });
    low = Math.max(low, bottomOf(end, pe));
    const dip = low + 30;
    const xe = pe.x + pe.w / 2, ye = pe.y + pe.h;
    const xs = ps.x + ps.w / 2, ys = ps.y + ps.h;
    root.appendChild(svg("path", { class: "gloop", "marker-end": "url(#arrow)",
      d: `M ${xe} ${ye} C ${xe} ${dip}, ${xs} ${dip}, ${xs} ${ys + 4}` }));
    const d = (schema.loop_decisions || {})[end];
    const condition = loopConditionCaption(schema, d, 14);
    const maxSuffix = d && d.max_iterations ? ` · max ${d.max_iterations}×` : "";
    const caption = condition ? `↻ solange ${condition}${maxSuffix}` : "↻ wiederholen";
    root.appendChild(svg("text", { class: "gloop-txt", x: (xs + xe) / 2, y: dip - 5, "text-anchor": "middle" },
      document.createTextNode(caption)));
    vbBottom = Math.max(vbBottom, dip + 12);
  });
  if (vbBottom > L.height) {
    root.setAttribute("height", vbBottom);
    root.setAttribute("viewBox", `0 0 ${L.width} ${vbBottom}`);
  }

  // Datenherkunft (gestrichelt): fuer jede gelesene Groesse ein Bogen vom
  // Schreib- zum Lese-Knoten. Rein visuell; ``opts.provenance`` wird von der
  // Modellieren-Sicht aus ``computeProvenance`` gefuellt. Ein leicht nach oben
  // versetzter Bogen vermeidet Deckung mit den Kontrollflusskanten.
  //
  // Zwei Sichtbarkeitsprobleme werden hier bewusst behandelt, damit die
  // Beschriftung (Name des Datenelements) *immer* lesbar bleibt:
  //  1. Der Bogen hebt sich um bis zu ~94px ueber die oberste Knotenzeile
  //     (die schon bei y=PAD sitzt) und ragt damit oberhalb der viewBox
  //     (y=0) aus dem sichtbaren Bereich. Wir bestimmen den obersten
  //     erreichten Punkt und erweitern die viewBox bei Bedarf nach oben.
  //  2. Enden mehrere Boegen am selben Leseknoten (mehrere gelesene Groessen),
  //     liegen ihre Beschriftungen fast uebereinander. Wir staffeln
  //     kollidierende Labels vertikal nach oben.
  const provItems = provenanceArcs(L, opts.provenance);

  // Labels vertikal entzerren; die Masse braucht unten auch der Herkunfts-Bereich.
  const LBL_LINE = 12; // Zeilenhoehe der 9px-Beschriftung inkl. kleinem Rand
  const LBL_HALF_W = 45; // grobe halbe Breite einer (auf 14 Zeichen gekuerzten) Beschriftung
  staggerProvenanceLabels(provItems, LBL_LINE);

  // viewBox nach oben erweitern, falls Bogen oder Label ueber y=0 hinausragen
  // (Bogenscheitel ~pv.my, Labeloberkante ~pv.ly-9). So wird nichts mehr
  // abgeschnitten; die Modellsicht waechst nur nach oben, Knoten bleiben fix.
  let topY = 0;
  provItems.forEach((pv) => { topY = Math.min(topY, pv.my - 2, pv.ly - 10); });
  // Der Schnellring des gewaehlten Knotens (siehe renderNodeRing) sitzt UEBER
  // dem Knotenrechteck. Bei einem Knoten auf der obersten Bahn (y = LAYOUT_PAD)
  // ragt er sonst ueber den Rand der viewBox hinaus und waere unklickbar --
  // deshalb geht er in dieselbe Erweiterung nach oben ein wie die Herkunft.
  const ringPos = opts.onNodeAction && opts.selectedId ? L.pos[opts.selectedId] : null;
  if (ringPos && nodeRingActions(schema, schema.nodes[opts.selectedId]).length) {
    topY = Math.min(topY, ringPos.y - RING_LIFT - RING_R - 2);
  }
  if (topY < 0) {
    const vbTop = topY - 6;
    root.setAttribute("viewBox", `0 ${vbTop} ${L.width} ${vbBottom - vbTop}`);
    root.setAttribute("height", vbBottom - vbTop);
  }

  provItems.forEach((pv) => {
    root.appendChild(svg("path", {
      class: "gprov", "marker-end": "url(#arrow-prov)",
      d: `M ${pv.x1} ${pv.y1} C ${pv.x1} ${pv.my}, ${pv.x2} ${pv.my}, ${pv.x2} ${pv.y2}` }));
    if (pv.label) {
      root.appendChild(svg("text", { class: "gprov-txt", x: pv.lx, y: pv.ly, "text-anchor": "middle" },
        document.createTextNode(pv.label)));
    }
  });

  // Umschliessender Bereich der Datenherkunft (Bogen + Beschriftung + **beide**
  // beteiligten Knoten). Er wandert an die Canvas, damit das Einrasten auf den
  // gewaehlten Knoten die Herkunft nicht aus dem Bild schiebt: Der Schreiber
  // liegt typischerweise mehrere hundert Pixel weiter links, sodass ein reines
  // Zentrieren auf den Leseknoten genau das versteckte, was der Bogen zeigen
  // soll -- die Verbindung zwischen den beiden Schritten (siehe centerOn).
  const provBounds = provItems.length ? provItems.reduce((acc, pv) => {
    const parts = [
      [pv.from.x, pv.from.y, pv.from.w, pv.from.h],
      [pv.to.x, pv.to.y, pv.to.w, pv.to.h],
      [pv.lx - LBL_HALF_W, Math.min(pv.my, pv.ly - LBL_LINE), LBL_HALF_W * 2, LBL_LINE],
    ];
    parts.forEach(([x, y, w, h]) => {
      acc.x0 = Math.min(acc.x0, x); acc.y0 = Math.min(acc.y0, y);
      acc.x1 = Math.max(acc.x1, x + w); acc.y1 = Math.max(acc.y1, y + h);
    });
    return acc;
  }, { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity }) : null;

  // Knoten
  Object.entries(L.pos).forEach(([id, p]) => {
    const node = schema.nodes[id];
    let cls = nodeClass(node, opts.instance);
    if (opts.selectedId === id) cls += " selected";
    // data-node-id: adressierbare Knoten-Gruppe (z. B. für die
    // Simulations-Abspielanimation, die Schritte nacheinander hervorhebt).
    const g = svg("g", { class: cls, "data-node-id": id,
      style: opts.onSelectNode ? "cursor:pointer" : "",
      onClick: opts.onSelectNode ? () => opts.onSelectNode(id) : null });
    g.appendChild(svg("rect", { x: p.x, y: p.y, width: p.w, height: p.h, rx: 10 }));
    // Gekuerzte Bezeichnung: die volle steht als Tooltip am Knoten.
    const caption = nodeCaption(node);
    if (caption.length > 18) g.appendChild(svg("title", null, document.createTextNode(caption)));
    g.appendChild(svg("text", { class: "glabel", x: p.x + p.w / 2, y: p.y + p.h / 2 - 2, "text-anchor": "middle" },
      document.createTextNode(truncate(caption, 18))));
    // E2-Status-Overlay (Stufe C): ein angehaltener
    // oder gescheiterter Schritt ist direkt in der Prozesslandkarte sichtbar,
    // nicht erst in der Aufgabenliste -- Statustext + Randfarbe am Knoten.
    const detail = opts.instance && opts.instance.node_details
      ? opts.instance.node_details[id] : null;
    let sub = opts.instance && opts.instance.node_states
      ? nodeStateLabel(opts.instance.node_states[id] || "") : nodeTypeLabel(node.type);
    // Soll-Ist-Sicht (opts.observed, aus /schemas/{id}/conformance): Haeufigkeit
    // und mittlere Dauer am Schritt; nie ausgefuehrte Schritte blass.
    const obs = opts.observed && opts.observed[id];
    if (obs) {
      sub = obs.completed ? `${obs.completed}× · Ø ${fmtStepDuration(obs.avg_total_seconds)}` : "nie ausgeführt";
      if (!obs.completed) g.classList.add("obs-none");
    }
    if (detail === "SUSPENDED") { sub += " · angehalten"; g.classList.add("d-suspended"); }
    else if (detail === "FAILED") { sub += " · gescheitert"; g.classList.add("d-failed"); }
    g.appendChild(svg("text", { class: "gstate", x: p.x + p.w / 2, y: p.y + p.h / 2 + 14, "text-anchor": "middle" },
      document.createTextNode(sub)));
    // Iterationszähler (K6, rein beobachtend): Wie oft hat diese Schleife
    // bereits wiederholt? Nur in Laufzeit-Sichten (Instanz vorhanden) und nur,
    // wenn mindestens einmal wiederholt wurde.
    const iters = opts.instance && opts.instance.loop_iterations
      ? opts.instance.loop_iterations[id] : null;
    if (node.type === NODE_TYPE.LOOP_END && iters) {
      g.appendChild(svg("text", { class: "gloop-iter", x: p.x + p.w / 2, y: p.y + p.h + 14, "text-anchor": "middle" },
        document.createTextNode(`↻ ${iters}× wiederholt`)));
    }
    root.appendChild(g);
    renderNodeBadges(root, schema, node, p, opts);
    renderNodeFindingMark(root, node, p, opts);
    renderNodeRing(root, schema, node, p, opts);
  });

  // Einpassen-Knopf oben rechts **im** Canvas (nicht im Panel-Kopf): so steht er
  // in jeder Sicht zur Verfuegung, die einen Kontrollfluss zeichnet -- auch in
  // Ausfuehrung/Monitoring, wo es keinen Panel-Kopf mit Knoepfen gibt. Er holt
  // ein verschobenes/gezoomtes Modell wieder vollstaendig ins Bild; derselbe
  // Weg wie der Doppelklick mit der mittleren Maustaste (siehe attachPanZoom).
  const fitBtn = el("button", {
    class: "canvas-fit",
    type: "button",
    title: "Modell einpassen – erster Klick: lesbar ab Start, zweiter Klick: ganze Übersicht (auch: Doppelklick mit der mittleren Maustaste)",
    "aria-label": "Modell in die Ansicht einpassen",
    onClick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (wrap._panzoom) wrap._panzoom.fitToView();
    },
  }, "\u2922 Einpassen");
  const wrap = el("div", { class: "canvas-wrap", "data-tour": "model.graph" }, root, fitBtn,
    el("div", { class: "canvas-hint" }, "Scrollen/Wischen: Verschieben \u00B7 Strg/Pinch: Zoom \u00B7 Ziehen: Verschieben"));
  // Der Herkunfts-Bereich haengt an der Canvas, weil nur sie ihre eigene Groesse
  // kennt (das Einpassen passiert erst nach dem Einhaengen ins Dokument).
  wrap._provBounds = provBounds;
  attachPanZoom(wrap, root);
  if (opts.fitOnShow) fitWhenVisible(wrap);
  else if (opts.instance) fitWhenVisible(wrap, activeRegion(L, opts.instance));
  return wrap;
}

/**
 * Schneidet ein Beschriftungsrechteck (Modellkoordinaten) einen Knoten samt
 * darunter haengendem Badge-Stapel?
 * @param {{pos: Object<string, {x:number,y:number,w:number,h:number}>}} L Layout
 * @param {object} schema Schema (fuer die Badge-Hoehe)
 * @param {{x0:number,x1:number,y0:number,y1:number}} box Rechteck
 * @returns {boolean}
 */
function labelHitsNode(L, schema, box) {
  return Object.entries(L.pos).some(([id, p]) => {
    const bottom = p.y + p.h + nodeBadgeStackHeight(schema, (schema.nodes || {})[id] || {});
    return box.x0 < p.x + p.w && box.x1 > p.x && box.y0 < bottom && box.y1 > p.y;
  });
}

/**
 * Bricht eine Kantenbeschriftung auf hoechstens zwei Zeilen um, die in eine
 * Breite in Pixeln passen (Schaetzung ~5,5 px je Zeichen bei 10 px Schrift);
 * was dann noch uebersteht, endet mit „…“. Umbrochen wird an Leerzeichen.
 * @param {string} text Beschriftung
 * @param {number} px verfuegbare Breite
 * @returns {string[]} eine oder zwei Zeilen (eine, wenn sie passt)
 */
function fitCaption(text, px) {
  const max = Math.max(4, Math.floor(px / 5.5));
  if (text.length <= max) return [text];
  const words = text.split(" ");
  const lines = [""];
  for (const w of words) {
    const cur = lines[lines.length - 1];
    if (!cur || (cur + " " + w).length <= max) lines[lines.length - 1] = cur ? cur + " " + w : w;
    else lines.push(w);
  }
  const cut = (l) => (l.length <= max ? l : l.slice(0, max - 1) + "\u2026");
  if (lines.length <= 2) return lines.map(cut);
  return [cut(lines[0]), cut(lines.slice(1).join(" "))];
}

/**
 * Bereich (Modellkoordinaten) um die gerade aktiven Schritte eines Vorgangs.
 *
 * Eine Live-Landkarte startete oben links im Bild; bei einem grossen Prozess
 * (Order-to-Cash) lag dort nichts, und man sah eine leere Flaeche, bis man
 * „Einpassen“ drueckte. Jetzt rueckt die Karte die bereiten bzw. laufenden
 * Schritte ins Bild -- auch nach jedem Neuaufbau.
 *
 * @param {{pos: Object<string, {x:number,y:number,w:number,h:number}>}} L Layout
 * @param {{node_states?: Object<string, string>}} instance Vorgang bzw. Simulation
 * @returns {{x0:number,y0:number,x1:number,y1:number}|null} Bereich, oder null
 *   ohne aktiven Schritt (dann wird das ganze Modell eingepasst)
 */
function activeRegion(L, instance) {
  const states = (instance && instance.node_states) || {};
  const ids = Object.keys(L.pos).filter((id) => states[id] === "ACTIVATED" || states[id] === "RUNNING");
  if (!ids.length) return null;
  const r = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
  ids.forEach((id) => {
    const p = L.pos[id];
    r.x0 = Math.min(r.x0, p.x); r.y0 = Math.min(r.y0, p.y);
    r.x1 = Math.max(r.x1, p.x + p.w); r.y1 = Math.max(r.y1, p.y + p.h);
  });
  return r;
}

/** Hoechstzahl Bilder, die fitWhenVisible auf das Einhaengen wartet (~3 s). */
const FIT_WAIT_FRAMES = 180;

/**
 * Passt eine Zeichenflaeche ein, sobald sie im Dokument haengt und Groesse hat.
 *
 * ``fitToView`` braucht die tatsaechliche Groesse des Rahmens -- die gibt es
 * erst nach dem Einhaengen. Eine Sicht, die ihren Graphen loesgeloest baut und
 * erst danach anhaengt (Soll/Ist-Karte im Monitoring), stand deshalb beim
 * ersten Oeffnen leer da: das Modell lag ausserhalb des Ausschnitts, und man
 * musste selbst "Einpassen" druecken.
 *
 * Wartet hoechstens etwa drei Sekunden (180 Bilder) -- danach ist die Flaeche
 * entweder da oder die Sicht wurde laengst wieder verlassen; ein ewiger
 * Ruecksprung waere ein Leck. Eine halbe Sekunde reichte nicht: Die
 * Vorgangsansicht haengt ihre Karte erst nach weiteren API-Aufrufen ein (Audit,
 * Aufgaben), und die Landkarte blieb dann doch oben links stehen.
 *
 * @param {HTMLElement} wrap die Zeichenflaeche aus renderGraph
 * @param {{x0:number,y0:number,x1:number,y1:number}|null} [focus] statt ganz
 *   einzupassen diesen Bereich ins Bild ruecken (aktive Schritte eines Vorgangs)
 */
function fitWhenVisible(wrap, focus) {
  let tries = 0;
  const attempt = () => {
    if (!wrap.isConnected || !wrap.clientWidth || !wrap.clientHeight) {
      if (++tries < FIT_WAIT_FRAMES) requestAnimationFrame(attempt);
      return;
    }
    if (!wrap._panzoom) return;
    if (focus) wrap._panzoom.centerOn(null, focus);
    else wrap._panzoom.fitToView();
  };
  requestAnimationFrame(attempt);
}

// Zeichnet die Knoten-Badges (Datenbindungen + Bearbeiter) als **vertikalen
// Stapel** UNTER dem Knoten. Jeder Chip ist auf die Knotenbreite gedeckelt und
// zentriert, sodass er nie seitlich in einen Nachbarknoten ragt; den vertikalen
// Platz reserviert layoutSchema ueber nodeBadgeStackHeight. In der Modellieren-
// Sicht sind die Chips klickbar (Sprung in die Daten-/Ressourcensicht), sonst
// rein informativ. Rein visuell -- veraendert nie Modell oder Backend.
function renderNodeBadges(root, schema, node, p, opts) {
  const chips = nodeChipModels(schema, node);
  if (!chips.length) return;
  let y = p.y + p.h + CHIP_TOP;
  chips.forEach((chip) => {
    const onOpen = chip.kind === "data" ? opts.onOpenData : opts.onOpenStaff;
    // Chipbreite auf die Knotenbreite deckeln -> ragt nie in Nachbarknoten;
    // die Beschriftung wird passend zur (ggf. gedeckelten) Breite gekuerzt.
    const rawW = Math.round(chip.label.length * CHIP_CH) + CHIP_PADX * 2;
    const w = Math.min(rawW, p.w);
    const cx = p.x + (p.w - w) / 2;
    const maxChars = Math.max(3, Math.floor((w - CHIP_PADX * 2) / CHIP_CH));
    const g = svg("g", {
      class: "gchip gchip-" + chip.kind + (onOpen ? " gchip-link" : ""),
      onClick: onOpen ? (e) => { e.stopPropagation(); onOpen(node.id); } : null,
    });
    // Wohin der Klick fuehrt, entscheidet die aufrufende Sicht (Karten-Sicht:
    // der passende Abschnitt der Schritt-Karte; klassisch: die Daten-/
    // Ressourcensicht) -- der Hinweis bleibt deshalb bewusst neutral.
    const hint = onOpen
      ? (chip.kind === "data" ? " \u2013 klicken zeigt die Datenbindungen" : " \u2013 klicken zeigt die Bearbeiterzuordnung")
      : "";
    g.appendChild(svg("title", null, document.createTextNode(chip.title + hint)));
    g.appendChild(svg("rect", { class: "gchip-bg", x: cx, y, width: w, height: CHIP_H, rx: 8 }));
    g.appendChild(svg("text", { class: "gchip-txt", x: cx + w / 2, y: y + 11, "text-anchor": "middle" },
      document.createTextNode(truncate(chip.label, maxChars))));
    root.appendChild(g);
    y += CHIP_H + CHIP_GAP;
  });
}

// Geometrie des Schnellrings am gewaehlten Knoten. Der Ring liegt UEBER dem Knoten, weil unter ihm bereits
// der Badge-Stapel haengt (siehe renderNodeBadges).
const RING_R = 11, RING_GAP = 7, RING_LIFT = 24;

/**
 * Die Aktionen des Schnellrings fuer einen Knoten -- rein deklarativ.
 *
 * Gemeinsame Quelle fuer das Zeichnen (renderNodeRing) und die
 * viewBox-Erweiterung in renderGraph, damit beide dieselbe Anzahl sehen. An
 * Start-, End- und Join-Knoten faellt der Ring ganz weg (dort gibt es nichts zu
 * tun; Joins werden ueber ihren oeffnenden Split entfernt).
 *
 * @param {object} schema Das Schema (fuer die Erkennung eines leeren XOR-Zweigs).
 * @param {object} node   Der Knoten.
 * @returns {Array<{key: string, symbol: string, title: string}>} Aktionen, ggf. leer.
 */
function nodeRingActions(schema, node) {
  if (!node) return [];
  if (node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS) {
    return [
      { key: "insert", symbol: "+", title: "Schritt danach einfügen" },
      { key: "rename", symbol: "✎", title: "Bezeichnung ändern" },
      { key: "bind", symbol: "⊕", title: "Datenelement binden" },
      { key: "delete", symbol: "✕", title: "Schritt entfernen" },
    ];
  }
  if (SPLIT_TYPES.has(node.type)) {
    const acts = [];
    if (emptyBranchJoin(schema, node.id)) {
      acts.push({ key: "empty-branch", symbol: "⌫", title: "Leeren Zweig entfernen" });
    }
    acts.push({ key: "delete", symbol: "✕", title: "Verzweigung entfernen" });
    return acts;
  }
  return [];
}

/**
 * Zeichnet den Schnellring des gewaehlten Knotens als Teil des SVG.
 *
 * Bewusst IM SVG und nicht als HTML-Overlay: Pan/Zoom verschiebt den Graphen
 * ueber eine CSS-Transformation, ein separat positioniertes Overlay muesste
 * jede Bewegung nachfuehren (oder verlöre den Bezug). Als SVG-Element wandert
 * der Ring automatisch mit seinem Knoten mit.
 *
 * Wird nur gezeichnet, wenn die aufrufende Sicht ``opts.onNodeAction`` setzt --
 * Ausfuehrung, Monitoring und Pruefinstanz nutzen dieselbe renderGraph-Funktion
 * und bleiben damit unveraendert.
 *
 * @param {SVGElement} root Wurzel-SVG.
 * @param {object} schema   Das Schema.
 * @param {object} node     Der zu bedienende Knoten.
 * @param {{x:number,y:number,w:number,h:number}} p Knotenrechteck aus layoutSchema.
 * @param {object} opts     renderGraph-Optionen (onNodeAction, selectedId).
 */
function renderNodeRing(root, schema, node, p, opts) {
  if (!opts.onNodeAction || opts.selectedId !== node.id) return;
  const acts = nodeRingActions(schema, node);
  if (!acts.length) return;
  const total = acts.length * (RING_R * 2) + (acts.length - 1) * RING_GAP;
  let cx = p.x + p.w / 2 - total / 2 + RING_R;
  const cy = p.y - RING_LIFT;
  acts.forEach((a) => {
    const g = svg("g", {
      class: "gring gring-" + a.key,
      style: "cursor:pointer",
      onClick: (e) => { e.stopPropagation(); opts.onNodeAction(node.id, a.key); },
    });
    g.appendChild(svg("title", null, document.createTextNode(a.title)));
    g.appendChild(svg("circle", { class: "gring-bg", cx, cy, r: RING_R }));
    g.appendChild(svg("text", { class: "gring-txt", x: cx, y: cy + 4, "text-anchor": "middle" },
      document.createTextNode(a.symbol)));
    root.appendChild(g);
    cx += RING_R * 2 + RING_GAP;
  });
}

/**
 * Setzt den Befund-Marker an einen Knoten, an dem der Kern etwas beanstandet.
 *
 * ``ValidationFinding`` traegt bereits ein optionales ``node_id`` -- der Client
 * gruppiert die Befunde nur (findingsByNode) und zeigt sie dort an, wo sie
 * entstehen, statt nur als Liste am Seitenrand. Er entscheidet dabei **nichts**:
 * angezeigt wird ausschliesslich, was der Kern geliefert hat.
 *
 * @param {SVGElement} root Wurzel-SVG.
 * @param {object} node     Der Knoten.
 * @param {{x:number,y:number,w:number,h:number}} p Knotenrechteck.
 * @param {object} opts     renderGraph-Optionen (findings, onFinding).
 */
function renderNodeFindingMark(root, node, p, opts) {
  const list = opts.findings ? opts.findings[node.id] : null;
  if (!list || !list.length) return;
  const cx = p.x + p.w - 9, cy = p.y + 9;
  const g = svg("g", {
    class: "gfind",
    style: opts.onFinding ? "cursor:pointer" : "",
    onClick: opts.onFinding ? (e) => { e.stopPropagation(); opts.onFinding(node.id); } : null,
  });
  g.appendChild(svg("title", null, document.createTextNode(
    list.map((f) => findingLine(f)).join("\n"))));
  g.appendChild(svg("circle", { class: "gfind-bg", cx, cy, r: 8 }));
  g.appendChild(svg("text", { class: "gfind-txt", x: cx, y: cy + 4, "text-anchor": "middle" },
    document.createTextNode(list.length > 1 ? String(list.length) : "!")));
  root.appendChild(g);
}

/**
 * Gruppiert die Befunde der letzten Validierung nach Knoten-Id.
 *
 * @returns {Object<string, Array<object>>} Knoten-Id -> Befunde (nur solche mit node_id).
 */
function findingsByNode() {
  const out = {};
  const v = state.validation;
  if (!v || !v.findings) return out;
  v.findings.forEach((f) => {
    if (!f.node_id) return;
    (out[f.node_id] = out[f.node_id] || []).push(f);
  });
  return out;
}

/** Befunde ohne Knotenbezug (modellweit, z. B. T2 kritischer Pfad). */
function globalFindings() {
  const v = state.validation;
  if (!v || !v.findings) return [];
  return v.findings.filter((f) => !f.node_id);
}

// Jump from the control flow into the data / resource view. Called from a node
// badge (``nodeId`` = that node, whose bindings are highlighted and scrolled
// into view) or from the "Vollansicht" button (``nodeId`` may be null -> no
// highlight). In both cases a **return point** is recorded so the user can jump
// straight back to where they left the control flow (see returnToControlFlow /
// returnBar). The highlight focus is mutually exclusive between the two views.
function focusBindingView(view, nodeId) {
  // Ausgangspunkt merken: aktuelle Ansicht + der Knoten, von dem aus gewechselt
  // wurde (der Badge-Knoten, sonst der aktuell gewählte Schritt).
  state.returnTo = { view: state.view, selectedNode: nodeId || state.selectedNode };
  state.dataFocusNode = view === "data" ? nodeId : null;
  state.staffFocusNode = view === "org" ? nodeId : null;
  state.view = view;
  setActiveNav();
  render();
}

// Ein-Klick-Rücksprung genau dorthin, wo der Kontrollfluss verlassen wurde:
// stellt die Ausgangsansicht wieder her, wählt den zuvor betrachteten Knoten
// erneut (die Modellieren-Sicht zentriert ihn nach dem Rendern) und räumt alle
// badge-getriebenen Hervorhebungen ab. Rein Navigations-UI.
function returnToControlFlow() {
  const r = state.returnTo;
  state.returnTo = null;
  state.dataFocusNode = null;
  state.staffFocusNode = null;
  state.orgFocusUnit = null;
  state.orgFocusAgents = [];
  state.view = r ? r.view : "model";
  if (r && r.selectedNode) state.selectedNode = r.selectedNode;
  setActiveNav();
  render();
}

// Rücksprung-Leiste: erscheint in der Daten-/Ressourcensicht, sobald ein
// Rücksprungpunkt gesetzt ist (also der Kontrollfluss per Klick verlassen
// wurde). Ein Klick führt exakt dorthin zurück. Liefert null, wenn es keinen
// Rücksprungpunkt gibt. Rein visuell -- kein Modell-/Backend-Zugriff.
function returnBar() {
  if (!state.returnTo) return null;
  const nodes = (state.schema && state.schema.nodes) || {};
  const node = state.returnTo.selectedNode && nodes[state.returnTo.selectedNode];
  const label = node
    ? "◀︎ Zurück zum Kontrollfluss – „" + nodeCaption(node) + "“"
    : "◀︎ Zurück zum Kontrollfluss";
  return el("div", { class: "return-bar" },
    el("button", { class: "btn small", onClick: returnToControlFlow, title: "Zurück zu der Stelle im Kontrollfluss, von der aus gewechselt wurde" }, label));
}

// Smoothly bring the first highlighted (.hl-row) table row of the current view
// into the centre of the viewport after a render.
function scrollHighlightIntoView() {
  requestAnimationFrame(() => {
    const row = byId("content").querySelector(".hl-row");
    if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

// Dismissible banner shown above a highlighted binding table to explain why a
// row is emphasised and let the user clear the emphasis.
function focusBanner(text, onClear) {
  return el("div", { class: "focus-banner" },
    el("span", null, text),
    el("button", { class: "btn small ghost", onClick: onClear }, "Hervorhebung l\u00F6schen"));
}

// Highlight an organisational unit (in the Abteilungen tree and the org chart)
// together with every agent that belongs to it -- including the unit's
// supervisor (manager) -- in the Agenten table. Selecting a unit is a fresh
// resource-focus intent, so it clears any staff-rule highlight. Not persisted.
function focusOrgUnit(unitId) {
  const org = (state.schema && state.schema.org_model) || { agents: {}, org_units: {} };
  const unit = (org.org_units || {})[unitId];
  if (!unit) return;
  const ids = Object.values(org.agents || {})
    .filter((a) => a.org_unit_id === unitId)
    .map((a) => a.id);
  if (unit.manager_id && (org.agents || {})[unit.manager_id] && !ids.includes(unit.manager_id)) {
    ids.push(unit.manager_id);
  }
  state.orgFocusUnit = unitId;
  state.orgFocusAgents = ids;
  state.staffFocusNode = null;
  render();
}

// Highlight a single agent (a unit's supervisor) in the Agenten table -- used
// when the supervisor badge in the Abteilungen tree is clicked.
function focusOrgAgent(agentId, unitId) {
  const org = (state.schema && state.schema.org_model) || { agents: {} };
  if (!agentId || !(org.agents || {})[agentId]) return;
  state.orgFocusUnit = unitId || null;
  state.orgFocusAgents = [agentId];
  state.staffFocusNode = null;
  render();
}

// Make a rendered graph canvas pannable (drag in any direction) and zoomable
// (mouse wheel, anchored to the pointer position). Pan/zoom is purely visual
// (a CSS transform on the SVG) and resets on the next render -- it never
// touches the model or any backend state. The controller is exposed on
// ``wrap._panzoom`` so ``centerCanvasOnNode`` can re-centre the selected node.
function attachPanZoom(wrap, svgEl) {
  const MIN = 0.2, MAX = 4;
  let scale = 1, tx = 0, ty = 0;
  // Zweistufiges Einpassen (siehe fitToView): true, solange die lesbare Stufe
  // gezeigt wird und der naechste Klick die volle Uebersicht bringen soll. Jede
  // eigene Bewegung (Rad, Ziehen) setzt zurueck.
  const fitState = { readableShown: false };
  // Breite eines am rechten Rand liegenden Overlays (Modellieren-Sicht: die
  // Schritt-Karte), die beim Einpassen und Zentrieren frei bleiben muss.
  // Ohne diese Reserve zentriert die Canvas den gewaehlten Knoten exakt unter
  // die Karte -- man bearbeitet dann einen Schritt, den man nicht sieht.
  let reserveRight = 0;

  /** Mindestmassstab, den der erste Einpassen-Klick nicht unterschreitet. */
  const FIT_READABLE = 0.6;
  /**
   * Mindestmassstab der Uebersicht (zweiter Klick). Frueher bis MIN (0,2) --
   * ein Modell mit 16 Knoten war dann nicht mehr lesbar. Was bei
   * diesem Massstab nicht passt, bleibt seitlich verschiebbar; die Randpfeile
   * zeigen, dass dort noch etwas liegt.
   */
  const FIT_OVERVIEW_MIN = 0.35;

  // Randpfeile: „hier geht es weiter“. Ohne sie wirkte das lesbare Einpassen
  // wie ein abgeschnittenes Modell.
  const moreLeft = el("div", { class: "canvas-more canvas-more-left", "aria-hidden": "true" }, "\u25C0");
  const moreRight = el("div", { class: "canvas-more canvas-more-right", "aria-hidden": "true" }, "\u25B6 weiter rechts");
  wrap.appendChild(moreLeft);
  wrap.appendChild(moreRight);

  /**
   * Der tatsaechlich sichtbare Streifen der Canvas.
   *
   * Die Canvas ist ``clamp(420px, 66vh, 900px)`` hoch; auf Laptop-Hoehe (etwa
   * 1054 x 676) ragt sie unter den Fensterrand. Einpassen zentrierte deshalb
   * ueber eine Hoehe, von der ein Teil unsichtbar war, und die Randpfeile
   * sassen unten ausserhalb des Fensters. Ist der sichtbare Streifen zu schmal
   * (Canvas weit weggescrollt), gilt die volle Hoehe wie bisher.
   * @returns {{top: number, h: number}} Oberkante (relativ zur Canvas) und Hoehe
   */
  function visibleBand() {
    const full = { top: 0, h: wrap.clientHeight };
    if (typeof wrap.getBoundingClientRect !== "function" || typeof window === "undefined") return full;
    const r = wrap.getBoundingClientRect();
    // Sichtbar ist, was Fenster UND jeder scrollende Vorfahre (``.main``) zeigen;
    // die App macht der Demo-Leiste unten Platz, ``.main`` endet also darueber.
    let clipTop = 0, clipBottom = window.innerHeight || 0;
    for (let a = wrap.parentElement; a && a !== document.body; a = a.parentElement) {
      const oy = getComputedStyle(a).overflowY;
      if (oy === "visible") continue;
      const ar = a.getBoundingClientRect();
      clipTop = Math.max(clipTop, ar.top);
      clipBottom = Math.min(clipBottom, ar.bottom);
    }
    const top = Math.max(0, clipTop - r.top);
    const bottom = Math.min(wrap.clientHeight, clipBottom - r.top);
    const h = bottom - top;
    return h >= 160 ? { top, h } : full;
  }

  /**
   * Senkrechte Mitte des Start-Knotens in Modellkoordinaten, falls vorhanden.
   * Die lesbare Einpassen-Stufe legt die Hauptlinie ab Start in die Mitte des
   * sichtbaren Streifens: Bei einem hohen Modell (Zweige ober- und unterhalb)
   * zeigte die obere Ausrichtung sonst zuerst leere Flaeche.
   * @returns {number|null}
   */
  function startCenterY() {
    const node = svgEl.querySelector && svgEl.querySelector('[data-node-id="start"]');
    if (!node || typeof node.getBBox !== "function") return null;
    try { const b = node.getBBox(); return b.height ? b.y + b.height / 2 : null; } catch (_e) { return null; }
  }

  function apply() {
    svgEl.style.transformOrigin = "0 0";
    svgEl.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
    const vw = wrap.clientWidth;
    if (vb && vb.width && vw) {
      const left = tx + vb.x * scale;
      const right = tx + (vb.x + vb.width) * scale;
      moreLeft.style.display = left < -4 ? "" : "none";
      moreRight.style.display = right > vw - reserveRight + 4 ? "" : "none";
      moreRight.style.right = `${10 + reserveRight}px`;  // nicht unter der Schritt-Karte
      // Pfeile am unteren Rand des *sichtbaren* Streifens, nicht der Canvas.
      const band = visibleBand();
      const lift = Math.max(0, wrap.clientHeight - (band.top + band.h));
      moreLeft.style.bottom = moreRight.style.bottom = `${8 + lift}px`;
    }
  }
  apply();

  // Wheel handling:
  //  * Pinch-to-zoom (trackpad) and Ctrl+wheel (mouse) arrive with ctrlKey and
  //    zoom towards / away from the pointer (model point under the cursor stays
  //    fixed while the scale changes).
  //  * A plain two-finger trackpad swipe (or mouse wheel) pans the canvas -- in
  //    both directions, so sideways scrolling works. Shift+wheel maps a
  //    vertical mouse wheel to horizontal panning.
  wrap.addEventListener("wheel", (e) => {
    fitState.readableShown = false;  // eigene Bewegung: Einpassen beginnt wieder lesbar
    e.preventDefault();
    if (e.ctrlKey) {
      const rect = wrap.getBoundingClientRect();
      const px = e.clientX - rect.left, py = e.clientY - rect.top;
      const cx = (px - tx) / scale, cy = (py - ty) / scale;
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      const next = Math.min(MAX, Math.max(MIN, scale * factor));
      if (next === scale) return;
      scale = next;
      tx = px - cx * scale;
      ty = py - cy * scale;
      apply();
      return;
    }
    let dx = e.deltaX, dy = e.deltaY;
    if (e.shiftKey && dx === 0) { dx = dy; dy = 0; }
    tx -= dx; ty -= dy;
    apply();
  }, { passive: false });

  // Drag = pan. Only capture the pointer once movement passes a small
  // threshold so a plain click still selects a node / hits a "+" handle.
  let down = false, dragging = false, sx = 0, sy = 0, lastX = 0, lastY = 0;
  wrap.addEventListener("pointerdown", (e) => {
    // Eigene Bewegung: Einpassen beginnt wieder lesbar -- ausser beim Klick auf
    // den Einpassen-Knopf selbst, der im Canvas liegt (sonst kaeme die zweite
    // Stufe nie).
    if (!(e.target && e.target.closest && e.target.closest(".canvas-fit"))) fitState.readableShown = false;
    if (e.button !== 0) return;
    down = true; dragging = false;
    sx = lastX = e.clientX; sy = lastY = e.clientY;
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!down) return;
    if (!dragging && Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) < 4) return;
    if (!dragging) {
      dragging = true;
      wrap.classList.add("grabbing");
      // Textselektion seitenweit unterdruecken, solange gezogen wird: sonst
      // markiert der Zeiger beim Verlassen des Canvas-Fensters die darunter
      // liegenden Seiteninhalte. Eine bereits (in den ersten Pixeln) begonnene
      // Selektion wird zusaetzlich geleert.
      document.documentElement.classList.add("graph-dragging");
      const sel = window.getSelection && window.getSelection();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
      try { wrap.setPointerCapture(e.pointerId); } catch (_e) { /* ignore */ }
    }
    tx += e.clientX - lastX; ty += e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    apply();
  });
  function endDrag(e) {
    if (!down) return;
    down = false;
    document.documentElement.classList.remove("graph-dragging");
    if (dragging) {
      wrap.classList.remove("grabbing");
      try { wrap.releasePointerCapture(e.pointerId); } catch (_e) { /* ignore */ }
    }
  }
  wrap.addEventListener("pointerup", endDrag);
  wrap.addEventListener("pointercancel", endDrag);
  // Swallow the click that trails a real drag so panning never selects a node.
  wrap.addEventListener("click", (e) => {
    if (dragging) { e.stopPropagation(); e.preventDefault(); }
    dragging = false;
  }, true);

  // Doppelklick mit der **mittleren** Maustaste rueckt das ganze Modell wieder
  // ins Bild -- die Tastatur-lose Notbremse, wenn man sich beim Verschieben
  // verloren hat. Zwei Dinge sind hier wichtig:
  //  * Der Browser (Windows/Linux) startet auf `mousedown` mit der mittleren
  //    Taste den Autoscroll-Modus; ohne `preventDefault` klebt danach ein
  //    Scroll-Anker am Zeiger.
  //  * Ein echter `dblclick` feuert nur fuer die linke Taste, deshalb zaehlen
  //    wir die mittleren Klicks selbst (Doppelklick-Fenster wie im System
  //    ueblich ~400 ms; ein zu langsamer zweiter Klick zaehlt als neuer
  //    erster, nicht als halber Doppelklick).
  const MIDDLE_DBL_MS = 400;
  let midClicks = 0, midAt = 0;
  wrap.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });
  wrap.addEventListener("auxclick", (e) => {
    if (e.button !== 1) return;
    e.preventDefault();
    const now = Date.now();
    midClicks = now - midAt <= MIDDLE_DBL_MS ? midClicks + 1 : 1;
    midAt = now;
    if (midClicks >= 2) { midClicks = 0; fitToView(); }
  });

  // Setzt Zoom und Verschiebung so, dass das **gesamte** Modell sichtbar und
  // zentriert im Fenster liegt. Bezugsrahmen ist die viewBox des SVG (sie kann
  // fuer die Datenherkunft-Boegen nach oben erweitert sein, also einen
  // negativen Ursprung haben) plus ein kleiner Rand. Kleine Modelle werden
  // **nicht** ueber ihre natuerliche Groesse hinaus vergroessert (Deckel 1),
  // sonst wirkt ein Zwei-Knoten-Prozess nach dem Einpassen aufgeblasen.
  function fitToView() {
    const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
    const bx = vb && vb.width ? vb.x : 0;
    const by = vb && vb.width ? vb.y : 0;
    const bw = vb && vb.width ? vb.width : svgEl.clientWidth;
    const bh = vb && vb.height ? vb.height : svgEl.clientHeight;
    const band = visibleBand();
    const vw = wrap.clientWidth, vh = band.h;
    if (!bw || !bh || !vw || !vh) return;
    const MARGIN = 16;
    const vwFree = Math.max(120, vw - reserveRight);
    const fit = Math.min((vwFree - MARGIN * 2) / bw, (vh - MARGIN * 2) / bh);
    // Lesbar statt winzig: Passt das Modell nur unterhalb von FIT_READABLE ins
    // Bild, haelt der erste Klick diese Mindestgroesse und zeigt den Ablauf ab
    // dem Start (links); erst ein zweiter Klick direkt danach zeigt die volle
    // Uebersicht. Vorher war ein Modell mit elf Schritten nach dem Einpassen
    // kaum lesbar. Kleine Modelle verhalten sich unveraendert.
    const overview = fitState.readableShown;
    if (fit < FIT_READABLE && !overview) {
      scale = FIT_READABLE;
      tx = MARGIN - bx * scale;
      const h = bh * scale;
      if (h <= vh - MARGIN * 2) {
        ty = band.top + (vh - h) / 2 - by * scale;
      } else {
        // Hauptlinie ab Start mittig, aber keine Leerflaeche ueber der Ober-
        // bzw. unter der Unterkante des Modells.
        const cy = startCenterY();
        const topAligned = band.top + MARGIN - by * scale;
        const bottomAligned = band.top + vh - MARGIN - (by + bh) * scale;
        ty = cy == null ? topAligned
          : Math.min(topAligned, Math.max(bottomAligned, band.top + vh / 2 - cy * scale));
      }
      fitState.readableShown = true;
    } else {
      scale = Math.min(MAX, Math.max(Math.max(MIN, FIT_OVERVIEW_MIN), Math.min(1, fit)));
      const w = bw * scale;
      // Passt es auch so nicht, beginnt die Ansicht links am Start statt
      // mittig abgeschnitten zu sein; der Rest ist per Ziehen erreichbar.
      tx = w <= vwFree - MARGIN * 2 ? (vwFree - w) / 2 - bx * scale : MARGIN - bx * scale;
      const h = bh * scale;
      ty = band.top + (h <= vh - MARGIN * 2 ? (vh - h) / 2 - by * scale : MARGIN - by * scale);
      fitState.readableShown = false;
    }
    apply();
  }

  wrap._panzoom = {
    fitToView,
    /**
     * Meldet die Breite eines rechts liegenden Overlays (Schritt-Karte), die
     * beim Einpassen/Zentrieren frei bleiben soll. ``0`` hebt die Reserve auf
     * (z. B. mobile Bodensheet-Darstellung, die den Canvas nicht seitlich
     * verdeckt).
     *
     * @param {number} px Reservierte Breite in Bildschirmpixeln.
     */
    setReserve(px) { reserveRight = Math.max(0, px || 0); },
    /**
     * Rueckt den gewaehlten Knoten -- und optional einen zusaetzlichen Bereich,
     * der zu ihm gehoert -- ins Bild.
     *
     * @param {{x:number,y:number,w:number,h:number}|null} pos Knotenrechteck in
     *        Modellkoordinaten (darf fehlen, wenn nur ``region`` interessiert).
     * @param {{x0:number,y0:number,x1:number,y1:number}|null} region Zusaetzlich
     *        sichtbar zu haltender Bereich -- in der Modellieren-Sicht die
     *        gestrichelte Datenherkunft samt ihrer **Quellknoten**.
     *
     * Ohne ``region`` bleibt es beim reinen Zentrieren im aktuellen Massstab.
     * Mit ``region`` wird der Massstab bei Bedarf **verkleinert** (nie
     * vergroessert, nie ueber 1), bis der ganze Bereich hineinpasst: Der
     * Schreiber eines gelesenen Datenelements liegt meist mehrere hundert Pixel
     * weiter links, ein Zentrieren allein auf den Leseknoten schob ihn samt
     * Bogen und Beschriftung aus dem (overflow:hidden) Canvas -- die Herkunft
     * war gezeichnet, aber unsichtbar.
     */
    centerOn(pos, region) {
      // Die viewBox kann fuer die Datenherkunft-Boegen nach oben erweitert
      // sein (negativer viewBox-Ursprung); der Knoten liegt dann um |vbY|
      // tiefer im Pixelraum. Diesen Versatz beim Zentrieren kompensieren,
      // sonst springt der gewaehlte Knoten aus der Mitte.
      const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
      const offX = vb ? vb.x : 0, offY = vb ? vb.y : 0;
      const vw = wrap.clientWidth, vh = wrap.clientHeight;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      if (pos) {
        x0 = pos.x; y0 = pos.y; x1 = pos.x + pos.w; y1 = pos.y + pos.h;
      }
      if (region) {
        x0 = Math.min(x0, region.x0); y0 = Math.min(y0, region.y0);
        x1 = Math.max(x1, region.x1); y1 = Math.max(y1, region.y1);
      }
      if (!(x1 > x0) || !(y1 > y0)) return;
      const vwFree = Math.max(120, vw - reserveRight);
      if (region && vw > 0 && vh > 0) {
        const MARGIN = 24;
        const fit = Math.min((vwFree - MARGIN * 2) / (x1 - x0), (vh - MARGIN * 2) / (y1 - y0));
        scale = Math.min(scale, Math.max(MIN, Math.min(1, fit)));
      }
      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
      tx = vwFree / 2 - (cx - offX) * scale;
      ty = vh / 2 - (cy - offY) * scale;
      apply();
    },
  };
}

/**
 * Anzeigetext einer Kantenbedingung.
 *
 * Der Kern leitet die Bedingung als technischen Text ab („Entscheidung:
 * otherwise“, „Freigabe == true“); der BPMN-Import liest genau diesen Text
 * zurueck, er bleibt deshalb unveraendert gespeichert. Nur die Anzeige wird
 * deutsch.
 * @param {string} text Bedingung wie gespeichert
 * @returns {string}
 */
function conditionCaption(text) {
  return String(text)
    .replace(/:\s*otherwise$/, ": sonst")
    .replace(/ == true\b/g, " = Ja")
    .replace(/ == false\b/g, " = Nein")
    .replace(/ or /g, " oder ");
}

function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "\u2026" : s; }
