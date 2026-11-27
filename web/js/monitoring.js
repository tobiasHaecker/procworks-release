// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Monitoring.
 *
 * Die Monitoring-Sicht: Konformitaet, Engpaesse, unbesetzte Schritte und
 * Stoerungen.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// View: Monitoring
// --------------------------------------------------------------------------

/**
 * Dauer fuer die Engpass-Tabelle: „keine Zeitdaten“ statt eines Strichs, und
 * „< 1 s“ statt „0 s“ -- ein Strich sah aus wie ein fehlendes Feature.
 * @param {number|null|undefined} sec Sekunden
 * @returns {string}
 */
function fmtStepDuration(sec) {
  if (sec == null) return "keine Zeitdaten";
  if (sec < 1) return "< 1 s";
  return fmtDuration(sec);
}

/**
 * Panel „Prozesskarte (Soll/Ist)“ im Monitoring.
 *
 * Zeigt fuer ein gewaehltes Schema das Soll-Modell mit der beobachteten
 * Haeufigkeit und Dauer je Schritt (``renderGraph`` mit ``observed``) und
 * listet, was davon abweicht: Uebergaenge, die das Modell nicht vorsieht, und
 * Schritte, die im Modell fehlen (Ad-hoc). Die Rechnung liegt im Kern
 * (``GET /schemas/{id}/conformance``); der Client zeigt nur an. Die rohe
 * Directly-follows-Tabelle bleibt einklappbar erhalten.
 *
 * @param {object[]} instances die geladenen Instanzen (fuer die Schema-Auswahl)
 * @param {object|null} pmap entdeckte Prozesskarte (Directly-follows) oder null
 * @param {*} [listError] Fehler beim Laden der Instanzliste: Ohne Liste ist
 *   unbekannt, ob es Ablaeufe gibt -- das Panel zeigt dann den Fehlerzustand
 *   statt „Noch keine Ablaeufe“.
 * @returns {Promise<HTMLElement>}
 */
async function conformancePanel(instances, pmap, listError = null) {
  const withRuns = [...new Set(instances.map((i) => i.schema_id))];
  if (!state.conformanceSchema || !withRuns.includes(state.conformanceSchema)) {
    state.conformanceSchema = withRuns.includes(state.schemaId) ? state.schemaId : withRuns[0] || null;
  }
  const picker = el("select", { class: "conf-picker", onChange: (e) => { state.conformanceSchema = e.target.value; render(); } },
    ...withRuns.map((sid) => {
      const o = el("option", { value: sid }, schemaLabel(sid));
      if (sid === state.conformanceSchema) o.selected = true;
      return o;
    }));
  const body = el("div", { class: "panel-b" });
  const panel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Prozesskarte (Soll/Ist)"),
      el("span", { class: "sub" }, "Process Mining über dem Modell"), withRuns.length ? picker : null),
    body);
  if (listError) { body.appendChild(loadErrorState(listError, "Die Prozesskarte konnte nicht geladen werden")); return panel; }
  if (!state.conformanceSchema) { body.appendChild(emptyState("Noch keine Abläufe. Starte eine Instanz und schließe Schritte ab.")); return panel; }

  let report = null, schema = null;
  try {
    [report, schema] = await Promise.all([
      api.get(`/schemas/${state.conformanceSchema}/conformance`),
      api.get(`/schemas/${state.conformanceSchema}`),
    ]);
  } catch (err) { body.appendChild(loadErrorState(err, "Der Soll/Ist-Vergleich konnte nicht geladen werden")); return panel; }

  const observed = {};
  report.steps.forEach((st) => { observed[st.node_id] = st; });
  // fitOnShow: die Karte wird losgeloest gebaut und erst danach angehaengt --
  // ohne Einpassen lag das Modell beim ersten Oeffnen ausserhalb des Bildes.
  body.appendChild(renderGraph(schema, { observed, fitOnShow: true }));
  const never = report.steps.filter((st) => !st.completed);
  const facts = el("div", { class: "conf-facts" },
    el("span", null, `${countLabel(report.instances, "Instanz", "Instanzen")} ausgewertet`),
    el("span", null, never.length ? `${countLabel(never.length, "Schritt", "Schritte")} nie ausgeführt` : "jeder Schritt wurde ausgeführt"),
    el("span", { class: report.deviations.length ? "conf-warn" : "" },
      report.deviations.length ? `${countLabel(report.deviations.length, "Abweichung", "Abweichungen")} vom Modell` : "keine Abweichung vom Modell"));
  body.appendChild(facts);
  if (report.deviations.length) {
    const foreign = new Set(report.foreign_steps);
    const stepName = (id, label) => (label || id) + (foreign.has(id) ? " (nicht im Modell)" : "");
    body.appendChild(table(["Von", "Nach", "Häufigkeit"], report.deviations.map((d) => [
      stepName(d.source, d.source_label), stepName(d.target, d.target_label), String(d.frequency)])));
    body.appendChild(el("p", { class: "muted", style: "font-size:12px;margin-top:6px" },
      "Übergänge, die das Modell nicht erzeugen kann – meist Ad-hoc-Änderungen einzelner Instanzen oder Verläufe einer früheren Version."));
  }
  // Rohdaten (alle Schemata) bleiben erreichbar, aber eingeklappt.
  const edges = (pmap && pmap.edges) || [];
  if (edges.length) {
    const names = {};
    ((pmap && pmap.nodes) || []).forEach((n) => { names[n.node_id] = n.label || n.node_id; });
    body.appendChild(el("details", { class: "conf-raw" },
      el("summary", null, "Alle beobachteten Übergänge (Directly-follows, alle Prozesse)"),
      table(["Von", "Nach", "Häufigkeit"], edges.map((e) => [names[e.source] || e.source, names[e.target] || e.target, String(e.frequency)]))));
  }
  return panel;
}

