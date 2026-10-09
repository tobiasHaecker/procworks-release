// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Integration.
 *
 * Die Integrationssicht: Connectoren, Datenbindungen (auch SQL),
 * Automationen und Webhooks.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// View: Integration (Roadmap P5)
// --------------------------------------------------------------------------
//
// Wie der ganze Client traegt diese Sicht KEINE Korrektheitslogik: sie ruft
// nur gepruefte Endpunkte (Connectoren, Datenanbindung, Automatik, Webhooks).
// Validitaet ist eine Eigenschaft des Serverzustands; ungueltige Eingaben
// weist der Kern mit 422 ab und wir zeigen den Befund als Toast.

async function viewIntegration() {
  const content = byId("content");
  clear(content);
  // Connectoren + Webhooks parallel laden (unabhaengige Endpunkte).
  const [connPanel, hookPanel] = await Promise.all([
    connectorRegistryPanel(),
    webhookPanel(),
  ]);
  content.appendChild(connPanel);          // 11.1
  content.appendChild(dataBindingPanel()); // 11.2
  content.appendChild(automationPanel());  // 11.3
  content.appendChild(hookPanel);          // 11.4
}

// --- 11.1 Connector-Registry ----------------------------------------------

async function connectorRegistryPanel() {
  let connectors = [];
  try { connectors = await api.get("/v1/connectors"); }
  catch (err) { toastError(err); }
  const rows = connectors.map((c) => {
    const st = state.connectorStatus[c.connector_id] || "unknown";
    const pill = el("span", { class: "pill " + (st === "ok" ? "pill-green" : st === "err" ? "pill-red" : "pill-gray") },
      st === "ok" ? "verbunden" : st === "err" ? "Fehler" : "ungepr\u00FCft");
    const testBtn = el("button", { class: "btn small", onClick: () => testConnector(c.connector_id) }, "Verbindung testen");
    const readBtn = el("button", { class: "btn small ghost", onClick: () => sampleReadConnector(c.connector_id) }, "Testlesen");
    return [c.connector_id, el("span", { class: "pill pill-blue" }, c.kind), pill,
      el("div", { class: "row-actions" }, testBtn, readBtn)];
  });
  const body = el("div", { class: "panel-b" },
    connectors.length
      ? table(["Connector", "Typ", "Status", ""], rows)
      : emptyState("Keine Connectoren konfiguriert. Connectoren werden serverseitig (admin-only) eingerichtet \u2013 Zugangsdaten bleiben dort, niemals im Modell."),
    el("p", { class: "muted", style: "margin-top:10px" },
      "Zugangsdaten werden nie angezeigt oder abgefragt. Die Modellierung referenziert nur eine serverseitig aufgel\u00F6ste Secret-Referenz."));
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Connector-Registry"), el("span", { class: "sub" }, "Externe Datensysteme \u00B7 nur Metadaten")),
    body);
}

async function testConnector(id) {
  try {
    const res = await api.post(`/v1/connectors/${id}/test`);
    state.connectorStatus[id] = res.ok ? "ok" : "err";
    toast(res.ok ? "ok" : "err", res.ok ? "Verbindung ok" : "Verbindung fehlgeschlagen", [id]);
  } catch (err) {
    state.connectorStatus[id] = "err";
    toastError(err);
  }
  render();
}

/**
 * Fuellt eine Datalist mit den Entitaeten (Tabellen/Sichten) eines Connectors.
 *
 * Der Katalog kommt aus ``GET /v1/connectors/{id}/entities`` (reine Metadaten,
 * keine Zeile wird gelesen). Schlaegt der Abruf fehl oder kennt der Connector
 * keinen Katalog, bleibt das zugehoerige Feld ein gewoehnliches Freitextfeld --
 * die Vorschlagsliste ist eine Hilfe, nie eine Voraussetzung.
 *
 * @param {HTMLElement} datalistEl die zu fuellende <datalist>
 * @param {string} connectorId Connector, dessen Katalog gelesen wird
 * @returns {Promise<string[]>} die angebotenen Namen (leer, wenn keine)
 */
async function fillEntitySuggestions(datalistEl, connectorId) {
  clear(datalistEl);
  if (!connectorId) return [];
  let names = [];
  try { names = await api.get(`/v1/connectors/${connectorId}/entities`); }
  catch (e) { return []; }  // best effort: ohne Katalog bleibt das Freitextfeld
  names.forEach((n) => datalistEl.appendChild(el("option", { value: n })));
  return names;
}

/**
 * Haengt an ein Entitaets-Feld die Tabellenliste seines Connectors.
 *
 * Gibt die zu ergaenzende <datalist> zurueck (sie muss im Dialog haengen, damit
 * der Browser sie findet). Wechselt die Connector-Auswahl, wird der Katalog neu
 * geladen. Bietet der Connector genau eine Tabelle an, wird sie in ein noch
 * leeres Feld eingetragen -- der haeufigste Fall, und niemand muss den Namen
 * raten.
 *
 * @param {HTMLInputElement} entityInput das Freitextfeld der Entitaet
 * @param {string} listId eindeutige Id der Datalist (pro Dialog eine)
 * @param {() => string} getConnectorId liefert die aktuelle Connector-Id
 * @param {HTMLSelectElement} [connSelect] Auswahlfeld, das den Katalog wechselt
 * @returns {HTMLElement} die <datalist>
 */
function wireEntitySuggestions(entityInput, listId, getConnectorId, connSelect) {
  const list = el("datalist", { id: listId });
  entityInput.setAttribute("list", listId);
  const reload = () => fillEntitySuggestions(list, getConnectorId()).then((names) => {
    if (names.length === 1 && !entityInput.value) entityInput.value = names[0];
  });
  if (connSelect) connSelect.addEventListener("change", reload);
  reload();
  return list;
}

