// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Ausfuehrung.
 *
 * Die Ausfuehrungssicht: Vorgaenge starten, Schritte uebernehmen und
 * abschliessen, Instanzdaten und Ad-hoc-Aenderungen.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// View: Ausfuehrung
// --------------------------------------------------------------------------

async function viewRun() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;

  const header = el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, schema.name), el("span", { class: "sub" }, `v${schema.version}`), lifecyclePill(schema),
      el("span", { class: "spacer", style: "flex:1" }),
      schema.lifecycle_state === "RELEASED"
        ? el("button", { class: "btn small primary", onClick: startInstance }, "\u25B6 Instanz starten")
        : hasRole("modeler", "admin")
          ? el("button", { class: "btn small", onClick: startInstance }, "\u25B6 Test-Instanz starten")
          : el("span", { class: "muted", style: "font-size:12px" }, "Nur freigegebene Schemata sind instanziierbar.")));
  content.appendChild(header);

  if (!state.instance) {
    content.appendChild(emptyState("Keine Instanz geladen. Starte eine Instanz oder w\u00E4hle eine im Monitoring."));
    return;
  }
  await renderInstanceDetail(content, true);
}

async function startInstance() {
  try {
    const inst = await api.post(`/schemas/${state.schemaId}/instances`);
    await loadInstance(inst.id);
    render();
    toast("ok", inst.is_test ? "Test-Instanz gestartet" : "Instanz gestartet",
      [`${instanceCaption(inst, state.schema)} \u00B7 ${inst.id}`]);
  } catch (err) { toastError(err); }
}

async function loadInstance(id) {
  state.instanceId = id;
  state.instance = await api.get(`/instances/${id}`);
  state.worklist = await api.get(`/instances/${id}/worklist`);
  rememberInstance(id);
}

/** sessionStorage-Schluessel des gewaehlten Vorgangs (siehe ``rememberInstance``). */
const INSTANCE_KEY = "selectedInstance";

/**
 * Merkt den gewaehlten Vorgang fuer ein Neuladen **dieses Tabs**.
 *
 * Nach F5 stand „Keine Instanz geladen“, obwohl man gerade an einem Vorgang
 * war. Gemerkt wird in ``sessionStorage`` (nur dieser Tab, endet mit ihm) und
 * **zusammen mit dem Login**: Ein Vorgang ist personenbezogen -- die naechste
 * Anmeldung darf ihn nie erben, auch nicht ueber einen anderen Tab, der sich
 * inzwischen als jemand anderes angemeldet hat. ``resetSessionState`` und
 * ``dropUnreadableInstance`` loeschen den Eintrag.
 * @param {string} id Vorgang
 */
function rememberInstance(id) {
  const owner = (state.principal && state.principal.subject) || "";
  storageSet(sessionStorage, INSTANCE_KEY, JSON.stringify({ owner, id }));
}

/** Vergisst den gemerkten Vorgang (Abmelden, Login-Wechsel, nicht mehr lesbar). */
function forgetInstance() {
  try { sessionStorage.removeItem(INSTANCE_KEY); } catch (_e) { /* nur Komfort */ }
}

/**
 * Laedt beim Start den gemerkten Vorgang wieder -- nur fuer denselben Login.
 *
 * Gehoert der Eintrag einem anderen Login (oder ist er unlesbar), wird er
 * verworfen und nichts geladen. Verweigert der Kern den Vorgang (404/403:
 * geloescht oder nicht mehr einsehbar), erscheint der Hinweis aus
 * ``dropUnreadableInstance``; bei anderen Fehlern bleibt die Auswahl leer.
 * Der Prozess des Vorgangs wird mitgewaehlt, damit die Ausfuehrung ihn zeigt.
 * @returns {Promise<boolean>} true, wenn ein Vorgang wiederhergestellt wurde
 */
async function restoreInstance() {
  if (state.instanceId) return false;
  let saved = null;
  try { saved = JSON.parse(storageGet(sessionStorage, INSTANCE_KEY) || "null"); } catch (_e) { saved = null; }
  const owner = (state.principal && state.principal.subject) || "";
  if (!saved || !saved.id || saved.owner !== owner) { forgetInstance(); return false; }
  try {
    await loadInstance(saved.id);
  } catch (err) {
    if (!dropUnreadableInstance(err)) { state.instanceId = null; state.instance = null; state.worklist = null; }
    forgetInstance();
    return false;
  }
  if (state.instance.schema_id !== state.schemaId) {
    // Ein Fehler hier darf den Start nicht abbrechen (sonst liefe etwa die
    // Live-Aktualisierung nie an): dann eben ohne wiederhergestellten Vorgang.
    const previous = state.schemaId;
    try {
      state.schemaId = state.instance.schema_id;
      await refreshSchema();
      renderSchemaPicker();
    } catch (_e) {
      // Zurueck zum vorher gewaehlten Prozess und ihn vollstaendig neu laden
      // (Schema, Pruefergebnis, gemerkte Auswahl) -- ``refreshSchema`` kann
      // schon teilweise umgestellt haben. Scheitert auch das, bleibt der Start
      // trotzdem stehen.
      state.schemaId = previous;
      try { await refreshSchema(); renderSchemaPicker(); } catch (_e2) { /* bestmoeglich */ }
      state.instanceId = null; state.instance = null; state.worklist = null;
      forgetInstance();
      return false;
    }
  }
  return true;
}