/**
 * Filterstufen der Instanzliste im Monitoring (Wert = InstanceState, "all"
 * oder "UNSTAFFED" = laufend, aber ein offener Schritt hat niemanden).
 */
const INSTANCE_FILTERS = [
  { key: "all", label: "Alle" },
  { key: "RUNNING", label: "Laufend" },
  { key: "COMPLETED", label: "Abgeschlossen" },
  { key: "UNSTAFFED", label: "Niemand zust\u00E4ndig" },
  { key: "ABSENT_ONLY", label: "Nur Abwesende zust\u00E4ndig" },
];

/** Grund aus ``GET /monitoring/unstaffed`` als Satzteil. */
const UNSTAFFED_REASONS = {
  no_rule: "keine Bearbeiterzuordnung",
  nobody: "Regel findet aktuell niemanden",
  no_login: "kein Login",
  only_absent: "abwesend ohne Vertretung",
};

/**
 * Grund einer Zeile aus ``GET /monitoring/unstaffed`` als Text – bei
 * „kein Login“ und „abwesend ohne Vertretung“ mit den betroffenen Personen
 * (Namen aus dem Personenverzeichnis), damit die Aufsicht weiss, wen es angeht.
 *
 * Nennt der Kern Vertretungen, die das Vier-Augen-Prinzip ausschliesst
 * (``excluded_deputies``: sie haben den zu pruefenden Schritt selbst
 * erledigt), sagt der Text das statt „ohne Vertretung“ -- sonst truege die
 * Aufsicht eine weitere Vertretung ein oder hielte die Regel fuer defekt.
 *
 * @param {{reason: string, agent_ids?: string[],
 *   excluded_deputies?: {agent_id: string, stands_in_for: string, step_label: string}[]}} u
 *   die Zeile
 * @returns {string} z. B. „kein Login: Petra Pruef“ oder „abwesend: Erika
 *   Sander – Vertretung Tom Berger ist nach dem Vier-Augen-Prinzip
 *   ausgeschlossen (hat „Erfassen“ erledigt)“
 */