/**
 * Testlesen eines Connectors: Entitaet waehlen, Ergebnis IM Dialog anzeigen.
 *
 * Das Ergebnis bleibt bewusst im selben Dialog. Vorher oeffnete der Rueckruf
 * ein zweites Fenster im selben Modal-Container, das ``openModal`` unmittelbar
 * danach wieder leerte (der Rueckruf gab nicht ``false`` zurueck) -- man sah
 * deshalb nie einen Datensatz, obwohl der Server sie lieferte.
 * Mit ``return false`` bleibt der Dialog stehen, und man kann eine andere
 * Tabelle lesen, ohne ihn neu zu oeffnen.
 *
 * @param {string} id Connector-Id aus der Registry
 */
function sampleReadConnector(id) {
  const entity = el("input", { type: "text", placeholder: "z. B. Kunde" });
  const list = wireEntitySuggestions(entity, "pw-sample-entities", () => id);
  const limit = el("input", { type: "number", value: "5", min: "1", max: "100" });
  const out = el("div", { class: "sample-out" });
  openModal("Testlesen \u2013 " + id, el("div", { class: "form-grid" },
    el("label", { class: "field wide" }, "Entit\u00E4t/Tabelle", entity, list,
      el("span", { class: "field-help" }, "Tabellen des Connectors werden vorgeschlagen, sobald der Katalog gelesen ist.")),
    el("label", { class: "field" }, "Anzahl", limit),
    out), async () => {
    if (!entity.value.trim()) { toast("info", "Bitte eine Entit\u00E4t/Tabelle angeben"); return false; }
    try {
      const rows = await api.post(`/v1/connectors/${id}/sample-read`,
        { entity: entity.value.trim(), limit: Number(limit.value) || 1 });
      renderSampleRecords(out, entity.value.trim(), rows);
    } catch (err) { toastError(err); }
    return false;  // Ergebnis steht im Dialog -- er darf sich nicht schliessen
  }, "Lesen");
}

/**
 * Zeichnet die gelesenen Beispieldatensaetze als Tabelle in einen Bereich.
 *
 * Spalten sind die Vereinigung aller Schluessel (ein Datensatz kann ein Feld
 * ausgelassen haben), Werte werden als Text dargestellt; ``null`` erscheint als
 * Gedankenstrich statt als "null".
 *
 * @param {HTMLElement} target Zielbereich (wird geleert)
 * @param {string} entity gelesene Entitaet, fuer die Ueberschrift
 * @param {object[]} rows Datensaetze der API
 */
function renderSampleRecords(target, entity, rows) {
  clear(target);
  target.appendChild(el("div", { class: "sub-h" },
    el("h3", null, `Beispieldatens\u00E4tze: ${entity} (${rows.length})`)));
  if (!rows.length) {
    target.appendChild(emptyState("Keine Datens\u00E4tze gefunden."));
    return;
  }
  const cols = [];
  rows.forEach((r) => Object.keys(r || {}).forEach((k) => { if (!cols.includes(k)) cols.push(k); }));
  target.appendChild(table(cols, rows.map((r) =>
    cols.map((c) => (r[c] === null || r[c] === undefined ? "\u2013" : String(r[c]))))));
}

// --- 11.2 Datenanbindungs-Assistent ---------------------------------------

function elemName(schema, id) {
  const e = schema.data_elements[id];
  return e ? e.name : id;
}

