// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Pruefinstanz.
 *
 * Die Pruefinstanz: Analyse-Cockpit fuer einen Entwurf in vier Quadranten.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// View: Prüfinstanz (4-Quadranten-Analyse-Cockpit für einen Entwurf)
// --------------------------------------------------------------------------
//
// Der Modellierer startet aus der Modellieransicht eine Prüfinstanz -- eine
// Test-Instanz eines noch nicht freigegebenen Entwurfs -- und spielt sie hier
// durch, um das Modellkonzept zu erarbeiten. Das Fenster ist in vier
// Quadranten geteilt:
//   oben links   Monitoring, beschränkt auf DIESE eine Instanz (Prozesskarte,
//                Fortschritt, Instanzdaten, Audit-Verlauf);
//   oben rechts  der angemeldete Starter (wer die Instanz gestartet hat);
//   unten        zwei frei wählbare, an der Instanz beteiligte Agenten, jeweils
//                mit ihrer auf diese Instanz gefilterten Arbeitsliste.
// Wie der ganze Client trägt die Sicht KEINE Korrektheitslogik: sie ruft nur
// geprüfte Endpunkte. Die instanzgefilterte Arbeitsliste entsteht rein
// clientseitig aus GET /instances/{id}/tasks (OpenTask.eligible_agents).

async function viewTestRun() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;

  if (!state.testInstanceId) {
    renderTestStartCard(content, schema);
    content.appendChild(simulationPanel(schema));
    return;
  }

  let inst;
  try { inst = await loadTestInstance(state.testInstanceId); }
  catch (err) {
    // Instanz verschwunden (z. B. Neustart des in-memory-Kerns) -> Startkarte.
    state.testInstanceId = null; state.testInstance = null; persistTestState();
    renderTestStartCard(content, schema,
      "Die zuletzt genutzte Pr\u00FCfinstanz ist nicht mehr verf\u00FCgbar (z. B. nach einem Neustart des Kerns). Bitte neu starten.");
    return;
  }

  const runSchema = inst.ad_hoc_schema || schema;
  const org = runSchema.org_model || { agents: {} };
  const agents = Object.values(org.agents || {}).sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  ensureTestAgentDefaults(agents);

  // Offene Aufgaben der Instanz EINMAL laden -> je Agent gefiltert dargestellt.
  let instanceTasks = [];
  try { instanceTasks = await api.get(`/instances/${inst.id}/tasks`); } catch (e) { /* ignore */ }

  content.appendChild(testHeader(schema, inst));
  content.appendChild(el("div", { class: "quad-grid" },
    el("div", { class: "quad-cell" }, testMonitorPanel(inst, runSchema)),
    el("div", { class: "quad-cell" }, testStarterPanel(runSchema)),
    el("div", { class: "quad-cell" }, testAgentPanel("A", inst, runSchema, agents, instanceTasks)),
    el("div", { class: "quad-cell" }, testAgentPanel("B", inst, runSchema, agents, instanceTasks))));
  content.appendChild(simulationPanel(runSchema));
}

// --- Simulation (E6): seiteneffektfreier Was-wäre-wenn-Durchlauf -----------
// Ergänzt die interaktive Prüfinstanz um die schnelle Vorab-Frage „Welchen
// Weg nimmt der Prozess mit diesen Werten?“. Jeder Lauf ist ein frischer
// Aufruf von POST /schemas/{id}/simulate – der Kern spielt rein und ohne
// jede Spur durch (kein Store, kein Audit, keine Mails); der Client zeigt
// das Ergebnis mit demselben renderGraph wie jede Laufzeit-Sicht.
//
// Eingaben und letztes Ergebnis liegen in ``state.simulation`` (je Schema),
// nicht nur im DOM: Die Pruefinstanz-Sicht zeichnet sich bei jedem Fortschritt
// und alle 30 s neu, und das Ergebnis verschwand sonst ungefragt.
/**
 * @param {object} schema das simulierte Schema (Entwurf oder Ad-hoc-Variante)
 * @returns {HTMLElement} das Panel
 */