async function renderInstanceDetail(container, withActions) {
  const inst = state.instance;
  const wl = state.worklist;
  // Schema, gegen das die Instanz laeuft (ggf. Ad-hoc-Variante)
  const runSchema = inst.ad_hoc_schema || state.schema;
  const statePill = statePillFor(inst.state);

  const graphPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Live-Prozesslandkarte"), el("span", { class: "sub", title: `Kennung: ${inst.id}` }, instanceCaption(inst, runSchema)), statePill,
      inst.is_test ? el("span", { class: "pill pill-amber", title: "Test-Instanz \u2013 nicht im Monitoring gez\u00E4hlt" }, "TEST") : null),
    el("div", { class: "panel-b" }, renderGraph(runSchema, { instance: inst })));

  // Worklist
  const wlBody = el("div", { class: "panel-b" });
  // Zustaendigkeit je bereitem Schritt (aus der Bearbeiterregel, inkl.
  // Vertretung). Die Instanz-Sicht zeigt weiter ALLE bereiten Schritte (E1:
  // Transparenz), aber sie sagt, wer zustaendig ist, und bietet das Abschliessen
  // nur passend an -- vorher stand bei jedem Schritt „Abschliessen“, egal fuer wen.
  const eligibleOf = {};
  if (inst.state !== "COMPLETED") {
    try {
      (await api.get(`/instances/${inst.id}/tasks`)).forEach((t) => { eligibleOf[t.node_id] = t.eligible_agents || []; });
    } catch (e) { /* best effort: ohne Liste bleibt die bisherige Anzeige */ }
  }
  if (inst.state === "COMPLETED") {
    wlBody.appendChild(el("div", { class: "ok-banner" }, "\u2713 Instanz abgeschlossen \u2013 jeder Schritt ist erledigt oder \u00FCbersprungen."));
  } else {
    (wl.ready_activities || []).forEach((nid) => {
      const node = runSchema.nodes[nid];
      // E1/E2: Transparenz statt Verstecken \u2013 die Instanz-Sicht zeigt alle
      // bereiten Schritte samt Inhaber und Detailzustand (Pause/Scheitern
      // inkl. Begr\u00FCndung); die pers\u00F6nlichen Listen filtern.
      const owner = (inst.claimed_by || {})[nid];
      const nodeDetail = (inst.node_details || {})[nid];
      const detailTag = nodeDetail === "SUSPENDED"
        ? el("span", { class: "tag" }, "angehalten")
        : nodeDetail === "FAILED"
          ? el("span", { class: "tag" }, "gescheitert: " + ((inst.node_detail_reason || {})[nid] || "ohne Begr\u00FCndung"))
          : null;
      const action = withActions ? completionActionFor(inst, nid, node, eligibleOf[nid], runSchema) : null;
      wlBody.appendChild(el("div", { class: "worklist-item" },
        el("span", { class: "name" }, node ? nodeCaption(node) : nid),
        owner
          ? el("span", { class: "tag" }, "\u00FCbernommen von " + agentNameOf(owner))
          : el("span", { class: "tag" }, "bereit"),
        eligibleOf[nid] && eligibleOf[nid].length
          ? el("span", { class: "tag muted", title: eligibleOf[nid].map(agentNameOf).join(", ") }, "zust\u00E4ndig: " + responsibleSummary(eligibleOf[nid]))
          : unstaffedTag(runSchema, nid),
        detailTag,
        action));
    });
    if (!(wl.ready_activities || []).length) {
      wlBody.appendChild(el("div", { class: "muted", style: "font-size:13px" }, "Keine bereiten Schritte (l\u00E4uft automatisch weiter oder wartet auf Teilprozess)."));
    }
  }
  const wlPanel = el("div", { class: "panel" }, el("div", { class: "panel-h" }, el("h2", null, "Arbeitsliste")), wlBody);

  // Datenwerte
  const dataRows = Object.entries(inst.data_values || {}).map(([k, v]) => {
    const elem = runSchema.data_elements[k];
    return [elem ? elem.name : k, formatValue(elem, v)];
  });
  // Daten koennen direkt nach dem Start eingegeben werden – unabhaengig davon,
  // ob schon eine Aktivitaet aktiviert wurde.
  // „Daten eingeben“ nur, wo der Kern es grundsaetzlich erlaubt (Spiegel von
  // _authorize_data_write, nur Sichtbarkeit): Schreibrolle vorausgesetzt;
  // dann Modellierer/Admin, jeder Test-Vorgang, wer fuer andere handeln darf
  // (offener/Token-Modus, Rolle integration) oder eine Person mit eigenem
  // offenem Schritt. Den Einzelfall entscheidet weiter der Kern.
  const meAgent = state.principal && state.principal.agent_id;
  const ownOpenStep = !!meAgent && hasRole("operator")
    && Object.values(eligibleOf || {}).some((ids) => (ids || []).includes(meAgent));
  const canEditData = withActions && inst.state !== "COMPLETED"
    && hasRole("operator", "modeler", "admin")
    && (hasRole("modeler", "admin") || inst.is_test || mayActForOthers() || ownOpenStep)
    && Object.values(runSchema.data_elements || {}).some((e) => e.source !== "EXTERNAL");
  const dataPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Instanzdaten"),
      canEditData ? el("span", { class: "spacer", style: "flex:1" }) : null,
      canEditData ? el("button", { class: "btn small", onClick: () => openInstanceDataForm(runSchema, inst) }, "Daten eingeben") : null),
    el("div", { class: "panel-b" }, dataRows.length ? table(["Element", "Wert"], dataRows) : emptyState("Noch keine Werte.")));

  // Audit-Timeline (Schritt 15)
  let events = [];
  try { events = await api.get(`/instances/${inst.id}/audit`); } catch (e) { /* ignore */ }
  const tlBody = el("div", { class: "panel-b" });
  if (!events.length) {
    tlBody.appendChild(emptyState("Noch keine Ereignisse aufgezeichnet."));
  } else {
    const tl = el("div", { class: "timeline" });
    // Spaltenkopf: der Bearbeiter (Akteur) steht in einer eigenen Spalte.
    tl.appendChild(el("div", { class: "tl-item tl-head" },
      el("span", { class: "tl-time" }, "Zeit"),
      el("span", { class: "tl-type" }, "Ereignis"),
      el("span", { class: "tl-actor" }, "Bearbeiter"),
      el("span", { class: "tl-meta" }, "Detail")));
    events.forEach((ev) => {
      tl.appendChild(el("div", { class: "tl-item" },
        el("span", { class: "tl-time" }, fmtTimestamp(ev.timestamp)),
        el("span", { class: "tl-type" }, eventLabel(ev.event_type)),
        el("span", { class: "tl-actor" }, auditActorLabel(ev)),
        el("span", { class: "tl-meta" }, auditDetailText(ev))));
    });
    tlBody.appendChild(tl);
  }
  const timelinePanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Audit-Verlauf"), el("span", { class: "sub" }, countLabel(events.length, "Ereignis", "Ereignisse"))),
    tlBody);

  // Ad-hoc-Instanzanpassung (Schema-Evolution einer einzelnen Instanz, R1/R2).
  // Der Modellierer darf eine laufende Instanz an die Realitaet anpassen, ohne
  // das freigegebene Schema zu aendern. Angeboten werden nur Aenderungen im
  // noch nicht ausgefuehrten Bereich (R1); der Kern prueft zusaetzlich R2.
  const adhocPanel = (withActions && hasRole("modeler", "admin") && inst.state !== "COMPLETED")
    ? renderAdhocPanel(runSchema, inst)
    : null;

  // Neuere freigegebene Version vorhanden? Dann Migration anbieten (derselbe
  // Assistent wie in der Modellieren-Sicht, auf diese eine Instanz beschraenkt).
  const migratePanel = (withActions && hasRole("operator", "modeler", "admin")
      && inst.state === "RUNNING" && !inst.is_test && !inst.parent_instance_id)
    ? await instanceMigrationPanel(inst)
    : null;

  container.appendChild(el("div", { class: "grid-2" }, graphPanel, el("div", null, migratePanel, wlPanel, dataPanel, adhocPanel, timelinePanel)));
}