function dataBindingPanel() {
  const head = el("div", { class: "panel-h" },
    el("h2", null, "Datenanbindung (extern)"),
    el("span", { class: "sub" }, "Datenelemente an Connectoren binden (C1\u2013C6)"));
  if (!state.schema) {
    return el("div", { class: "panel" }, head,
      el("div", { class: "panel-b" }, emptyState("Kein Schema ausgew\u00E4hlt \u2013 oben ein Schema w\u00E4hlen.")));
  }
  const schema = state.schema;
  const draft = isDraft(schema);

  // Modell-Connectoren (Metadaten im Schema, getrennt von der Laufzeit-Registry).
  const connRows = Object.values(schema.connectors || {}).map((c) =>
    [c.name, el("span", { class: "pill pill-blue" }, c.kind), c.id]);
  const addConnBtn = el("button", { class: "btn small", onClick: registerSchemaConnector, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Connector");
  const connBlock = el("div", null,
    el("div", { class: "sub-h" }, el("h3", null, "Modell-Connectoren"), el("span", { class: "spacer", style: "flex:1" }), addConnBtn),
    connRows.length ? table(["Name", "Typ", "ID"], connRows)
      : emptyState("Noch keine Connectoren im Modell registriert."));

  // Datenelemente + externe Bindung.
  const hasConn = Object.keys(schema.connectors || {}).length > 0;
  const elemRows = Object.values(schema.data_elements).map((d) => {
    const src = d.source === "EXTERNAL"
      ? el("span", { class: "pill pill-amber" }, "EXTERN")
      : el("span", { class: "pill pill-gray" }, "Instanz");
    let detail = "\u2013";
    if (d.external) {
      detail = `${d.external.connector_id} \u00B7 ${d.external.entity} \u00B7 Schl\u00FCssel ${elemName(schema, d.external.key_element_id)}`;
    } else if (d.select) {
      const proj = d.select.aggregate && d.select.aggregate !== "NONE"
        ? `${d.select.aggregate}(${d.select.column})` : d.select.column;
      detail = `${d.select.connector_id} \u00B7 SELECT ${proj} FROM ${d.select.entity}`;
    } else if (d.write) {
      detail = `${d.write.connector_id} \u00B7 UPDATE ${d.write.entity} SET ${d.write.column}`;
    }
    const connLock = lockedBy([!draft, DRAFT_ONLY_REASON],
      [!hasConn, "Erst oben unter „Connectoren“ einen Connector anlegen."]);
    const bindBtn = el("button", { class: "btn small ghost", onClick: () => bindExternalElement(d), ...connLock }, "Datensatz");
    const sqlBtn = el("button", { class: "btn small ghost", onClick: () => bindSqlSelect(d), ...connLock }, "SQL-Select");
    const writeBtn = el("button", { class: "btn small ghost", onClick: () => bindSqlWrite(d), ...connLock }, "SQL-Write");
    return [d.name, typeName(d.data_type), src, detail, el("div", { class: "row-actions" }, bindBtn, sqlBtn, writeBtn)];
  });
  const elemBlock = el("div", null,
    el("div", { class: "sub-h" }, el("h3", null, "Datenelemente")),
    elemRows.length ? table(["Element", "Typ", "Quelle", "Abbildung", ""], elemRows)
      : emptyState("Noch keine Datenelemente. Lege sie in der Datensicht an."));

  return el("div", { class: "panel" }, head,
    el("div", { class: "panel-b" }, connBlock, el("div", { style: "height:14px" }), elemBlock,
      el("p", { class: "muted", style: "margin-top:10px" },
        "Lese-/Schreib-Richtung (D/C) wird \u00FCber die Bindungen in der Datensicht festgelegt.")));
}

function registerSchemaConnector() {
  const name = el("input", { type: "text", placeholder: "z. B. ERP-Kunden" });
  const kind = el("select", null, ...CONNECTOR_KINDS.map((k) => el("option", { value: k }, k)));
  const cid = el("input", { type: "text", placeholder: "optionale ID, z. B. erp" });
  openModal("Connector registrieren", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field" }, "Typ", kind),
    el("label", { class: "field" }, "ID (optional)", cid)), async () => {
    if (!name.value.trim()) return false;
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/connectors`,
      { name: name.value.trim(), kind: kind.value, connector_id: cid.value.trim() || null }), "Connector registriert");
  }, "Registrieren");
}

function bindExternalElement(element) {
  const schema = state.schema;
  const conn = el("select", null, ...Object.values(schema.connectors || {}).map((c) =>
    el("option", { value: c.id }, `${c.name} (${c.id})`)));
  const entity = el("input", { type: "text", placeholder: "z. B. Kunde" });
  const entityList = wireEntitySuggestions(entity, "pw-ext-entities", () => conn.value, conn);
  const keyElems = Object.values(schema.data_elements).filter((d) => d.source !== "EXTERNAL" && d.id !== element.id);
  const key = el("select", null, ...keyElems.map((d) => el("option", { value: d.id }, d.name)));
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Connector", conn),
    el("label", { class: "field wide" }, "Entit\u00E4t/Tabelle", entity, entityList),
    el("label", { class: "field" }, "Schl\u00FCssel-Datenelement", keyElems.length
      ? key
      : el("span", { class: "muted" }, "Erst ein Instanz-Datenelement anlegen.")));
  openModal(`Extern anbinden \u2013 ${element.name}`, body, async () => {
    if (!entity.value.trim() || !conn.value || !keyElems.length) return false;
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${element.id}/external`,
      { connector_id: conn.value, entity: entity.value.trim(), key_element_id: key.value }), "Datenelement extern angebunden");
  }, "Anbinden");
}

// --- 11.2b SQL-Select-Assistent (C4-C6) -----------------------------------

function sqlQuoteIdent(name) { return '"' + name + '"'; }
function sqlOpText(op) {
  return { EQ: "=", NE: "<>", LT: "<", LE: "<=", GT: ">", GE: ">=", LIKE: "LIKE", IN: "IN" }[op];
}
function sqlResultType(aggregate, columnType) {
  return aggregate === "COUNT" ? "INTEGER" : aggregate === "AVG" ? "FLOAT" : columnType;
}
// Client-side mirror of compile_select for the live preview (the server stays
// the sole authority; a wrong binding is rejected with 422 + C4-C6 findings).
function sqlSelectPreview(s) {
  const proj = s.aggregate === "NONE" ? sqlQuoteIdent(s.column) : `${s.aggregate}(${sqlQuoteIdent(s.column)})`;
  let sql = `SELECT ${proj} FROM ${s.entity.split(".").map(sqlQuoteIdent).join(".")}`;
  const where = s.filters.map((f, i) => `${sqlQuoteIdent(f.column)} ${sqlOpText(f.operator)} :f${i}`);
  if (where.length) sql += " WHERE " + where.join(" AND ");
  if (s.cardinality === "FIRST_ORDERED" && s.order_by.length) {
    sql += " ORDER BY " + s.order_by.map((o) => sqlQuoteIdent(o.column) + (o.descending ? " DESC" : "")).join(", ") + " LIMIT 1";
  }
  return sql;
}