function simulationPanel(schema) {
  const key = schema.id || state.schemaId;
  // Fingerabdruck des Modellstands: Aendert sich der Entwurf (Zweig geloescht,
  // Schritt eingefuegt), passt ein altes Ergebnis nicht mehr zum Graphen --
  // es wird verworfen; die Eingaben bleiben als Vorschlag stehen.
  const fp = JSON.stringify([schema.nodes, schema.edges, schema.data_elements,
    schema.data_accesses, schema.version]);
  if (!state.simulation || state.simulation.key !== key) {
    state.simulation = { key, fp, values: {}, result: null, runId: 0 };
  } else if (state.simulation.fp !== fp) {
    state.simulation.fp = fp;
    state.simulation.result = null;
    state.simulation.runId = (state.simulation.runId || 0) + 1;  // laufende Antwort ist veraltet
  }
  const sim0 = state.simulation;
  const elems = Object.values(schema.data_elements || {})
    .filter((d) => d.source === "INSTANCE")
    .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  const inputs = {};
  const fields = elems.map((d) => {
    let input;
    if (d.data_type === "BOOLEAN") {
      input = el("select", null,
        el("option", { value: "" }, "– nicht gesetzt –"),
        el("option", { value: "true" }, "wahr"),
        el("option", { value: "false" }, "falsch"));
    } else if (isNumericType(d.data_type)) {
      input = el("input", { type: "number", placeholder: "– nicht gesetzt –" });
    } else {
      input = el("input", { type: "text", placeholder: "– nicht gesetzt –" });
    }
    inputs[d.id] = { input, type: d.data_type };
    // Wert aus state wiederherstellen und dort mitfuehren; data-live-safe:
    // diese Eingabe haelt die Live-Aktualisierung nicht auf (sie uebersteht sie).
    input.setAttribute("data-live-safe", "1");
    if (sim0.values[d.id] != null) input.value = sim0.values[d.id];
    const keep = () => { sim0.values[d.id] = input.value; };
    input.addEventListener("input", keep);
    input.addEventListener("change", keep);
    return el("label", { class: "field" }, `${d.name} (${typeName(d.data_type)})`, input);
  });
  // data-sim-result: Kommt die Antwort erst, nachdem die Sicht neu gezeichnet
  // wurde, landet sie im dann sichtbaren Panel statt im verworfenen.
  const result = el("div", { "data-sim-result": "1" });
  if (sim0.result) result.appendChild(renderSimulationResult(schema, sim0.result));
  async function run() {
    const data = {};
    Object.entries(inputs).forEach(([id, { input, type }]) => {
      const v = (input.value || "").trim();
      if (v === "") return;  // leer = nicht gesetzt (bewusst)
      if (type === "BOOLEAN") data[id] = v === "true";
      else data[id] = coerceTypedInput(type, v);  // Zahlen inkl. Betrag
    });
    clear(result);
    sim0.result = null;
    const runId = (sim0.runId || 0) + 1;
    sim0.runId = runId;
    try {
      const sim = await api.post(`/schemas/${state.schemaId}/simulate`, { data });
      if (sim0.runId !== runId || state.simulation !== sim0) return;  // ueberholt
      sim0.result = sim;
      const target = result.isConnected ? result : document.querySelector("[data-sim-result]");
      if (target) { clear(target); target.appendChild(renderSimulationResult(schema, sim)); }
    } catch (err) { toastError(err); }
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Simulation"),
      el("span", { class: "sub" }, "Was-wäre-wenn: Weg und Dauer mit diesen Werten – ohne Seiteneffekte")),
    el("div", { class: "panel-b" },
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Die Simulation spielt den Prozess mit den angegebenen Werten automatisch durch – gewählte Zweige, Schleifenrunden, erwartete Dauer. Es wird nichts gespeichert; fehlt ein entscheidender Wert, bricht der Lauf mit Begründung ab."),
      fields.length
        ? el("div", { class: "form-grid" }, ...fields)
        : el("div", { class: "muted", style: "font-size:13px" }, "Keine Instanz-Datenelemente – die Simulation läuft ohne Eingaben."),
      el("div", { class: "row", style: "margin-top:8px" },
        el("button", { class: "btn small primary", onClick: run }, "Simulieren")),
      result));
}