// Panel fuer die Ad-hoc-Anpassung einer einzelnen Instanz. Die Auswahl der
// erlaubten Ziele spiegelt R1 aus procworks.adhoc wider (nur der noch nicht
// ausgefuehrte Bereich ist aenderbar); der Kern setzt R1 und R2 verbindlich
// durch, die UI bietet nur zulaessige Aktionen vorab an.
function renderAdhocPanel(schema, inst) {
  const nodes = schema.nodes || {};
  const reached = (nid) => {
    const s = (inst.node_states || {})[nid];
    return s !== undefined && s !== "NOT_ACTIVATED";
  };
  const edgeSignaled = (source, target) => {
    const st = (inst.edge_states || {})[`${source}->${target}`];
    return st !== undefined && st !== "NOT_SIGNALED";
  };
  const out = (nid) => (schema.edges || []).filter((e) => e.source === nid);
  const inc = (nid) => (schema.edges || []).filter((e) => e.target === nid);

  // R1 fuer Einfuegen: Anker != END, genau eine ausgehende, noch nicht
  // signalisierte Kante, Nachfolger noch nicht erreicht.
  const insertAnchors = Object.values(nodes).filter((n) => {
    if (n.type === "END") return false;
    const o = out(n.id);
    if (o.length !== 1) return false;
    const succ = o[0].target;
    return !edgeSignaled(n.id, succ) && !reached(succ);
  });
  // R1 fuer Umbenennen: ACTIVITY/SUBPROCESS, noch nicht erreicht.
  const renameTargets = Object.values(nodes).filter(
    (n) => (n.type === "ACTIVITY" || n.type === "SUBPROCESS") && !reached(n.id));
  // R1 fuer Entfernen: serielle ACTIVITY (eine rein/eine raus), noch nicht erreicht.
  const deleteTargets = Object.values(nodes).filter(
    (n) => n.type === "ACTIVITY" && !reached(n.id) && inc(n.id).length === 1 && out(n.id).length === 1);

  const body = el("div", { class: "panel-b" });
  body.appendChild(el("p", { class: "muted", style: "font-size:13px;margin-top:0" },
    "Passt diese eine Instanz an die Realit\u00E4t an, ohne das freigegebene Schema zu \u00E4ndern. " +
    "Nur der noch nicht ausgef\u00FChrte Bereich ist \u00E4nderbar (R1); jede \u00C4nderung wird vor der \u00DCbernahme auf Korrektheit gepr\u00FCft (R2)."));
  body.appendChild(el("div", { class: "btn-row" },
    el("button", { class: "btn small", disabled: insertAnchors.length ? null : "disabled",
      onClick: () => openAdhocInsert(schema, inst, insertAnchors) }, "Schritt einf\u00FCgen"),
    el("button", { class: "btn small", disabled: renameTargets.length ? null : "disabled",
      onClick: () => openAdhocRename(schema, inst, renameTargets) }, "Schritt umbenennen"),
    el("button", { class: "btn small danger", disabled: deleteTargets.length ? null : "disabled",
      onClick: () => openAdhocDelete(schema, inst, deleteTargets) }, "Schritt entfernen")));

  const deltas = inst.ad_hoc_deltas || [];
  if (deltas.length) {
    const list = el("ul", { class: "adhoc-deltas" }, ...deltas.map((d) => el("li", null, d)));
    body.appendChild(el("div", { class: "adhoc-log" },
      el("div", { class: "sub", style: "margin:8px 0 4px" }, "Angewendete Anpassungen"), list));
  }

  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Instanz anpassen (Ad-hoc)")), body);
}

async function reloadInstance(instId) {
  await loadInstance(instId);
  render();
}

/**
 * Bearbeiterregel, die ein ad hoc eingefuegter Schritt als Vorschlag erbt: die
 * des Ankers bzw. -- ist der Anker ein Gateway oder ohne Regel -- die des
 * naechsten Schritts davor (Rueckweg ueber die erste eingehende Kante).
 * Reine Vorbelegung; ob die Regel passt, prueft der Kern (Z1-Z3, B2).
 * @param {object} schema wirksames Schema der Instanz
 * @param {string} anchorId Knoten, hinter dem eingefuegt wird
 * @returns {{rule: object, from: string}|null} Regel und Schrittname oder null
 */