function unstaffedReasonText(u) {
  const excluded = u.reason === "only_absent" ? (u.excluded_deputies || []) : [];
  if (excluded.length) {
    const absent = (u.agent_ids || []).map(agentNameOf).join(", ");
    // Eine Vertretung kann fuer mehrere Abwesende eingetragen sein: einmal nennen.
    const seen = new Set();
    const parts = excluded.filter((d) => !seen.has(d.agent_id) && seen.add(d.agent_id))
      .map((d) => `Vertretung ${agentNameOf(d.agent_id)} ist nach dem Vier-Augen-Prinzip ausgeschlossen (hat „${d.step_label}“ erledigt)`);
    return `abwesend: ${absent} – ${parts.join("; ")}`;
  }
  const base = UNSTAFFED_REASONS[u.reason] || u.reason;
  const names = (u.agent_ids || []).map(agentNameOf);
  return names.length ? `${base}: ${names.join(", ")}` : base;
}

/**
 * Teilt den Bericht in die zwei Kategorien des Monitorings.
 *
 * „Niemand zust\u00E4ndig“ (``no_rule``, ``nobody``, ``no_login``): der Vorgang
 * steht still, bis jemand zugeordnet wird oder eine Aufsicht abschliesst.
 * „Nur Abwesende zust\u00E4ndig“ (``only_absent``): schwaecher – die Aufgabe
 * bleibt bei der Person und laeuft weiter, sobald sie zurueck ist.
 *
 * @param {object[]} unstaffed Zeilen aus ``GET /monitoring/unstaffed``
 * @returns {{stalled: object[], absentOnly: object[]}}
 */
function splitUnstaffed(unstaffed) {
  const rows = unstaffed || [];
  return {
    stalled: rows.filter((u) => u.reason !== "only_absent"),
    absentOnly: rows.filter((u) => u.reason === "only_absent"),
  };
}

/**
 * Zaehlkacheln des Monitorings: aus dem KPI-Bericht, nicht aus der Liste.
 *
 * Die Anleitung sagt zu: Kennzahlen ueber **alle** Vorgaenge, einzeln nur die,
 * an denen ein Bearbeiter beteiligt ist. Die Liste (``GET /instances``) ist fuer
 * einen solchen Login gefiltert und enthaelt dazu Test-Instanzen, die nicht ins
 * Monitoring zaehlen; der Bericht (``GET /monitoring/kpis``) zaehlt alle echten
 * Vorgaenge. Frueher kamen „Instanzen gesamt“ aus der Liste, die
 * Durchlaufzeit und die Engpaesse aus dem Bericht -- widerspruechlich ohne
 * Hinweis. Die Einschraenkung kennt der Client nicht; er erkennt sie daran,
 * dass die Liste weniger echte Vorgaenge zeigt als der Bericht zaehlt.
 *
 * @param {object[]} instances geladene Vorgaenge der Liste
 * @param {?{total_instances: number, running: number, completed: number}} report
 *   KPI-Bericht; ``null``, wenn er nicht geladen werden konnte
 * @param {boolean} [listLoaded=true] ``false``, wenn das Laden der Liste
 *   scheiterte -- eine leere oder kurze Liste ist dann keine Einschraenkung,
 *   und der Hinweis darauf waere irrefuehrend
 * @returns {{total: number, running: number, done: number, listIsPartial: boolean}}
 *   ``listIsPartial``: Die Liste (und alles, was aus ihr gezaehlt wird) zeigt
 *   nur einen Teil der Vorgaenge. Ohne Bericht zaehlt die Liste ohne
 *   Test-Instanzen, ``listIsPartial`` ist dann ``false`` (nicht feststellbar).
 */
function monitorCounts(instances, report, listLoaded = true) {
  const real = instances.filter((i) => !i.is_test);
  if (!report) {
    return {
      total: real.length,
      running: real.filter((i) => i.state === "RUNNING").length,
      done: real.filter((i) => i.state === "COMPLETED").length,
      listIsPartial: false,
    };
  }
  return {
    total: report.total_instances,
    running: report.running,
    done: report.completed,
    listIsPartial: listLoaded && real.length < report.total_instances,
  };
}