function renderSimulationResult(schema, sim) {
  const box = el("div", { style: "margin-top:12px" });
  const badge = sim.completed
    ? el("span", { class: "pill pill-green" }, "Ende erreicht")
    : el("span", { class: "pill pill-red" }, "nicht bis zum Ende");
  const dur = sim.expected_duration_seconds != null
    ? formatDuration(sim.expected_duration_seconds) : "–";
  box.appendChild(el("div", { class: "row", style: "gap:8px;align-items:center;margin-bottom:8px" },
    badge,
    el("span", { class: "muted", style: "font-size:12px" }, `erwartete Dauer: ${dur}`),
    el("span", { class: "muted", style: "font-size:12px" }, `${countLabel((sim.executed || []).length, "Schritt", "Schritte")} ausgeführt`)));
  (sim.findings || []).forEach((f) => box.appendChild(el("div", { class: "warn-banner" }, f)));
  // Markierungsbild: dieselbe Kontrollfluss-Darstellung wie jede Laufzeit-
  // Sicht, gespeist aus der synthetischen Instanz des Simulationsergebnisses
  // (node_states + loop_iterations reichen renderGraph als „Instanz“).
  const graphWrap = renderGraph(schema, { instance: sim });
  // Abspiel-Animation (Stufe B): die Abschlussreihenfolge
  // ``executed`` Schritt für Schritt über dem Markierungsbild nachspielen.
  if ((sim.executed || []).length) {
    box.appendChild(simulationPlaybackControls(graphWrap, schema, sim.executed));
  }
  box.appendChild(graphWrap);
  const dec = Object.entries(sim.decisions || {});
  if (dec.length) {
    box.appendChild(el("div", { class: "sub-h", style: "margin-top:8px" }, el("h3", null, "Gewählte Zweige")));
    box.appendChild(table(["Verzweigung", "Zweig"], dec.map(([split, target]) => {
      const s = schema.nodes[split];
      return [s ? nodeCaptionInContext(schema, s) : split, branchCaption(schema, split, target)];
    })));
  }
  return box;
}

// Abspiel-Animation der Simulation (Stufe B): spielt die
// Abschlussreihenfolge ``executed`` über dem fertigen Markierungsbild nach –
// rein visuell im Client (CSS-Klassen auf den ``data-node-id``-Gruppen des
// SVG), kein weiterer Kern-Aufruf, keine erfundenen Zwischen-Markierungen:
// Zu Beginn werden alle Knoten gedimmt, dann leuchtet Schritt für Schritt der
// jeweils abgeschlossene auf und bleibt sichtbar; am Ende ist wieder exakt
// das ungefilterte Endbild zu sehen. Der Takt bricht selbstständig ab, wenn
// das SVG den DOM verlässt (Panel neu gerendert) – kein Timer-Leck.
const SIM_PLAY_TICK_MS = 700;