/**
 * Gemeinsamer Unterbau der beiden SQL-Assistenten (Select und Write).
 *
 * Beide Dialoge haben dieselben Grundfelder (Connector, Entitaet mit
 * Vorschlaegen, Spalte samt Datalist, Spaltentyp, eindeutige Spalte), dieselbe
 * Filterliste (WHERE) und dasselbe Nachladen der Live-Spalten. Was sie
 * unterscheidet -- Zusatzfelder, Vorschau-Text, Hinweise und Endpunkt --, bleibt
 * in ``bindSqlSelect`` bzw. ``bindSqlWrite``. Rein anzeigend: Ob die Bindung
 * korrekt ist, entscheidet allein der Kern beim Absenden.
 *
 * @param {object} element das anzubindende Datenelement (``id``, ``data_type``)
 * @param {string} listPrefix Praefix der Datalist-Ids, pro Dialog eindeutig
 *   (``"pw-sql"`` ergibt ``pw-sql-entities`` und ``pw-sql-cols``)
 * @param {string} columnPlaceholder Platzhalter des Spaltenfelds
 * @param {function(): void} refresh Vorschau und Hinweise des Dialogs
 *   neu berechnen. Wird nach jeder Eingabe in die Grundfelder, nach dem Laden
 *   der Spalten und nach jeder Filteraenderung gerufen -- nie waehrend des
 *   Aufbaus, darf also auf Felder zugreifen, die der Aufrufer erst danach anlegt.
 * @returns {{conn: HTMLSelectElement, entity: HTMLInputElement,
 *   colInput: HTMLInputElement, colType: HTMLSelectElement,
 *   uniqueCol: HTMLInputElement, preview: HTMLElement, typeHint: HTMLElement,
 *   cardHint: HTMLElement, filterSpecs: function(): object[],
 *   renderFilters: function(): void, connectorField: HTMLElement,
 *   entityField: HTMLElement, filterRows: HTMLElement[],
 *   previewRows: HTMLElement[]}}
 *   ``filterSpecs`` liefert nur vollstaendige Filterzeilen (Spalte und Quelle
 *   gesetzt) im API-Format. ``filterRows``/``previewRows`` sind die
 *   Formularzeilen fuer Filter bzw. Vorschau in Anzeigereihenfolge (``el``
 *   flacht die Listen beim Einhaengen ab).
 */
function sqlDialogParts(element, listPrefix, columnPlaceholder, refresh) {
  const schema = state.schema;
  const conns = Object.values(schema.connectors || {});
  const instanceElems = Object.values(schema.data_elements).filter((d) => d.source !== "EXTERNAL" && d.id !== element.id);
  const sourceType = (id) => { const e = schema.data_elements[id]; return e ? e.data_type : null; };
  const colListId = `${listPrefix}-cols`;

  const conn = el("select", null, ...conns.map((c) => el("option", { value: c.id }, `${c.name} (${c.id})`)));
  const entity = el("input", { type: "text", placeholder: "z. B. Kunde" });
  const entityList = wireEntitySuggestions(entity, `${listPrefix}-entities`, () => conn.value, conn);
  const colInput = el("input", { type: "text", placeholder: columnPlaceholder, list: colListId });
  const colDatalist = el("datalist", { id: colListId });
  const colType = el("select", null, ...DATA_TYPES.map((t) => el("option", { value: t }, typeName(t))));
  colType.value = element.data_type;
  const uniqueCol = el("input", { type: "text", placeholder: "z. B. kd_id" });
  const preview = el("pre", { class: "code-block" });
  const typeHint = el("div", { class: "sub" });
  const cardHint = el("div", { class: "sub" });
  const filtersBox = el("div", null);
  let columns = null;
  const filters = [];

  function applyColType() {
    if (!columns) return;
    const hit = columns.find((c) => c.column === colInput.value.trim());
    if (hit && hit.data_type) colType.value = hit.data_type;
  }
  async function loadColumns() {
    if (!conn.value || !entity.value.trim()) { toast("info", "Erst Connector und Entität wählen"); return; }
    try {
      columns = await api.get(`/v1/connectors/${conn.value}/columns?entity=${encodeURIComponent(entity.value.trim())}`);
      clear(colDatalist);
      columns.forEach((c) => colDatalist.appendChild(el("option", { value: c.column }, `${c.sql_type} → ${typeName(c.data_type)}`)));
      applyColType(); refresh();
      toast("ok", `${columns.length} Spalten geladen`);
    } catch (err) { columns = null; const d = describeError(err); toast("info", "Keine Live-Spalten – Namen/Typ manuell", d.lines); }
  }
  function filterSpecs() {
    return filters.filter((f) => f.column.trim() && f.source)
      .map((f) => ({ column: f.column.trim(), column_type: sourceType(f.source), operator: f.operator, key_element_id: f.source }));
  }

  function buildFilterRow(f) {
    const col = el("input", { type: "text", placeholder: "DB-Spalte", list: colListId, value: f.column });
    col.addEventListener("input", () => { f.column = col.value; refresh(); });
    const opSel = el("select", null, ...SQL_OPERATORS.map(([v, l]) => el("option", { value: v }, l)));
    opSel.value = f.operator;
    opSel.addEventListener("change", () => { f.operator = opSel.value; refresh(); });
    const srcSel = el("select", null, ...instanceElems.map((d) => el("option", { value: d.id }, `${d.name} (${typeName(d.data_type)})`)));
    srcSel.value = f.source;
    srcSel.addEventListener("change", () => { f.source = srcSel.value; refresh(); });
    const rm = el("button", { class: "btn small danger", type: "button", onClick: () => { const i = filters.indexOf(f); if (i >= 0) filters.splice(i, 1); renderFilters(); refresh(); } }, "×");
    return el("div", { class: "check-row" }, col, opSel, srcSel, rm);
  }
  function renderFilters() { clear(filtersBox); filters.forEach((f) => filtersBox.appendChild(buildFilterRow(f))); }
  function addFilter() {
    filters.push({ column: "", operator: "EQ", source: instanceElems.length ? instanceElems[0].id : "" });
    renderFilters(); refresh();
  }

  conn.addEventListener("change", refresh);
  entity.addEventListener("input", refresh);
  colInput.addEventListener("input", () => { applyColType(); refresh(); });
  colType.addEventListener("change", refresh);
  uniqueCol.addEventListener("input", refresh);

  return {
    conn, entity, colInput, colType, uniqueCol, preview, typeHint, cardHint,
    filterSpecs, renderFilters,
    connectorField: el("label", { class: "field" }, "Connector", conns.length ? conn : el("span", { class: "muted" }, "Erst einen Connector registrieren.")),
    entityField: el("label", { class: "field wide" }, "Entität/Tabelle", el("div", { class: "check-row" }, entity, el("button", { class: "btn small", type: "button", onClick: loadColumns }, "Spalten laden")), entityList),
    filterRows: [
      el("div", { class: "sub-h" }, el("h3", null, "Filter (WHERE)"), el("span", { style: "flex:1" }), el("button", { class: "btn small", type: "button", onClick: addFilter }, "+ Filter")),
      filtersBox,
    ],
    previewRows: [
      el("div", { class: "sub-h" }, el("h3", null, "Vorschau")),
      typeHint, cardHint, preview, colDatalist,
    ],
  };
}