/**
 * Panel „Niemand zustaendig“ bzw. „Nur Abwesende zustaendig“ des Monitorings:
 * je offenem Schritt eine klickbare Zeile (oeffnet den Vorgang) mit dem Grund
 * aus ``unstaffedReasonText``. Die Rechnung, wer zustaendig ist, liegt im Kern
 * (``GET /monitoring/unstaffed``); hier wird nur angezeigt.
 *
 * @param {object[]} rows Eintraege aus ``splitUnstaffed`` (``instance_id``,
 *   ``schema_id``, ``schema_version``, ``label``/``node_id``)
 * @param {string} title Ueberschrift des Panels
 * @param {string} sub Erlaeuterung neben der Ueberschrift
 * @param {Object<string, object>} instancesById geladene Vorgaenge nach Id --
 *   liefert die Startzeit fuer den Vorgangsnamen; fehlt ein Vorgang (nicht in
 *   der eigenen Liste), bleibt nur die Id bzw. der Titel
 * @param {Object<string, object>} titles benennende Werte je Vorgang
 *   (``GET /instance-titles``), darf leer sein
 * @returns {HTMLElement} das Panel; der Aufrufer zeigt es nur bei ``rows.length``
 */
function monitorUnstaffedPanel(rows, title, sub, instancesById, titles) {
  const cells = rows.map((u) => [
    instanceNameCell(u.instance_id, (instancesById[u.instance_id] || {}).started_at, titles[u.instance_id]),
    schemaLabel(u.schema_id, u.schema_version),
    u.label || u.node_id,
    unstaffedReasonText(u)]);
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, title), el("span", { class: "sub" }, sub)),
    el("div", { class: "panel-b" },
      table(["Instanz", "Schema", "Schritt", "Grund"], cells,
        (i) => ({ class: "clickable", onClick: () => openInstanceFromMonitor(rows[i].instance_id) }))));
}

/**
 * Panel „Engpaesse – Aktivitaeten“ des Monitorings.
 *
 * Engpass-Analyse: je Schritt Liegezeit (bereit -> uebernommen), Bearbeitung
 * (uebernommen -> erledigt) und Gesamtdauer (bereit -> erledigt). Die
 * Gesamtdauer gibt es auch, wenn ohne Uebernahme erledigt wurde -- vorher stand
 * dann ueberall „–“. Sortiert nach Gesamtdauer: der Engpass steht oben;
 * Schritte ohne Gesamtdauer stehen am Ende.
 *
 * @param {?{activity_stats: object[]}} report KPI-Bericht
 *   (``GET /monitoring/kpis``); ``null``, wenn er nicht geladen werden konnte
 * @param {*} [reportError] Fehler beim Laden des Berichts: dann der
 *   Fehlerzustand statt „Noch keine abgeschlossenen Aktivitaeten“.
 * @returns {HTMLElement} das Panel; ohne Kennzahlen mit einem Leerhinweis
 *   statt der Tabelle
 */
function monitorBottleneckPanel(report, reportError = null) {
  const stats = ((report && report.activity_stats) || []).slice()
    .sort((a, b) => (b.avg_total_seconds ?? -1) - (a.avg_total_seconds ?? -1));
  const statRows = stats.map((s) => [s.label || s.node_id, String(s.completed),
    fmtStepDuration(s.avg_total_seconds), fmtStepDuration(s.avg_wait_seconds), fmtStepDuration(s.avg_duration_seconds)]);
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Engp\u00E4sse \u2013 Aktivit\u00E4ten"),
      el("span", { class: "sub" }, "\u00D8 Dauer von \u201Ebereit\u201C bis \u201Eerledigt\u201C \u2013 l\u00E4ngste zuerst")),
    el("div", { class: "panel-b" }, statRows.length
      ? el("div", null,
          table(["Aktivit\u00E4t", "Abschl\u00FCsse", "\u00D8 gesamt", "\u00D8 Liegezeit", "\u00D8 Bearbeitung"], statRows),
          el("p", { class: "muted", style: "font-size:12px;margin-top:8px" },
            "Liegezeit und Bearbeitung gibt es nur für Aufgaben, die vor dem Erledigen übernommen wurden. „keine Zeitdaten“: Abschlüsse aus der Zeit vor dieser Messung."))
      : reportError ? loadErrorState(reportError, "Die Kennzahlen konnten nicht geladen werden")
      : emptyState("Noch keine abgeschlossenen Aktivit\u00E4ten erfasst.")));
}