function simulationPlaybackControls(graphWrap, schema, executed) {
  const readout = el("span", { class: "muted", style: "font-size:12px" }, "");
  let timer = null;
  let btn = null;

  function groupOf(nodeId) {
    return graphWrap.querySelector(`g[data-node-id="${nodeId}"]`);
  }
  function allGroups() {
    return Array.from(graphWrap.querySelectorAll("g[data-node-id]"));
  }
  function stop(finished) {
    if (timer) { clearInterval(timer); timer = null; }
    allGroups().forEach((g) => g.classList.remove("sim-dim", "sim-current"));
    readout.textContent = finished ? `abgespielt – ${executed.length} Schritte` : "";
    btn.textContent = "▶ Abspielen";
  }
  function play() {
    if (timer) { stop(false); return; }  // der Knopf ist zugleich „Stopp"
    let i = -1;
    allGroups().forEach((g) => { g.classList.add("sim-dim"); g.classList.remove("sim-current"); });
    btn.textContent = "■ Stopp";
    const step = () => {
      if (!graphWrap.isConnected) { clearInterval(timer); timer = null; return; }
      if (i >= 0) {
        const prev = groupOf(executed[i]);
        if (prev) prev.classList.remove("sim-current");
      }
      i += 1;
      if (i >= executed.length) { stop(true); return; }
      const g = groupOf(executed[i]);
      if (g) {
        g.classList.remove("sim-dim");
        g.classList.add("sim-current");
      }
      const node = schema.nodes[executed[i]];
      readout.textContent = `Schritt ${i + 1}/${executed.length}: ${node ? nodeCaption(node) : executed[i]}`;
    };
    timer = setInterval(step, SIM_PLAY_TICK_MS);
    step();  // sofort starten, nicht erst nach dem ersten Takt
  }
  btn = el("button", { class: "btn small", onClick: play }, "▶ Abspielen");
  return el("div", { class: "row", style: "gap:8px;align-items:center;margin:4px 0" }, btn, readout);
}

// Startkarte, solange keine (gültige) Prüfinstanz geladen ist.
function renderTestStartCard(content, schema, note) {
  const draft = isDraft(schema);
  const canStart = draft && hasRole("modeler", "admin");
  const body = el("div", { class: "panel-b" },
    note ? el("div", { class: "muted", style: "margin-bottom:10px" }, note) : null,
    el("p", { class: "muted" },
      "Eine Pr\u00FCfinstanz ist eine Test-Instanz dieses Entwurfs. Sie k\u00F6nnen den Prozess hier durchspielen "
      + "und so das Modellkonzept erarbeiten \u2013 beim Start wird gefragt, wer die Instanz startet."),
    canStart
      ? el("div", { style: "margin-top:12px" },
        el("button", { class: "btn primary", onClick: startTestInstance }, "\u2697 Pr\u00FCfinstanz starten"))
      : el("div", { class: "muted" }, draft
        ? "Nur Modellierer/Administratoren k\u00F6nnen eine Pr\u00FCfinstanz starten."
        : "Das Schema ist bereits freigegeben. Pr\u00FCfinstanzen dienen der Analyse von Entw\u00FCrfen \u2013 "
          + "nutzen Sie f\u00FCr freigegebene Schemata die Ausf\u00FChrungs-/Monitoring-Sicht."));
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Pr\u00FCfinstanz"),
      el("span", { class: "sub" }, schema.name + " v" + schema.version), lifecyclePill(schema)),
    body));
}

// Prüfinstanz starten: fragt, wer sie startet, legt sie an und öffnet das Cockpit.
async function startTestInstance() {
  const schema = state.schema;
  if (!schema) return;
  if (!isDraft(schema)) {
    toast("err", "Nicht m\u00F6glich", ["Pr\u00FCfinstanzen sind nur f\u00FCr Entw\u00FCrfe (nicht freigegeben) vorgesehen."]);
    return;
  }
  const org = schema.org_model || { agents: {} };
  const agents = Object.values(org.agents || {}).sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  if (!agents.length) {
    toast("err", "Keine Agenten", ["Legen Sie zuerst Agenten in der Ressourcensicht an."]);
    return;
  }
  const sel = el("select", null, ...agents.map((a) => el("option", { value: a.id }, a.name)));
  const body = el("div", { class: "form-grid" },
    el("p", { class: "muted", style: "margin:0 0 4px" },
      "W\u00E4hlen Sie, wer die Pr\u00FCfinstanz startet. Diese Person erscheint oben rechts als angemeldeter Starter."),
    el("label", { class: "field" }, "Startende Person", sel));
  openModal("Pr\u00FCfinstanz starten", body, async () => {
    try {
      const inst = await api.post(`/schemas/${state.schemaId}/instances`);
      state.testInstanceId = inst.id;
      state.testInstance = inst;
      state.testStarter = sel.value;
      state.testAgentA = null;
      state.testAgentB = null;
      persistTestState();
      state.view = "testrun";
      setActiveNav();
      render();
      toast("ok", "Pr\u00FCfinstanz gestartet", [`${instanceCaption(inst, schema)} \u00B7 ${inst.id}`]);
      return true;
    } catch (err) { toastError(err); return false; }
  }, "Starten");
}