function adhocSuggestedRule(schema, anchorId) {
  const rules = schema.staff_rules || {};
  const seen = new Set();
  let current = anchorId;
  while (current && !seen.has(current)) {
    seen.add(current);
    if (rules[current]) {
      const n = (schema.nodes || {})[current];
      return { rule: rules[current], from: n ? nodeCaption(n) : current };
    }
    const inc = controlEdges(schema).filter((e) => e.target === current);
    current = inc.length ? inc[0].source : null;
  }
  return null;
}

/**
 * Eingabefeld „Anlass" fuer eine Ad-hoc-Aenderung.
 *
 * Eine Ad-hoc-Aenderung legt fest, wie *dieser eine* laufende Vorgang
 * weitergeht; der Kern verlangt deshalb fuer echte Vorgaenge einen Anlass und
 * schreibt ihn mit dem Namen der handelnden Person in den Verlauf. Test-
 * Instanzen schreiben keinen Verlauf -- dort bleibt das Feld freiwillig.
 * @param {object} inst die Instanz, die geaendert wird
 * @returns {{input: HTMLInputElement, field: HTMLElement, value: function(): (string|null)}}
 *   ``value()`` liefert den Anlass oder ``null``, wenn er fehlt, obwohl er
 *   Pflicht ist (dann ist bereits ein Hinweis angezeigt).
 */
function adhocReasonField(inst) {
  const required = !inst.is_test;
  const input = el("input", { type: "text",
    placeholder: required ? "z. B. Kunde verlangt Zusatzpr\u00FCfung" : "optional" });
  const field = el("label", { class: "field" }, required ? "Anlass *" : "Anlass", input);
  const value = () => {
    const text = input.value.trim();
    if (required && !text) {
      toast("err", "Bitte einen Anlass angeben", ["Er steht mit deinem Namen im Verlauf des Vorgangs."]);
      return null;
    }
    return text;
  };
  return { input, field, value };
}

// Ad-hoc: neuen seriellen Schritt hinter einem Anker einfuegen.
//
// Der Schritt braucht eine Bearbeiterregel (B2 im Ad-hoc-Pfad): ohne
// sie stand er zur Laufzeit in keiner Arbeitsliste, und der Vorgang kam nur per
// Aufsichtseingriff weiter. Vorbelegt ist die Regel des Schritts davor
// (``adhocSuggestedRule``); daneben Rollen, Abteilungen und Personen des
// Organisationsmodells. Die Entscheidung trifft der Kern.
function openAdhocInsert(schema, inst, anchors) {
  const anchorSel = el("select", null,
    ...anchors.map((n) => el("option", { value: n.id }, nodeCaptionInContext(schema, n))));
  const labelInput = el("input", { type: "text", placeholder: "Bezeichnung des neuen Schritts" });
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };
  const choices = {};
  const staffSel = el("select");
  const fillStaff = () => {
    staffSel.innerHTML = "";
    Object.keys(choices).forEach((k) => delete choices[k]);
    const add = (group, key, label, rule) => {
      choices[key] = rule;
      group.appendChild(el("option", { value: key }, label));
    };
    const suggestion = adhocSuggestedRule(schema, anchorSel.value);
    staffSel.appendChild(el("option", { value: "" }, "\u2013 bitte w\u00E4hlen \u2013"));
    if (suggestion) {
      const g = el("optgroup", { label: "Vorschlag" });
      add(g, "SUGGEST", `Wie \u201E${suggestion.from}\u201C: ${describeRule(suggestion.rule, schema)}`, suggestion.rule);
      staffSel.appendChild(g);
    }
    [["Rollen", "ROLE", org.roles], ["Abteilungen", "ORG_UNIT", org.org_units], ["Personen", "AGENT", org.agents]]
      .forEach(([title, kind, entries]) => {
        const list = Object.values(entries || {});
        if (!list.length) return;
        const g = el("optgroup", { label: title });
        list.forEach((x) => add(g, `${kind}:${x.id}`, x.name, { kind, ref: x.id }));
        staffSel.appendChild(g);
      });
    staffSel.value = suggestion ? "SUGGEST" : "";
  };
  anchorSel.addEventListener("change", fillStaff);
  fillStaff();
  const reason = adhocReasonField(inst);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Einf\u00FCgen hinter", anchorSel),
    el("label", { class: "field" }, "Neuer Schritt", labelInput),
    el("label", { class: "field" }, "Bearbeiter *", staffSel),
    reason.field);
  openModal("Schritt einf\u00FCgen (Ad-hoc)", body, async () => {
    const label = labelInput.value.trim();
    if (!label) { toast("info", "Bitte eine Bezeichnung angeben."); return false; }
    const rule = choices[staffSel.value];
    if (!rule) { toast("err", "Bitte festlegen, wer den neuen Schritt bearbeitet."); return false; }
    const why = reason.value();
    if (why === null) return false;
    const payload = { after_node_id: anchorSel.value, label, staff_rule: rule };
    if (why) payload.reason = why;
    try {
      await api.post(`/instances/${inst.id}/adhoc/insert`, payload);
      toast("ok", "Schritt eingef\u00FCgt", [describeRule(rule, schema)]);
      await reloadInstance(inst.id);
    } catch (err) { toastError(err); return false; }
  }, "Einf\u00FCgen");
}

// Ad-hoc: noch nicht erreichten Schritt umbenennen.
function openAdhocRename(schema, inst, targets) {
  const targetSel = el("select", null,
    ...targets.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const labelInput = el("input", { type: "text", placeholder: "Neue Bezeichnung" });
  const syncLabel = () => {
    const n = schema.nodes[targetSel.value];
    labelInput.value = n ? (n.label || "") : "";
  };
  targetSel.addEventListener("change", syncLabel);
  syncLabel();
  const reason = adhocReasonField(inst);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Schritt", targetSel),
    el("label", { class: "field" }, "Neue Bezeichnung", labelInput),
    reason.field);
  openModal("Schritt umbenennen (Ad-hoc)", body, async () => {
    const label = labelInput.value.trim();
    if (!label) { toast("info", "Bitte eine Bezeichnung angeben."); return false; }
    const why = reason.value();
    if (why === null) return false;
    try {
      await api.post(`/instances/${inst.id}/adhoc/rename`,
        why ? { node_id: targetSel.value, label, reason: why } : { node_id: targetSel.value, label });
      toast("ok", "Schritt umbenannt");
      await reloadInstance(inst.id);
    } catch (err) { toastError(err); return false; }
  }, "Umbenennen");
}