/**
 * Dialog „SQL-Select“: bindet ein Datenelement an das Ergebnis einer
 * Einzelwert-Abfrage (eine Spalte, optional aggregiert, mit Filtern und
 * Kardinalitaet).
 *
 * Grundfelder, Filter und Spaltenladen kommen aus ``sqlDialogParts``; hier
 * stehen nur Aggregat, Kardinalitaet (mit eindeutiger Spalte bzw. Sortierung),
 * die Vorschau ueber ``sqlSelectPreview`` und die Hinweise zu Ergebnistyp und
 * Kardinalitaet. Die Hinweise sind Lesehilfe -- verbindlich prueft der Kern
 * (422 mit Befunden).
 *
 * @param {object} element das anzubindende Datenelement
 */
function bindSqlSelect(element) {
  const { conn, entity, colInput, colType, uniqueCol, preview, typeHint, cardHint,
    filterSpecs, renderFilters, connectorField, entityField, filterRows, previewRows } =
    sqlDialogParts(element, "pw-sql", "z. B. name", refresh);
  const agg = el("select", null, ...SQL_AGGREGATES.map((a) => el("option", { value: a }, a === "NONE" ? "— kein —" : a)));
  const card = el("select", null, ...SQL_CARDINALITIES.map(([v, l]) => el("option", { value: v }, l)));
  const orderCol = el("input", { type: "text", placeholder: "Sortierspalte" });
  const orderDesc = el("input", { type: "checkbox" });

  function spec() {
    return {
      connector_id: conn.value,
      entity: entity.value.trim(),
      column: colInput.value.trim(),
      column_type: colType.value,
      aggregate: agg.value,
      filters: filterSpecs(),
      cardinality: card.value,
      order_by: (card.value === "FIRST_ORDERED" && orderCol.value.trim())
        ? [{ column: orderCol.value.trim(), descending: orderDesc.checked }] : [],
      unique_column: uniqueCol.value.trim(),
    };
  }
  function refresh() {
    const s = spec();
    preview.textContent = (s.column && s.entity) ? sqlSelectPreview(s) : "…";
    const rt = sqlResultType(s.aggregate, s.column_type);
    const ok4 = rt === element.data_type;
    typeHint.textContent = `Ergebnistyp ${typeName(rt)} ${ok4 ? "✓ passt zu" : "✗ passt nicht zu"} „${element.name}“ (${typeName(element.data_type)})`;
    typeHint.className = "sub " + (ok4 ? "ok-hint" : "bad-hint");
    let ok6 = true, msg = "";
    if (s.cardinality === "KEY_UNIQUE") {
      ok6 = !!s.unique_column && s.filters.some((f) => f.operator === "EQ" && f.column === s.unique_column);
      msg = ok6 ? "Höchstens eine Zeile (eindeutiger Schlüssel)" : "Gleichheitsfilter auf die eindeutige Spalte nötig";
    } else if (s.cardinality === "AGGREGATE") {
      ok6 = s.aggregate !== "NONE";
      msg = ok6 ? "Aggregat liefert genau eine Zeile" : "Aggregat wählen";
    } else {
      ok6 = s.order_by.length > 0;
      msg = ok6 ? "Erste Zeile nach Sortierung" : "Sortierspalte angeben";
    }
    cardHint.textContent = (ok6 ? "✓ " : "✗ ") + msg;
    cardHint.className = "sub " + (ok6 ? "ok-hint" : "bad-hint");
    uniqueRow.style.display = s.cardinality === "KEY_UNIQUE" ? "" : "none";
    orderRow.style.display = s.cardinality === "FIRST_ORDERED" ? "" : "none";
  }

  agg.addEventListener("change", refresh);
  card.addEventListener("change", refresh);
  orderCol.addEventListener("input", refresh);
  orderDesc.addEventListener("change", refresh);

  const uniqueRow = el("label", { class: "field" }, "Eindeutige Spalte (Schlüssel)", uniqueCol);
  const orderRow = el("label", { class: "field" }, "Sortierung",
    el("div", { class: "check-row" }, orderCol, el("label", { class: "check-inline" }, orderDesc, " absteigend")));
  const body = el("div", { class: "form-grid" },
    connectorField,
    entityField,
    el("label", { class: "field" }, "Ergebnis-Spalte", colInput),
    el("label", { class: "field" }, "Spaltentyp", colType),
    el("label", { class: "field" }, "Aggregat", agg),
    el("label", { class: "field" }, "Kardinalität", card),
    uniqueRow, orderRow,
    filterRows,
    previewRows);

  openModal(`SQL-Select – ${element.name}`, body, async () => {
    const s = spec();
    if (!s.connector_id || !s.entity || !s.column) { toast("info", "Connector, Entität und Ergebnis-Spalte angeben"); return false; }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${element.id}/sql-select`, s), "Datenelement per SQL-Select angebunden");
  }, "Anbinden");
  renderFilters(); refresh();
}

// --- 11.2c SQL-Write-Assistent (C7-C9) ------------------------------------

// Client-side mirror of compile_update for the live preview (server-authoritative).
function sqlUpdatePreview(s) {
  let sql = `UPDATE ${s.entity.split(".").map(sqlQuoteIdent).join(".")} SET ${sqlQuoteIdent(s.column)} = :val`;
  const where = s.filters.map((f, i) => `${sqlQuoteIdent(f.column)} ${sqlOpText(f.operator)} :f${i}`);
  if (where.length) sql += " WHERE " + where.join(" AND ");
  return sql;
}

/**
 * Dialog „SQL-Write“: schreibt den Wert eines Datenelements per UPDATE in eine
 * Spalte genau einer Zeile zurueck.
 *
 * Grundfelder, Filter und Spaltenladen kommen aus ``sqlDialogParts``; hier
 * stehen nur die Vorschau ueber ``sqlUpdatePreview`` und die Hinweise zu
 * Zielspaltentyp und Eindeutigkeit (Gleichheitsfilter auf die eindeutige
 * Spalte). Die Hinweise sind Lesehilfe -- verbindlich prueft der Kern
 * (422 mit Befunden).
 *
 * @param {object} element das anzubindende Datenelement
 */
function bindSqlWrite(element) {
  const { conn, entity, colInput, colType, uniqueCol, preview, typeHint, cardHint,
    filterSpecs, renderFilters, connectorField, entityField, filterRows, previewRows } =
    sqlDialogParts(element, "pw-sqlw", "z. B. status", refresh);

  function spec() {
    return {
      connector_id: conn.value,
      entity: entity.value.trim(),
      column: colInput.value.trim(),
      column_type: colType.value,
      filters: filterSpecs(),
      unique_column: uniqueCol.value.trim(),
    };
  }
  function refresh() {
    const s = spec();
    preview.textContent = (s.column && s.entity) ? sqlUpdatePreview(s) : "…";
    const ok7 = s.column_type === element.data_type;
    typeHint.textContent = `Zielspalte ${typeName(s.column_type)} ${ok7 ? "✓ passt zu" : "✗ passt nicht zu"} „${element.name}“ (${typeName(element.data_type)})`;
    typeHint.className = "sub " + (ok7 ? "ok-hint" : "bad-hint");
    const ok9 = !!s.unique_column && s.filters.some((f) => f.operator === "EQ" && f.column === s.unique_column);
    cardHint.textContent = (ok9 ? "✓ " : "✗ ") + (ok9 ? "Trifft genau eine Zeile (eindeutiger Schlüssel)" : "Gleichheitsfilter auf die eindeutige Spalte nötig");
    cardHint.className = "sub " + (ok9 ? "ok-hint" : "bad-hint");
  }

  const body = el("div", { class: "form-grid" },
    connectorField,
    entityField,
    el("label", { class: "field" }, "Ziel-Spalte", colInput),
    el("label", { class: "field" }, "Spaltentyp", colType),
    el("label", { class: "field" }, "Eindeutige Spalte (Schlüssel)", uniqueCol),
    filterRows,
    previewRows);

  openModal(`SQL-Write – ${element.name}`, body, async () => {
    const s = spec();
    if (!s.connector_id || !s.entity || !s.column) { toast("info", "Connector, Entität und Ziel-Spalte angeben"); return false; }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${element.id}/sql-write`, s), "Datenelement per SQL-Write angebunden");
  }, "Anbinden");
  renderFilters(); refresh();
}