/**
 * Panel „Inzidente (externe Aufgaben)“ des Monitorings: laedt die offenen
 * Inzidente und zeigt sie als Tabelle.
 *
 * Sichtbar fuer alle Monitoring-Leser; "Erneut versuchen" (Aufloesen +
 * Wiedereinreihen) ist nur fuer Bearbeiter/Administratoren freigeschaltet
 * (tasks:complete). Scheitert das Laden, steht der Fehlerzustand im Panel --
 * nie „Keine offenen Inzidente“, denn der Bestand ist dann unbekannt.
 *
 * @returns {Promise<HTMLElement>} das Panel; ohne Inzidente mit einem
 *   Hinweis „Keine offenen Inzidente“
 */
async function monitorIncidentsPanel() {
  const loaded = await loadPart(() => api.get("/v1/incidents?unresolved_only=true"), []);
  const incidents = loaded.value;
  const canResolve = hasRole("operator", "admin");
  const incBody = el("div", { class: "panel-b" });
  if (!loaded.ok) {
    incBody.appendChild(loadErrorState(loaded.error, "Die Inzidente konnten nicht geladen werden"));
  } else if (!incidents.length) {
    incBody.appendChild(el("div", { class: "ok-banner" }, "\u2713 Keine offenen Inzidente externer Aufgaben."));
  } else {
    const incRows = incidents.map((inc) => {
      const action = canResolve
        ? el("button", { class: "btn small green", onClick: () => resolveIncident(inc) }, "Erneut versuchen")
        : el("span", { class: "muted" }, "\u2013");
      return [inc.node_id, inc.message, fmtTimestamp(new Date(inc.created_at * 1000).toISOString()), action];
    });
    incBody.appendChild(table(["Schritt", "Fehler", "Zeit", ""], incRows));
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Inzidente (externe Aufgaben)"),
      el("span", { class: "sub" }, "Topic-Fehler \u00B7 Aufl\u00F6sen reiht die Aufgabe erneut ein")),
    incBody);
}