// Ad-hoc: noch nicht erreichten seriellen Schritt entfernen.
function openAdhocDelete(schema, inst, targets) {
  const targetSel = el("select", null,
    ...targets.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const reason = adhocReasonField(inst);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Zu entfernender Schritt", targetSel),
    reason.field,
    el("p", { class: "muted", style: "font-size:13px" },
      "Vorg\u00E4nger und Nachfolger werden wieder direkt verbunden."));
  openModal("Schritt entfernen (Ad-hoc)", body, async () => {
    const why = reason.value();
    if (why === null) return false;
    try {
      await api.post(`/instances/${inst.id}/adhoc/delete`,
        why ? { node_id: targetSel.value, reason: why } : { node_id: targetSel.value });
      toast("ok", "Schritt entfernt");
      await reloadInstance(inst.id);
    } catch (err) { toastError(err); return false; }
  }, "Entfernen", { danger: true });
}

/**
 * Warnzeichen fuer einen bereiten Schritt, dessen Bearbeiterregel niemanden
 * findet.
 *
 * Ohne es saehe dieser Fall aus wie ein Schritt ganz ohne Bearbeiterregel: kein
 * „zustaendig"-Eintrag, ein gruenes „Abschliessen" -- und der Schritt taucht in
 * keiner persoenlichen Arbeitsliste auf, ohne dass irgendwo stand, warum. Der
 * haeufigste Grund ist eine relative Regel (Vier-Augen), deren Bezugsschritt per
 * Aufsichtseingriff ohne Bearbeiter erledigt wurde; der Hinweistext nennt genau
 * das, wenn er zutrifft.
 *
 * @param {object} schema Schema der Instanz
 * @param {string} nid Knoten-ID des bereiten Schritts
 * @returns {HTMLElement|null} das Zeichen, oder null ohne Bearbeiterregel
 */
function unstaffedTag(schema, nid) {
  const rule = ((schema || {}).staff_rules || {})[nid];
  if (!rule) return null;  // Automatik-/unbesetzter Schritt: kein Widerspruch
  const why = ruleIsRelative(rule)
    ? " Sie bezieht sich auf die Person eines früheren Schritts – wurde dieser als Aufsichtseingriff ohne Bearbeiter abgeschlossen, gibt es diese Person nicht."
    : " Keiner der in Frage kommenden Bearbeiter ist derzeit im Organisationsmodell zu finden.";
  return el("span", { class: "tag warn", title: `Bearbeiterregel: ${describeRule(rule, schema)}.${why} Nur eine Aufsicht kann den Schritt abschließen.` },
    "niemand zuständig");
}

/**
 * Kurzform der Zustaendigen: bis zu zwei Namen, sonst „Name, Name +N“.
 * @param {string[]} agentIds zustaendige Agenten
 * @returns {string}
 */
function responsibleSummary(agentIds) {
  const names = agentIds.map(agentNameOf);
  return names.length <= 2 ? names.join(", ") : `${names.slice(0, 2).join(", ")} +${names.length - 2}`;
}

/**
 * Abschliessen-Knopf eines bereiten Schritts in der Instanz-Sicht, passend zur Rolle.
 *
 * * An eine Person gebunden und zustaendig (oder Schritt ohne Bearbeiterregel):
 *   „Abschliessen“.
 * * Gebunden, aber nicht zustaendig: kein Knopf, sondern ein Hinweis -- der Kern
 *   lehnte den Abschluss ohnehin ab (409).
 * * Nicht gebunden (Modellierer/Administrator) bei einem Schritt mit
 *   Bearbeiterregel: „Als Aufsicht abschliessen“. Der Kern verlangt dann eine
 *   Begruendung und protokolliert den Eingriff; bei aktiver Lizenzierung lehnt er
 *   ihn ab. Test-Instanzen sind davon ausgenommen (normales „Abschliessen“).
 *
 * Der Client entscheidet nichts endgueltig -- er waehlt nur die passende Form.
 *
 * Sonderfall **Regel ohne Zustaendige**: Traegt
 * der Schritt eine Bearbeiterregel, loest sie aber gerade niemanden auf -- etwa
 * eine Vier-Augen-Regel, deren Vorschritt per Aufsichtseingriff ohne Bearbeiter
 * erledigt wurde --, dann stand hier bisher das gruene "Abschliessen", als waere
 * alles in Ordnung. Jetzt heisst es auch dort "Als Aufsicht abschliessen"; einer
 * gebundenen Person wird kein Knopf angeboten, den der Kern mit 409 abweisen
 * wuerde.
 *
 * @param {object} inst die Instanz
 * @param {string} nid Knoten-ID
 * @param {object} node der Knoten
 * @param {string[]|undefined} eligible zustaendige Agenten (undefined = unbekannt)
 * @param {object} [schema] Schema der Instanz (fuer die Bearbeiterregel)
 * @returns {HTMLElement}
 */