// --- 11.3 Automatik-Schritt-Binding ---------------------------------------

function automationPanel() {
  const head = el("div", { class: "panel-h" },
    el("h2", null, "Automatik-Schritte"),
    el("span", { class: "sub" }, "Person / Automatisch (External-Task \u00B7 HTTP-Push)"));
  if (!state.schema) {
    return el("div", { class: "panel" }, head,
      el("div", { class: "panel-b" }, emptyState("Kein Schema ausgew\u00E4hlt.")));
  }
  const schema = state.schema;
  const draft = isDraft(schema);
  const acts = activitiesOf(schema);
  const rows = acts.map((n) => {
    const sb = (schema.service_bindings || {})[n.id];
    const auto = sb ? sb.automation : "MANUAL_NONE";
    const detail = auto === "EXTERNAL_TASK" ? `External-Task \u00B7 Topic ${sb.topic || "?"}`
      : auto === "HTTP_PUSH" ? `HTTP-Push \u00B7 ${sb.endpoint_ref || "?"}`
      : "Person / manuell";
    const pill = el("span", { class: "pill " + (auto === "MANUAL_NONE" ? "pill-gray" : "pill-green") },
      auto === "MANUAL_NONE" ? "manuell" : "automatisch");
    const btn = sb
      ? el("button", { class: "btn small", onClick: () => editAutomation(n, sb), ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "Bearbeitung w\u00E4hlen")
      : el("span", { class: "muted" }, "erst Dienst zuweisen");
    return [nodeCaption(n), pill, detail, btn];
  });
  const body = el("div", { class: "panel-b" },
    acts.length ? table(["Aktivit\u00E4t", "Modus", "Anbindung", ""], rows)
      : emptyState("Keine Aktivit\u00E4ten im Schema."));
  return el("div", { class: "panel" }, head, body);
}

function editAutomation(node, sb) {
  const kindSel = el("select", null,
    el("option", { value: "MANUAL_NONE" }, "Person / manuell"),
    el("option", { value: "EXTERNAL_TASK" }, "Automatisch \u00B7 External-Task (Topic)"),
    el("option", { value: "HTTP_PUSH" }, "Automatisch \u00B7 HTTP-Push (Ziel)"));
  kindSel.value = sb.automation || "MANUAL_NONE";
  const topic = el("input", { type: "text", placeholder: "z. B. invoice-check", value: sb.topic || "" });
  const endpoint = el("input", { type: "text", placeholder: "z. B. webhook_1", value: sb.endpoint_ref || "" });
  const retryMax = el("input", { type: "number", min: "0", value: String(sb.retry_max != null ? sb.retry_max : 5) });
  const backoff = el("input", { type: "number", min: "0", value: String(sb.retry_backoff_ms != null ? sb.retry_backoff_ms : 2000) });
  const timeout = el("input", { type: "number", min: "0", value: String(sb.request_timeout_ms != null ? sb.request_timeout_ms : 30000) });
  const topicField = el("label", { class: "field" }, "Topic", topic);
  const endpointField = el("label", { class: "field" }, "Endpunkt-Referenz", endpoint);
  const advanced = el("details", { class: "adv-block" }, el("summary", null, "Erweitert (Robustheit)"),
    el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Max. Versuche", retryMax),
      el("label", { class: "field" }, "Backoff (ms)", backoff),
      el("label", { class: "field" }, "Timeout (ms)", timeout)));
  const syncFields = () => {
    const k = kindSel.value;
    topicField.style.display = k === "EXTERNAL_TASK" ? "" : "none";
    endpointField.style.display = k === "HTTP_PUSH" ? "" : "none";
    advanced.style.display = k === "MANUAL_NONE" ? "none" : "";
  };
  kindSel.addEventListener("change", syncFields);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Bearbeitung", kindSel), topicField, endpointField, advanced);
  syncFields();
  openModal(`Automatik \u2013 ${nodeCaption(node)}`, body, async () => {
    const k = kindSel.value;
    const req = { node_id: node.id, automation: k };
    if (k === "EXTERNAL_TASK") { if (!topic.value.trim()) return false; req.topic = topic.value.trim(); }
    if (k === "HTTP_PUSH") { if (!endpoint.value.trim()) return false; req.endpoint_ref = endpoint.value.trim(); }
    if (k !== "MANUAL_NONE") {
      req.retry_max = Number(retryMax.value);
      req.retry_backoff_ms = Number(backoff.value);
      req.request_timeout_ms = Number(timeout.value);
    }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/automation`, req), "Automatik gesetzt");
  }, "\u00DCbernehmen");
}

// --- 11.4 Webhook-/Ereignis-Panel -----------------------------------------

async function webhookPanel() {
  let subs = [];
  try { subs = await api.get("/v1/webhooks"); }
  catch (err) { toastError(err); }
  const rows = subs.map((s) => {
    const events = (s.events || []).map(webhookEventLabel).join(", ");
    const test = el("button", { class: "btn small", onClick: () => testWebhook(s.id) }, "Testzustellung");
    const log = el("button", { class: "btn small ghost", onClick: () => showDeliveries(s.id) }, "Protokoll");
    const del = el("button", { class: "btn small danger", onClick: () => deleteWebhook(s) }, "L\u00F6schen");
    return [s.url, events, s.secret_ref || "\u2013", el("div", { class: "row-actions" }, test, log, del)];
  });
  const addBtn = el("button", { class: "btn small", onClick: addWebhook }, "+ Abonnement");
  const body = el("div", { class: "panel-b" },
    // In der oeffentlichen Demo ist jede ausgehende Verbindung gesperrt
    // (PROCWORKS_EGRESS_DENY). Ohne Hinweis wirkt das wie ein Defekt; der
    // Probelauf zeigt trotzdem, was gesendet wuerde und wie die SSRF-Regel urteilt.
    state.demoPassword
      ? el("div", { class: "warn-banner", style: "margin-bottom:10px" },
          "In der öffentlichen Demo sind ausgehende Verbindungen gesperrt – Zustellungen scheitern hier absichtlich. Beim Anlegen zeigt „Probelauf“, was gesendet würde und ob ein Ziel die Sicherheitsprüfung besteht (interne Adressen werden abgelehnt).")
      : null,
    subs.length ? table(["Ziel-URL", "Ereignisse", "Secret-Ref", ""], rows)
      : emptyState("Noch keine Webhook-Abonnements."));
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Webhooks / Ereignisse"), el("span", { class: "spacer", style: "flex:1" }), addBtn),
    body);
}

function addWebhook() {
  // Auflösbares Beispiel: „hooks.example.com" existiert nicht, der eigene
  // Vorschlag scheiterte deshalb an der Zielprüfung.
  const url = el("input", { type: "url", placeholder: "https://example.com/procworks" });
  const secret = el("input", { type: "text", placeholder: "z. B. WEBHOOK_SECRET (optional)" });
  const checks = WEBHOOK_EVENT_TYPES.map((ev) => {
    const cb = el("input", { type: "checkbox", value: ev });
    return { cb, row: el("label", { class: "check-row" }, cb, " " + webhookEventLabel(ev)) };
  });
  const result = el("div", { class: "wh-preview" });
  // Ein Probelauf gilt nur fuer die Eingaben, mit denen er lief. Aendert sich
  // URL, Secret oder Ereignis, verschwindet das Ergebnis -- sonst stand der
  // Befund zu „127.0.0.1“ noch unter einer laengst anderen URL. ``previewGen``
  // verwirft zudem eine Antwort, die erst nach einer Aenderung eintrifft.
  let previewGen = 0;
  const dropPreview = () => { previewGen++; clear(result); };
  url.addEventListener("input", dropPreview);
  secret.addEventListener("input", dropPreview);
  checks.forEach((c) => c.cb.addEventListener("change", dropPreview));
  const previewBtn = el("button", { class: "btn small", onClick: async () => {
    const chosen = checks.filter((c) => c.cb.checked).map((c) => c.cb.value);
    if (!url.value.trim()) { toast("info", "Bitte eine Ziel-URL angeben"); return; }
    const gen = ++previewGen;
    try {
      const p = await api.post("/v1/webhooks/preview",
        { url: url.value.trim(), event: chosen[0] || "task.completed", secret_ref: secret.value.trim() });
      if (gen === previewGen) renderWebhookPreview(result, p);
    } catch (err) { if (gen === previewGen) toastError(err); }
  } }, "Probelauf (sendet nichts)");
  // Jedes Feld in eigener Zeile: Die URL ist lang, und die Ereignisliste
  // drueckte im Dreier-Raster URL und Secret auf ein Drittel zusammen.
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field wide" }, "Ziel-URL", url),
    el("label", { class: "field wide" }, "Secret-Referenz (Servername, optional)", secret),
    el("div", { class: "field wide" }, el("span", null, "Ereignisse"),
      el("div", { class: "check-list" }, ...checks.map((c) => c.row))),
    el("div", null, previewBtn), result);
  openModal("Webhook-Abonnement", body, async () => {
    const events = checks.filter((c) => c.cb.checked).map((c) => c.cb.value);
    if (!url.value.trim() || !events.length) return false;
    try {
      await api.post("/v1/webhooks", { url: url.value.trim(), events, secret_ref: secret.value.trim() });
      render(); toast("ok", "Webhook angelegt");
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * Ergebnis eines Webhook-Probelaufs (``POST /v1/webhooks/preview``) anzeigen.
 *
 * Der Kern sendet dabei nichts. Gezeigt werden das Urteil der SSRF-Regel (mit
 * derselben Begruendung, die eine echte Zustellung haette), eine Egress-Sperre
 * getrennt davon, und Kopfzeilen samt signiertem Rumpf -- ein Empfaenger kann
 * die Signatur daran nachpruefen.
 *
 * @param {HTMLElement} box Zielbehaelter (wird geleert)
 * @param {object} p Antwort des Probelaufs
 */
function renderWebhookPreview(box, p) {
  clear(box);
  box.appendChild(p.allowed
    ? el("div", { class: "ok-banner" }, `\u2713 Ziel zulässig${p.resolved_address ? ` (Adresse ${p.resolved_address})` : ""}.`)
    : el("div", { class: "warn-banner" }, "Ziel abgelehnt: " + findingText(
        { code: p.reason_code, params: p.reason_params, message: p.reason }, { withHint: true })));
  if (p.egress_locked) {
    box.appendChild(el("div", { class: "warn-banner", style: "margin-top:6px" },
      "Auf dieser Instanz sind ausgehende Verbindungen gesperrt – gesendet würde trotzdem nichts."));
  }
  box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-top:8px" },
    p.signed
      ? "Signiert (X-ProcWorks-Signature, HMAC-SHA256 über den Rumpf)."
      : !p.secret_ref
        ? "Nicht signiert – es wurde keine Secret-Referenz angegeben."
        : `Nicht signiert – die Secret-Referenz „${p.secret_ref}“ ist auf diesem Server nicht hinterlegt. Das Secret selbst wird serverseitig gesetzt und verlässt ihn nie.`));
  const headerText = Object.entries(p.headers).map(([k, v]) => `${k}: ${v}`).join("\n");
  box.appendChild(el("pre", { class: "wh-preview-code" }, headerText + "\n\n" + p.body));
}

async function testWebhook(id) {
  try {
    const d = await api.post(`/v1/webhooks/${id}/test`);
    const detail = d.status_code != null ? "HTTP " + d.status_code : (d.error || "");
    toast(d.ok ? "ok" : "err", d.ok ? "Testzustellung erfolgreich" : "Testzustellung fehlgeschlagen", detail ? [detail] : []);
  } catch (err) { toastError(err); }
}

async function showDeliveries(id) {
  let deliveries = [];
  try { deliveries = await api.get(`/v1/webhooks/${id}/deliveries`); }
  catch (err) { toastError(err); return; }
  const rows = deliveries.map((d) => [
    fmtTimestamp(new Date(d.at * 1000).toISOString()),
    d.event_type, String(d.attempt),
    d.ok ? el("span", { class: "pill pill-green" }, "ok") : el("span", { class: "pill pill-red" }, "Fehler"),
    d.status_code != null ? "HTTP " + d.status_code : (d.error || "\u2013"),
  ]);
  const body = rows.length ? table(["Zeit", "Ereignis", "Versuch", "Status", "Detail"], rows)
    : emptyState("Noch keine Zustellungen.");
  openModal("Zustellprotokoll", body, async () => true, "Schlie\u00DFen");
}

function deleteWebhook(sub) {
  openModal("Webhook l\u00F6schen", el("p", { class: "muted" }, `Abonnement f\u00FCr ${sub.url} entfernen?`), async () => {
    try { await api.del(`/v1/webhooks/${sub.id}`); render(); toast("ok", "Webhook gel\u00F6scht"); }
    catch (err) { toastError(err); return false; }
  }, "L\u00F6schen", { danger: true });
}