async function viewMonitor() {
  // Beim Neuzeichnen bleibt der alte Inhalt bis zum fertigen Laden stehen
  // (beginViewContent in core.js); ein Sichtwechsel beginnt leer.
  const content = beginViewContent();
  let instances = [];
  let listLoaded = false;
  // Nicht geladen ist nicht leer: ohne Liste steht in der Instanztabelle ein
  // Fehlerzustand statt „Keine Instanzen“ (``loadErrorState``).
  let listError = null;
  // Alle lesbaren Vorgaenge in einer Anfrage (``GET /monitoring/instances``).
  // Frueher kam erst die ID-Liste und dann jeder Vorgang einzeln -- eine
  // Anfrage je Vorgang, bei einigen hundert Vorgaengen minutenlang.
  try {
    instances = await api.get("/monitoring/instances");
    listLoaded = true;
    await ensureSchemaNames(instances.map((i) => i.schema_id));
  } catch (err) { if (!listLoaded) listError = err; toastError(err); }

  // KPI-Report + Prozesskarte aus dem Audit-Log (Schritt 15). Jeder Teil
  // merkt seinen Fehler (``loadPart``): Was nicht geladen ist, zeigt „–“ bzw.
  // den Fehlerzustand, nie „0“ oder „keine …“.
  const reportPart = await loadPart(() => api.get("/monitoring/kpis"), null);
  const report = reportPart.value;
  let pmap = null;
  try { pmap = await api.get("/monitoring/process-map"); } catch (e) { /* nur Kanten der Karte */ }

  // Offene Schritte, die niemand bearbeiten darf -- der Vorgang steht
  // still, zeigte aber „ueberfaellig 0, eskaliert 0“. Die Rechnung liegt im Kern
  // (GET /monitoring/unstaffed); hier nur Kachel, Liste und Filter.
  const unstaffedPart = await loadPart(() => api.get("/monitoring/unstaffed"), []);
  const unstaffed = unstaffedPart.value;
  // Benennende Werte je Vorgang; fehlen sie, bleibt die ID.
  let titles = {};
  try { titles = await api.get("/instance-titles"); } catch (e) { /* best-effort */ }
  const { stalled, absentOnly } = splitUnstaffed(unstaffed);
  const instancesById = Object.fromEntries(instances.map((i) => [i.id, i]));
  const unstaffedIds = new Set(stalled.map((u) => u.instance_id));
  const absentOnlyIds = new Set(absentOnly.map((u) => u.instance_id));

  // Z4: \u00DCberf\u00E4llig-Zusammenfassung \u00FCber alle
  // laufenden Vorg\u00E4nge, in einer Anfrage (``GET /monitoring/tasks``: die
  // offenen Aufgaben aller lesbaren laufenden Vorgaenge, ohne Pruefinstanzen).
  // Frueher fragte das Monitoring jeden laufenden Vorgang einzeln -- und jede
  // dieser Anfragen glich vorher alle Vorgaenge auf Eskalationen ab.
  // Dieselbe Sammlung tr\u00E4gt die Eskalations-Sicht (T3/E9 Stufe C): welche
  // Aufgaben haben bereits Stufen gefeuert.
  // Eine Aufgabenliste, die nicht kam, macht die Summe unbekannt (nicht 0).
  const tasksPart = await loadPart(() => api.get("/monitoring/tasks"), []);
  const tasksLoaded = tasksPart.ok;
  const allTasks = tasksPart.value;
  const overdueTasks = allTasks.filter((t) => t.time_criticality === "OVERDUE").length;
  const escalatedTasks = allTasks.filter((t) => (t.escalated_stage || 0) > 0);

  const counts = monitorCounts(instances, report, listLoaded);
  // Anzahl, Laufend, Abgeschlossen kommen aus dem Bericht oder -- ohne ihn --
  // aus der Liste; fehlen beide, sind sie unbekannt.
  const countsLoaded = reportPart.ok || listLoaded;
  const kpis = el("div", { class: "kpis" },
    kpi("Instanzen gesamt", loadedValue(countsLoaded, counts.total)),
    kpi("Laufend", loadedValue(countsLoaded, counts.running)),
    kpi("Abgeschlossen", loadedValue(countsLoaded, counts.done)),
    kpi("\u00DCberf\u00E4llige Aufgaben", loadedValue(tasksLoaded, overdueTasks)),
    kpi("Eskalierte Aufgaben", loadedValue(tasksLoaded, escalatedTasks.length)),
    kpi("Niemand zust\u00E4ndig", loadedValue(unstaffedPart.ok, unstaffedIds.size)),
    kpi("Nur Abwesende zust\u00E4ndig", loadedValue(unstaffedPart.ok, absentOnlyIds.size)),
    kpi("\u00D8 Durchlaufzeit", report ? fmtDuration(report.avg_cycle_seconds) : "\u2013"));
  content.appendChild(kpis);
  if (counts.listIsPartial) {
    content.appendChild(el("div", { class: "muted monitor-scope-note" },
      "Anzahl, Durchlaufzeit und Engp\u00E4sse z\u00E4hlen alle Vorg\u00E4nge. "
      + "\u00DCberf\u00E4llige und eskalierte Aufgaben, die Zust\u00E4ndigkeits-Kacheln und die Liste "
      + "zeigen nur Vorg\u00E4nge, an denen du beteiligt bist."));
  }

  // Eskalations-Sicht (Stufe C): jede Aufgabe mit
  // gefeuerten Stufen, klickbar zur Instanz. Nur sichtbar, wenn es etwas zu
  // zeigen gibt \u2013 ein Betrieb ohne Eskalationen bekommt kein leeres Panel.
  if (escalatedTasks.length) {
    const escRows = escalatedTasks.map((t) => [
      instanceNameCell(t.instance_id, t.instance_started_at, t.context),
      t.label || t.node_id,
      `Stufe ${t.escalated_stage}`,
      criticalityBadge(t) || "\u2013",
      t.claimed_by || "\u2013"]);
    content.appendChild(el("div", { class: "panel" },
      el("div", { class: "panel-h" }, el("h2", null, "Eskalationen"),
        el("span", { class: "sub" }, "Aufgaben mit gefeuerten Eskalationsstufen")),
      el("div", { class: "panel-b" },
        table(["Instanz", "Schritt", "Eskalation", "Kritikalit\u00E4t", "Inhaber"], escRows,
          (i) => ({ class: "clickable", onClick: () => openInstanceFromMonitor(escalatedTasks[i].instance_id) })))));
  }

  // „Niemand zustaendig“ / „Nur Abwesende zustaendig“: nur sichtbar, wenn
  // es etwas zu zeigen gibt.
  if (stalled.length) {
    content.appendChild(monitorUnstaffedPanel(stalled, "Niemand zust\u00E4ndig",
      "Diese Vorg\u00E4nge stehen still, bis jemand zugeordnet wird, einen Login bekommt oder per Aufsicht abschlie\u00DFt",
      instancesById, titles));
  }
  if (absentOnly.length) {
    content.appendChild(monitorUnstaffedPanel(absentOnly, "Nur Abwesende zust\u00E4ndig",
      "Die Aufgabe bleibt bei der Person und l\u00E4uft weiter, sobald sie zur\u00FCck ist \u2013 oder eine Vertretung eintragen",
      instancesById, titles));
  }

  // Filter der Instanzliste: die Liste hiess
  // „Aktive Instanzen“, zeigte aber auch abgeschlossene. Rein clientseitig,
  // transient (state.monitorFilter), Standard "alle".
  const filter = INSTANCE_FILTERS.some((f) => f.key === state.monitorFilter) ? state.monitorFilter : "all";
  const matches = (i, key) => key === "all"
    || (key === "UNSTAFFED" ? unstaffedIds.has(i.id)
      : key === "ABSENT_ONLY" ? absentOnlyIds.has(i.id) : i.state === key);
  // Pruefinstanzen zaehlen nicht ins Monitoring (wie in den Kacheln,
  // ``monitorCounts``): Liste und Filterzaehler zeigen nur echte Vorgaenge,
  // sonst stand „Alle (25)“ neben „Instanzen gesamt 24“ und eine Pruefinstanz
  // ungekennzeichnet als „läuft“. Ein Hinweis nennt, wie viele ausgeblendet sind.
  const listed = instances.filter((i) => !i.is_test);
  const hiddenTests = instances.length - listed.length;
  const shown = listed.filter((i) => matches(i, filter));
  const filterBar = el("div", { class: "seg-filter", role: "group", "aria-label": "Instanzen filtern" },
    ...INSTANCE_FILTERS.map((f) => {
      // Ohne Liste (bzw. ohne Zustaendigkeits-Abruf fuer die beiden
      // Zustaendigkeits-Filter) ist die Anzahl unbekannt: „(–)“ statt „(0)“.
      const known = listLoaded && (unstaffedPart.ok || (f.key !== "UNSTAFFED" && f.key !== "ABSENT_ONLY"));
      const n = loadedValue(known, listed.filter((i) => matches(i, f.key)).length);
      // ``data-focus-key``: Die Beschriftung traegt die Anzahl und aendert
      // sich mit jedem neuen Vorgang; der Fokus findet den Knopf ueber die
      // feste Kennung wieder (restoreViewState in core.js).
      return el("button", {
        class: "seg-btn" + (f.key === filter ? " active" : ""),
        "data-focus-key": "monitor-filter:" + f.key,
        "aria-pressed": f.key === filter ? "true" : "false",
        onClick: () => { state.monitorFilter = f.key; render(); },
      }, `${f.label} (${n})`);
    }));
  const rows = shown.map((i) => {
    const total = Object.keys(i.node_states || {}).length || 1;
    const completed = Object.values(i.node_states || {}).filter((s) => s === "COMPLETED" || s === "SKIPPED").length;
    const pct = Math.round((completed / total) * 100);
    const idCell = instanceNameCell(i.id, i.started_at, titles[i.id]);
    return { i, cells: [idCell, schemaLabel(i.schema_id, i.schema_version), statePillFor(i.state), `${pct}%`] };
  });

  // Leere Liste: ein Hinweis statt einer Tabelle mit einer Leerzeile.
  const tbl = rows.length
    ? table(["Instanz", "Schema", "Status", "Fortschritt"], rows.map((r) => r.cells),
        (i) => ({ class: rows[i].i.id === state.instanceId ? "clickable selected" : "clickable",
          onClick: () => openInstanceFromMonitor(rows[i].i.id) }))
    : listError ? loadErrorState(listError)
    : emptyState(listed.length
        ? "Keine Instanzen in diesem Filter."
        : "Keine Instanzen. Starte eine in der Ausf\u00FChrungs-Sicht.");

  content.appendChild(el("div", { class: "panel", "data-tour": "monitor.instances" },
    el("div", { class: "panel-h" }, el("h2", null, "Instanzen"), el("span", { class: "sub" }, "Klick \u00F6ffnet Detail"), filterBar),
    el("div", { class: "panel-b" }, tbl,
      hiddenTests > 0
        ? el("div", { class: "muted monitor-test-note", style: "font-size:12px;margin-top:6px" },
            `${countLabel(hiddenTests, "Pr\u00FCfinstanz", "Pr\u00FCfinstanzen")} ausgeblendet \u2013 sie z\u00E4hlen nicht ins Monitoring (Sicht \u201EPr\u00FCfinstanz\u201C).`)
        : null)));

  // Detail der ausgewaehlten Instanz inkl. Live-Prozesslandkarte -- direkt unter
  // der Liste der aktiven Instanzen, damit der Bezug sofort sichtbar ist.
  // Die ausgewaehlte Instanz frisch laden: ``state.instance`` stammt sonst aus
  // einer frueheren Sicht und zeigte beim Oeffnen ihren Startzustand, bis man
  // die Zeile anklickte. Die Revisions-Abfrage rendert bei neuem
  // Fortschritt ohnehin neu -- damit bleibt das Detail aktuell. Ist sie nicht
  // mehr lesbar (geloescht, fremd), faellt die Auswahl weg.
  if (state.instanceId) {
    // Nur 404/403 nimmt den Vorgang aus der Ansicht; bei einem Ausfall bleibt
    // er gewaehlt und steht mit dem letzten Stand unter dem Ausfall-Banner.
    try { await loadInstance(state.instanceId); }
    catch (e) { dropUnreadableInstance(e); }
  }
  if (state.instance) {
    const detail = el("div");
    // Schema der Instanz laden, damit der Graph passt
    let schemaError = null;
    if (state.instance.schema_id !== state.schemaId) {
      try { state.schema = await api.get(`/schemas/${state.instance.schema_id}`); } catch (e) { schemaError = e; }
    }
    // Ohne passendes Schema (Ausfall beim Nachladen) kein Detail mit einem
    // fremden Graphen, sondern der Hinweis; der Vorgang bleibt gewaehlt.
    const schemaFits = state.instance.ad_hoc_schema
      || (state.schema && state.schema.id === state.instance.schema_id);
    if (schemaFits) await renderInstanceDetail(detail, true);
    else {
      detail.appendChild(el("div", { class: "panel" }, el("div", { class: "panel-b" },
        loadErrorState(schemaError || unreachableError(0), "Der gew\u00E4hlte Vorgang konnte nicht geladen werden"))));
    }
    content.appendChild(detail);
  }

  // Engpass-Analyse je Schritt, der Engpass steht oben.
  content.appendChild(monitorBottleneckPanel(report, reportPart.error));

  // Prozesskarte Soll/Ist: das Beobachtete ueber dem Soll-Modell (dieselbe
  // Zeichnung wie ueberall), dazu die Abweichungen. Frueher nur eine Tabelle
  // „von / nach / Haeufigkeit“ ohne Bezug zum Modell.
  content.appendChild(await conformancePanel(instances, pmap, listError));

  // Inzidente externer Aufgaben.
  content.appendChild(await monitorIncidentsPanel());
}