function completionActionFor(inst, nid, node, eligible, schema) {
  // Abschliessen (auch als Aufsicht) erlaubt der Kern nur Bearbeitern,
  // Modellierern und Administratoren. Ein Leser bekam die Knoepfe trotzdem
  // und scheiterte erst nach dem Ausfuellen der Maske mit 403.
  if (!hasRole("operator", "modeler", "admin")) return null;
  const me = state.principal && state.principal.agent_id;
  const staffed = eligible && eligible.length > 0;
  const ruled = !!((schema || {}).staff_rules || {})[nid];
  if (me && staffed && !eligible.includes(me)) {
    return el("span", { class: "tag muted" }, "nicht deine Aufgabe");
  }
  if (ruled && !staffed && !inst.is_test) {
    if (me) return el("span", { class: "tag muted" }, "nur per Aufsicht abschlie\u00DFbar");
    return el("button", { class: "btn small",
      title: "Die Bearbeiterregel dieses Schritts findet aktuell niemanden \u2013 der Abschluss wird als Aufsichtseingriff mit Begr\u00FCndung protokolliert",
      onClick: () => completeActivity(nid, node) }, "Als Aufsicht abschlie\u00DFen");
  }
  if (!me && staffed && !inst.is_test) {
    return el("button", { class: "btn small",
      title: "Du bist keinem Bearbeiter zugeordnet – der Abschluss wird als Aufsichtseingriff mit Begründung protokolliert",
      onClick: () => completeActivity(nid, node) }, "Als Aufsicht abschlie\u00DFen");
  }
  return el("button", { class: "btn small green", onClick: () => completeActivity(nid, node) }, "Abschlie\u00DFen");
}

async function completeActivity(nodeId, node) {
  const schema = state.instance.ad_hoc_schema || state.schema;
  await promptComplete(schema, state.instanceId, nodeId, node ? nodeCaption(node) : nodeId, null,
    async () => { await loadInstance(state.instanceId); render(); },
    state.instance.data_values);
}

// Instanzdaten direkt eingeben/aendern – ohne eine Aktivitaet abzuschliessen.
// Angeboten werden alle INSTANCE-Datenelemente des Schemas (EXTERNAL-Elemente
// werden zur Laufzeit ueber Connectoren aufgeloest und daher nicht abgefragt).
//
// Wer was aendern darf, entscheidet allein der Kern: ein Bearbeiter
// nur die Werte seines eigenen offenen Schritts (sonst 403 mit Erklaerung),
// Modellierer/Admin alles Uebrige als Aufsichtseingriff mit Begruendung. Der
// Client kennt die Regel nicht; er reagiert auf die 422 „Aufsichtseingriff…"
// mit der Frage nach der Begruendung und sendet erneut. Die Felder kommen aus
// derselben Widget-Factory wie die Aufgabenmaske (``maskControl`` +
// ``fallbackWidget``) -- frueher wandelte der Dialog Ja/Nein-Freitext selbst
// und still in ``false``.
function openInstanceDataForm(schema, inst) {
  const elems = Object.values(schema.data_elements || {}).filter((e) => e.source !== "EXTERNAL");
  if (!elems.length) { toast("info", "Keine Instanz-Datenelemente definiert."); return; }
  const inputs = {};
  const body = el("div", { class: "form-grid" });
  elems.forEach((elem) => {
    const cur = (inst.data_values || {})[elem.id];
    const { control, read } = maskControl(elem, fallbackWidget(elem), null, cur);
    inputs[elem.id] = { read, cur };
    body.appendChild(el("label", { class: "field" }, elem.name, control));
  });
  const submit = async (values, reason) => {
    try {
      const payload = { values };
      if (reason) payload.reason = reason;
      await api.put(`/instances/${inst.id}/data`, payload);
      toast("ok", "Instanzdaten gespeichert");
      await loadInstance(inst.id);
      render();
    } catch (err) {
      if (isSupervisionRequired(err)) { askDataCorrectionReason((text) => submit(values, text)); return false; }
      toastError(err); return false;
    }
  };
  openModal("Instanzdaten eingeben", body, async () => {
    const values = {};
    for (const [eid, { read, cur }] of Object.entries(inputs)) {
      const val = read();
      if (val === undefined || val === cur) continue;
      values[eid] = val;
    }
    if (!Object.keys(values).length) { toast("info", "Keine ge\u00E4nderten Werte."); return; }
    return submit(values, null);
  }, "Speichern");
}

/**
 * Fragt die Begruendung einer Datenkorrektur ab (Aufsichtseingriff):
 * Die Werte gehoeren zu keinem Schritt, den der Login gerade bearbeitet. Die
 * Begruendung steht danach im Audit-Verlauf neben altem und neuem Wert.
 * @param {(reason: string) => Promise<unknown>} onConfirm sendet erneut mit Begruendung
 */
function askDataCorrectionReason(onConfirm) {
  const reason = el("textarea", { rows: "3", placeholder: "z. B. Tippfehler im Antrag, R\u00FCcksprache mit der Antragstellerin" });
  const body = el("div", { class: "form-grid" },
    el("div", { class: "card-hint" },
      "Diese Werte geh\u00F6ren zu keinem Schritt, den du gerade bearbeitest. Die \u00C4nderung wird als Aufsichtseingriff mit altem und neuem Wert und deiner Begr\u00FCndung im Audit-Verlauf festgehalten."),
    el("label", { class: "field" }, "Begr\u00FCndung *", reason));
  openModal("Daten korrigieren", body, async () => {
    const text = reason.value.trim();
    if (!text) { toast("err", "Bitte eine Begr\u00FCndung angeben"); return false; }
    return onConfirm(text);
  }, "Korrektur speichern");
}

// Schliesst einen Schritt ueber seine Eingabemaske ab.
//
// ``dataValues`` sind die aktuellen Instanzdaten (``ProcessInstance.data_values``)
// und belegen jedes Feld vor, dessen Element schon einen Wert traegt. Das ist fuer
// datenreiche Prozesse wesentlich und nicht bloss Komfort: Ein Nur-Lese-Feld ist
// per Definition die *Entscheidungsgrundlage* des Schritts (der Auftragswert bei
// der Freigabe, der Rechnungsbetrag beim Zahlungsabgleich) -- ohne Vorbelegung
// bliebe es leer und der Schritt waere fachlich nicht entscheidbar. Auch
// Schreibfelder starten mit dem bisherigen Wert, damit eine Uebernahme aus einem
// Vor- oder Elternprozess nur bestaetigt statt abgetippt werden muss.
/**
 * Ist die Ablehnung die Aufforderung zum Aufsichtseingriff?
 * Erkennt die 422-Antwort des Kerns, die eine Begruendung verlangt.
 * @param {{status?: number, detail?: unknown}} err abgefangener Request-Fehler
 * @returns {boolean}
 */