async function loadTestInstance(id) {
  state.testInstanceId = id;
  state.testInstance = await api.get(`/instances/${id}`);
  return state.testInstance;
}

// Ungültige Agenten-Auswahl bereinigen und sinnvoll vorbelegen (erste zwei).
function ensureTestAgentDefaults(agents) {
  const ids = agents.map((a) => a.id);
  if (state.testAgentA && !ids.includes(state.testAgentA)) state.testAgentA = null;
  if (state.testAgentB && !ids.includes(state.testAgentB)) state.testAgentB = null;
  if (!state.testAgentA && ids.length) state.testAgentA = ids[0];
  if (!state.testAgentB && ids.length > 1) state.testAgentB = ids.find((id) => id !== state.testAgentA) || null;
  persistTestState();
}

function persistTestState() {
  const set = (k, v) => { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); };
  set("testInstanceId", state.testInstanceId);
  set("testStarter", state.testStarter);
  set("testAgentA", state.testAgentA);
  set("testAgentB", state.testAgentB);
}

function testHeader(schema, inst) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, "Pr\u00FCfinstanz"),
      el("span", { class: "sub" }, schema.name + " v" + schema.version),
      lifecyclePill(schema),
      el("span", { class: "pill pill-amber", title: "Test-Instanz eines Entwurfs \u2013 nicht Teil des echten Betriebs" }, "TEST"),
      statePillFor(inst.state),
      el("span", { class: "spacer", style: "flex:1" }),
      hasRole("modeler", "admin")
        ? el("button", { class: "btn small primary", onClick: startTestInstance }, "\u2697 Neue Pr\u00FCfinstanz")
        : null,
      el("button", { class: "btn small ghost", onClick: closeTestInstance }, "Analyse schlie\u00DFen")));
}

function closeTestInstance() {
  state.testInstanceId = null;
  state.testInstance = null;
  persistTestState();
  render();
}