function isSupervisionRequired(err) {
  return !!err && err.status === 422 && typeof err.detail === "string" && err.detail.startsWith("Aufsichtseingriff");
}

/**
 * Fragt die Pflichtbegruendung eines Aufsichtseingriffs ab. Der Dialog erklaert,
 * warum (kein Bearbeiter hinter dem Login) und dass die Angabe im Audit steht.
 *
 * Er warnt ausserdem **vorher**, wenn spaetere Schritte ihre Bearbeiter relativ
 * zu diesem hier bestimmen (Vier-Augen-Prinzip): Ein Aufsichtseingriff
 * hinterlaesst keinen Ausfuehrer, an dem eine solche Regel ansetzen koennte --
 * der Folgeschritt landet dann in keiner Arbeitsliste. Die Warnung kommt
 * deshalb vorher, nicht erst, wenn es zu spaet ist.
 *
 * @param {string} label Bezeichnung des Schritts
 * @param {(reason: string) => Promise<unknown>} onConfirm sendet den Abschluss mit Begruendung
 * @param {object} [schema] Schema der Instanz (fuer die Folgewirkungs-Warnung)
 * @param {string} [nodeId] Knoten-ID des Schritts
 */
function askSupervisionReason(label, onConfirm, schema, nodeId) {
  const reason = el("textarea", { rows: "3", placeholder: "z. B. Bearbeiterin krank, Frist l\u00E4uft ab" });
  const dependants = Object.entries(((schema || {}).staff_rules) || {})
    .filter(([nid, rule]) => nid !== nodeId && ruleRefersToPerformerOf(rule, nodeId))
    .map(([nid]) => {
      const n = (schema.nodes || {})[nid];
      return n && n.label ? n.label : nid;
    });
  const body = el("div", { class: "form-grid" },
    el("div", { class: "card-hint" },
      "Dieser Login ist keinem Bearbeiter zugeordnet. Der Schritt wird an der Bearbeiterregel vorbei abgeschlossen (Aufsichtseingriff). Die Begr\u00FCndung wird im Audit-Verlauf festgehalten."),
    dependants.length
      ? el("div", { class: "warn-banner" },
          "\u26A0 Achtung: " + (dependants.length === 1 ? "Der Schritt " : "Die Schritte ")
          + dependants.map((n) => "\u201E" + n + "\u201C").join(", ")
          + (dependants.length === 1 ? " bestimmt" : " bestimmen")
          + " den Bearbeiter aus der Person, die diesen Schritt erledigt. Nach einem Aufsichtseingriff gibt es diese Person nicht \u2013 dort ist dann niemand zust\u00E4ndig, und auch das geht nur per Aufsicht weiter.")
      : null,
    el("label", { class: "field" }, "Begr\u00FCndung *", reason));
  openModal(`Aufsichtseingriff: ${label}`, body, async () => {
    const text = reason.value.trim();
    if (!text) { toast("err", "Bitte eine Begr\u00FCndung angeben"); return false; }
    return onConfirm(text);
  }, "Trotzdem abschlie\u00DFen");
}

/**
 * Anordnung einer Eingabemaske: Gruppen mit Ueberschrift, 1-3 Spalten.
 *
 * EINE Funktion fuer die Vorschau im Designer und die echte Aufgabenmaske,
 * damit beide gleich aussehen. Gruppen erscheinen in der Reihenfolge ihres
 * ersten Felds; Felder ohne Gruppe stehen ohne Ueberschrift an ihrer Stelle.
 * Am Smartphone ist die Maske immer einspaltig (CSS ``.mask-cols``).
 *
 * @param {{group?: string, node: HTMLElement}[]} items Felder in Maskenreihenfolge
 * @param {number} columns Spaltenzahl (1-3)
 * @returns {HTMLElement}
 */
function maskLayout(items, columns) {
  const wrap = el("div", { class: "mask-layout" });
  const cols = Math.min(3, Math.max(1, Number(columns) || 1));
  const sections = [];
  const byGroup = {};
  items.forEach((it) => {
    const key = (it.group || "").trim();
    if (!(key in byGroup)) { byGroup[key] = []; sections.push(key); }
    byGroup[key].push(it.node);
  });
  sections.forEach((key) => {
    if (key) wrap.appendChild(el("div", { class: "mask-group" }, key));
    wrap.appendChild(el("div", { class: "mask-cols", style: `grid-template-columns: repeat(${cols}, minmax(0, 1fr))` },
      ...byGroup[key]));
  });
  return wrap;
}