// Oben links: Monitoring, beschränkt auf diese eine Instanz.
//
// Test-Instanzen erzeugen bewusst KEINE Audit-Events (sie sollen das globale
// Monitoring/die KPIs nie verf\u00E4lschen), daher speist sich diese Sicht rein aus
// dem Instanz-Objekt: node_states (Schrittfortschritt), performed_by (Bearbeiter)
// und data_values -- unabh\u00E4ngig vom Audit-Log.
function testMonitorPanel(inst, runSchema) {
  const steps = Object.values(runSchema.nodes)
    .filter((n) => n.type === "ACTIVITY" || n.type === "SUBPROCESS");
  const stateOf = (id) => (inst.node_states || {})[id] || "NOT_ACTIVATED";
  const done = steps.filter((n) => { const s = stateOf(n.id); return s === "COMPLETED" || s === "SKIPPED"; }).length;
  const total = steps.length || 1;
  const pct = Math.round((done / total) * 100);

  const kpis = el("div", { class: "kpis kpis-compact" },
    kpi("Status", INSTANCE_STATE_LABELS[inst.state] || inst.state),
    kpi("Fortschritt", pct + "%"),
    kpi("Schritte", done + "/" + steps.length),
    kpi("Datenwerte", Object.keys(inst.data_values || {}).length));

  const dataRows = Object.entries(inst.data_values || {}).map(([k, v]) => {
    const elem = runSchema.data_elements[k];
    return [elem ? elem.name : k, formatValue(elem, v)];
  });
  const dataBlock = el("div", { class: "panel-b" },
    el("div", { class: "sub-h" }, el("h3", null, "Instanzdaten")),
    dataRows.length ? table(["Element", "Wert"], dataRows) : emptyState("Noch keine Werte."));

  // Schrittübersicht aus den Knotenmarkierungen (audit-unabhängig).
  const stepBlock = el("div", { class: "panel-b" }, el("div", { class: "sub-h" }, el("h3", null, "Schritte")));
  if (!steps.length) {
    stepBlock.appendChild(emptyState("Keine Aktivit\u00E4ten im Modell."));
  } else {
    const rows = steps.map((n) => {
      const s = stateOf(n.id);
      const who = (inst.performed_by || {})[n.id];
      return [nodeCaption(n), nodeStatePill(s), who ? agentNameOfIn(runSchema, who) : "\u2013"];
    });
    stepBlock.appendChild(table(["Schritt", "Status", "Bearbeiter"], rows));
  }

  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Monitoring"),
      el("span", { class: "sub", title: `Kennung: ${inst.id}` }, instanceCaption(inst, runSchema)),
      inst.state === "COMPLETED"
        ? el("span", { class: "pill pill-green" }, "fertig")
        : el("span", { class: "pill pill-blue" }, "l\u00E4uft")),
    el("div", { class: "panel-b" }, kpis),
    el("div", { class: "panel-b" }, el("div", { class: "sub-h" }, el("h3", null, "Live-Prozesslandkarte")), renderGraph(runSchema, { instance: inst })),
    dataBlock, stepBlock);
}

// Farbige Statusmarke für eine Knotenmarkierung (NodeState) im Schritt-Panel.
const NODE_STATE_META = {
  NOT_ACTIVATED: { label: "wartet", cls: "" },
  ACTIVATED: { label: "bereit", cls: "pill-blue" },
  RUNNING: { label: "l\u00E4uft", cls: "pill-blue" },
  COMPLETED: { label: "erledigt", cls: "pill-green" },
  SKIPPED: { label: "\u00FCbersprungen", cls: "pill-amber" },
};
/**
 * Deutsche Bezeichnung einer Knotenmarkierung (``NodeState``), z. B. fuer die
 * Unterzeile der Prozesslandkarte. Leer bleibt leer.
 * @param {string} s Markierung
 * @returns {string}
 */
function nodeStateLabel(s) { return s ? ((NODE_STATE_META[s] || {}).label || s) : ""; }

function nodeStatePill(s) {
  const m = NODE_STATE_META[s] || { label: s, cls: "" };
  return el("span", { class: "pill " + m.cls }, m.label);
}

// Oben rechts: der angemeldete Starter (wer die Instanz gestartet hat).
function testStarterPanel(runSchema) {
  const org = runSchema.org_model || { agents: {} };
  const id = state.testStarter;
  const body = el("div", { class: "panel-b" });
  if (!id || !(org.agents || {})[id]) {
    body.appendChild(emptyState("Kein Starter erfasst."));
  } else {
    const info = agentIdentity(org, id);
    body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Angemeldet als " + info.name));
    body.appendChild(el("div", { class: "id-card" },
      idRow("Person", info.name),
      idRow("Rollen", info.roles || "\u2013"),
      idRow("Abteilung", info.unit || "\u2013")));
    body.appendChild(el("p", { class: "muted", style: "margin-top:12px; font-size:12px" },
      "Diese Person hat die Pr\u00FCfinstanz gestartet. Im Anmeldebetrieb entspr\u00E4che dies der angemeldeten Kennung."));
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Starter"), el("span", { class: "sub" }, "wer die Instanz gestartet hat")),
    body);
}

// Unten: ein frei wählbarer beteiligter Agent mit seiner instanzgefilterten
// Arbeitsliste. ``slot`` ist "A" (links) oder "B" (rechts).
function testAgentPanel(slot, inst, runSchema, agents, instanceTasks) {
  const org = runSchema.org_model || { agents: {} };
  const current = slot === "A" ? state.testAgentA : state.testAgentB;
  const sel = el("select", null,
    el("option", { value: "" }, "\u2013 Agent w\u00E4hlen \u2013"),
    ...agents.map((a) => el("option", { value: a.id }, a.name)));
  sel.value = current || "";
  sel.addEventListener("change", () => {
    if (slot === "A") state.testAgentA = sel.value || null;
    else state.testAgentB = sel.value || null;
    persistTestState();
    render();
  });

  const body = el("div", { class: "panel-b" },
    el("label", { class: "field" }, "Beteiligter Agent (frei w\u00E4hlbar)", sel));

  if (current && (org.agents || {})[current]) {
    const info = agentIdentity(org, current);
    const meta = [info.roles ? "Rollen: " + info.roles : null, info.unit || null].filter(Boolean).join(" \u00B7 ");
    if (meta) body.appendChild(el("div", { class: "muted", style: "font-size:12px; margin:2px 0 10px" }, meta));

    if (inst.state === "COMPLETED") {
      body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Instanz abgeschlossen \u2013 keine offenen Aufgaben."));
    } else {
      const mine = (instanceTasks || []).filter((t) => (t.eligible_agents || []).includes(current));
      if (!mine.length) {
        body.appendChild(el("div", { class: "muted", style: "font-size:13px" },
          "Keine offenen Aufgaben f\u00FCr " + info.name + " in dieser Pr\u00FCfinstanz."));
      } else {
        mine.forEach((t) => {
          const node = runSchema.nodes[t.node_id];
          const badge = criticalityBadge(t);
          const item = el("div", { class: "worklist-item" },
            el("span", { class: "name" }, t.label || (node ? nodeCaption(node) : t.node_id)),
            el("span", { class: "tag" }, priorityShort(t.priority)));
          if (badge) item.appendChild(badge);
          item.appendChild(el("button", { class: "btn small green", onClick: () => completeTestTask(inst, runSchema, t, current) }, "Erledigen"));
          body.appendChild(item);
        });
      }
    }
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:13px" },
      "W\u00E4hlen Sie einen beteiligten Agenten, um dessen auf diese Pr\u00FCfinstanz gefilterte Arbeitsliste zu sehen."));
  }

  const who = current && (org.agents || {})[current] ? agentIdentity(org, current).name : "kein Agent";
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Arbeitsliste " + slot), el("span", { class: "sub" }, who)),
    body);
}

async function completeTestTask(inst, schema, task, agentId) {
  await promptComplete(schema, inst.id, task.node_id, task.label || task.node_id, agentId, async () => {
    await loadTestInstance(inst.id);
    render();
  }, inst.data_values);
}

// Identität eines Agenten (Name, Rollen, Abteilung) aus einem gegebenen
// Organisationsmodell -- robust auch für Ad-hoc-Instanzschemata.
function agentIdentity(org, agentId) {
  const a = org && org.agents ? org.agents[agentId] : null;
  if (!a) return { name: agentId || "\u2013", roles: "", unit: "" };
  const roles = (a.role_ids || []).map((r) => (org.roles && org.roles[r] ? org.roles[r].name : r)).join(", ");
  const unit = a.org_unit_id && org.org_units && org.org_units[a.org_unit_id] ? org.org_units[a.org_unit_id].name : "";
  return { name: a.name, roles, unit };
}

function idRow(label, value) {
  return el("div", { class: "id-row" },
    el("span", { class: "id-label" }, label), el("span", { class: "id-value" }, value));
}

// Agentenname aus einem konkreten Schema (statt dem global geladenen state.schema).
function agentNameOfIn(schema, id) {
  const org = schema && schema.org_model;
  const a = org && org.agents ? org.agents[id] : null;
  return a ? a.name : id;
}