async function promptComplete(schema, instanceId, nodeId, label, agentId, onDone, dataValues) {
  // Bevorzugt die gestaltete Eingabemaske dieses Schritts; sonst generische
  // Felder fuer die Pflicht-Schreibvariablen.
  const form = (schema.forms || {})[nodeId];
  const values = dataValues || {};
  const inputs = {};
  const body = el("div", { class: "form-grid" });
  if (form) {
    if (form.title) body.appendChild(el("div", { class: "mask-title" }, form.title));
    body.appendChild(maskLayout(form.fields.map((f) => {
      const elem = schema.data_elements[f.element_id];
      const writable = f.mode === "WRITE" || f.mode === "READ_WRITE";
      const { control, read } = maskControl(elem, f.widget, f.options, values[f.element_id]);
      // Pruefregeln auch als HTML-Attribute (Tastatur, Browserhinweis).
      if (f.min_value != null) control.setAttribute("min", String(f.min_value));
      if (f.max_value != null) control.setAttribute("max", String(f.max_value));
      if (f.max_length != null) control.setAttribute("maxlength", String(f.max_length));
      const wrap = el("label", { class: "field" },
        f.label + (showsRequiredMark(f) && writable ? " *" : ""), control,
        f.help_text ? el("span", { class: "field-help" }, f.help_text) : null);
      if (!writable) {
        control.setAttribute("disabled", "disabled");
        // Nur-Lese-Zahlen wie ueberall anzeigen (Betrag „1.500,00“): ein
        // Zahlenfeld zeigte den Rohwert „1500“.
        const v = values[f.element_id];
        if (elem && isNumericType(elem.data_type) && typeof v === "number" && "value" in control) {
          control.setAttribute("type", "text");
          control.value = formatValue(elem, v);
        }
      } else inputs[f.element_id] = { read, elem, label: f.label, required: f.required !== false, field: f, wrap };
      return { group: f.group, node: wrap };
    }), form.columns || 1));
  } else {
    // Was der Schritt nur liest, steht als Nur-Lese-Zeile darueber: Wer
    // „Freigabe pruefen“ ohne Maske erledigte, sah den Betrag nicht, den er
    // freigab. Lesen-und-Schreiben erscheint unten als vorbelegtes Feld.
    const readOnly = readOnlyValues(schema, nodeId, values);
    if (readOnly) body.appendChild(readOnly);
    const writes = (schema.data_accesses || []).filter((a) => a.node_id === nodeId && (a.mode === "WRITE" || a.mode === "READ_WRITE"));
    writes.forEach((a) => {
      const elem = schema.data_elements[a.element_id];
      const { control, read } = maskControl(elem, fallbackWidget(elem), null, values[a.element_id]);
      const wrap = el("label", { class: "field" }, (elem ? elem.name : a.element_id) + ` (${elem ? typeName(elem.data_type) : "?"})`, control);
      inputs[a.element_id] = { read, elem, label: elem ? elem.name : a.element_id, required: true, wrap };
      body.appendChild(wrap);
    });
  }
  const doComplete = async () => {
    const data = {};
    // Ein leer gelassenes Pflichtfeld wird hier abgefangen statt vom Kern: der
    // fehlende Wert faellt sonst erst spaeter auf -- an einer XOR-Verzweigung
    // oder einer Folgeprozess-Bedingung, die ihn braucht -- und zwar als
    // Laufzeitfehler an einem *anderen* Schritt. Das Modell erklaert die
    // Pflicht bereits (FormField.required), also halten wir sie auch ein.
    const missing = [];
    const invalid = [];
    for (const [eid, { read, label: fieldLabel, required, field, wrap }] of Object.entries(inputs)) {
      const val = read();
      if (val === undefined) {
        // Pflichtfehler am Feld zeigen, nicht nur in der Meldung.
        markField(wrap, required ? "Pflichtfeld" : null);
        if (required) missing.push(fieldLabel);
        continue;
      }
      const problem = field ? fieldRuleProblem(field, val) : null;
      markField(wrap, problem);
      if (problem) { invalid.push(`${fieldLabel}: ${problem}`); continue; }
      // Kein Umwandeln hier: ``read()`` hat schon typisiert, was eindeutig ist
      // (``coerceTypedInput``); der Rest geht roh an den Kern und kommt als
      // D3-Meldung zurueck.
      data[eid] = val;
    }
    if (missing.length) {
      toast("err", "Bitte alle Pflichtfelder ausfüllen", missing);
      return false;
    }
    if (invalid.length) {
      toast("err", "Bitte die markierten Eingaben pr\u00FCfen", invalid);
      return false;
    }
    return submitCompletion(data, null);
  };
  // Sendet den Abschluss. Der Kern entscheidet, ob ein Aufsichtseingriff
  // vorliegt (Login ohne Agentenbindung an einem Schritt mit Bearbeiterregel) und
  // verlangt dann eine Begruendung -- der Client kennt diese Regel nicht, er
  // reagiert nur auf die Ablehnung und fragt nach (keine Korrektheitslogik hier).
  const submitCompletion = async (data, supervisionReason) => {
    try {
      const payload = { node_id: nodeId, data };
      if (agentId) payload.agent_id = agentId;
      if (supervisionReason) payload.supervision_reason = supervisionReason;
      await api.post(`/instances/${instanceId}/complete`, payload);
      toast("ok", "Schritt abgeschlossen");
      if (onDone) await onDone();
    } catch (err) {
      if (isSupervisionRequired(err)) { askSupervisionReason(label, (reason) => submitCompletion(data, reason), schema, nodeId); return false; }
      toastError(err); return false;
    }
  };
  // Ohne Maske und ohne Werte schloss ein einziger Klick den Schritt ab --
  // unumkehrbar und leicht versehentlich. Jetzt fragt ein kurzer Dialog nach;
  // der Fokus liegt auf „Abschliessen“, Enter bestaetigt.
  if (!form && !body.children.length) {
    body.appendChild(el("p", null,
      `\u201E${label}\u201C ist erledigt? Der Schritt hat keine Eingaben; abgeschlossen geht es mit dem n\u00E4chsten Schritt weiter.`));
  }
  openModal(`Abschlie\u00DFen: ${label}`, body, doComplete, "Abschlie\u00DFen");
}

/**
 * Nur-Lese-Anzeige der Werte, die ein Schritt ohne Maske liest.
 *
 * Ein Schritt mit gestalteter Maske zeigt Lesefelder selbst (deaktiviert); ohne
 * Maske gab es bisher nur die Schreibfelder -- eine Freigabe geschah blind.
 * Reine Anzeige, keine Logik: welche Werte ein Schritt liest, sagt die
 * Datenbindung des Modells.
 * @param {object} schema wirksames Schema der Instanz
 * @param {string} nodeId der abzuschliessende Schritt
 * @param {Record<string, *>} values aktuelle Vorgangsdaten
 * @returns {HTMLElement|null} Liste „Name: Wert“ oder null ohne Lesezugriffe
 */
function readOnlyValues(schema, nodeId, values) {
  const reads = (schema.data_accesses || []).filter((a) => a.node_id === nodeId && a.mode === "READ");
  if (!reads.length) return null;
  return el("dl", { class: "read-values" },
    ...reads.flatMap((a) => {
      const elem = (schema.data_elements || {})[a.element_id];
      return [
        el("dt", null, elem ? elem.name : a.element_id),
        el("dd", null, formatValue(elem, values[a.element_id])),
      ];
    }));
}

