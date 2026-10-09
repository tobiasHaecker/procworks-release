// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Daten, Ressourcen, Lizenz.
 *
 * Die Datensicht, die Ressourcensicht und die Anzeige von Lizenz und
 * Bearbeiter-Zaehlung (unsichtbar, solange keine Lizenz aktiv ist).
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// View: Datensicht
// --------------------------------------------------------------------------

/**
 * Nimmt ein Datenelement in die benennenden Werte des Vorgangs auf oder
 * entfernt es. Geschrieben wird ueber den Kern
 * (``POST /schemas/{id}/display-fields``), der U5 prueft -- beim dritten
 * Element kommt dessen Meldung.
 * @param {string} elementId Datenelement
 */
async function toggleDisplayField(elementId) {
  const current = (state.schema.display_fields || []).slice();
  const next = current.includes(elementId)
    ? current.filter((e) => e !== elementId)
    : [...current, elementId];
  try {
    await api.post(`/schemas/${state.schemaId}/display-fields`, { element_ids: next });
    await refreshSchema(); render();
  } catch (err) { toastError(err); }
}

function viewData() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;
  const draft = isDraft(schema);

  // Datenelemente
  const elemRows = Object.values(schema.data_elements);
  const elemHeaders = draft ? ["Name", "Typ", "Quelle", "Aktionen"] : ["Name", "Typ", "Quelle"];
  const elemTable = elemRows.length
    ? table(elemHeaders, elemRows.map((d) => {
        const cells = [d.name, typeName(d.data_type), DATA_SOURCE_LABELS[d.source] || d.source];
        if (draft) {
          // Benennt den Vorgang in Listen; hoechstens zwei, nur
          // Vorgangsdaten -- das prueft der Kern (U5).
          const shown = (schema.display_fields || []).includes(d.id);
          const nameBtn = d.source === "INSTANCE"
            ? el("button", { class: "btn small" + (shown ? " primary" : " ghost"),
                title: "Der Wert benennt Vorgang und Aufgaben in Listen (höchstens zwei Datenelemente)",
                onClick: () => toggleDisplayField(d.id) }, shown ? "\u2713 benennt Vorgang" : "Benennt Vorgang")
            : null;
          const editBtn = el("button", { class: "btn small", onClick: () => editDataElement(d) }, "Bearbeiten");
          const resetBtn = d.source === "EXTERNAL"
            ? el("button", { class: "btn small", onClick: () => resetDataElementSource(d) }, "Quelle zur\u00FCcksetzen")
            : null;
          const delBtn = el("button", { class: "btn small danger", onClick: () => deleteDataElement(d) }, "L\u00F6schen");
          cells.push(el("div", { class: "row-actions" }, nameBtn, editBtn, resetBtn, delBtn));
        }
        return cells;
      }))
    : emptyState("Noch keine Datenelemente.");

  const addElemBtn = el("button", { class: "btn small", onClick: addDataElement, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Datenelement");
  const elemPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Datenelemente"), el("span", { class: "spacer", style: "flex:1" }), addElemBtn),
    el("div", { class: "panel-b" }, elemTable));

  // Datenzugriffe
  const accList = schema.data_accesses || [];
  const accRows = accList.map((a) => {
    const node = schema.nodes[a.node_id];
    const elem = schema.data_elements[a.element_id];
    return [node ? nodeCaption(node) : a.node_id, elem ? elem.name : a.element_id, accessModeLabel(a.mode), a.mandatory ? "Pflicht" : "optional"];
  });
  const accTable = accRows.length
    ? table(["Schritt", "Element", "Modus", "Bindung"], accRows,
        (i) => accList[i].node_id === state.dataFocusNode ? "hl-row" : "")
    : emptyState("Noch keine Datenbindungen.");
  const addAccBtn = el("button", { class: "btn small", onClick: addDataAccess, ...lockedBy([!draft, DRAFT_ONLY_REASON],
    [activitiesOf(schema).length === 0, "Erst einen Schritt modellieren."],
    [elemRows.length === 0, "Erst ein Datenelement anlegen."]) }, "+ Datenbindung");
  const accPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Lese-/Schreibbindungen"), el("span", { class: "sub" }, "D1-D4 live gepr\u00FCft"), el("span", { class: "spacer", style: "flex:1" }), addAccBtn),
    el("div", { class: "panel-b" }, accTable));

  const dataFocus = state.dataFocusNode && schema.nodes[state.dataFocusNode];
  const rightCol = el("div", null,
    returnBar(),
    dataFocus
      ? focusBanner("Hervorgehoben: Bindungen von \u201E" + nodeCaption(dataFocus) + "\u201C",
          () => { state.dataFocusNode = null; render(); })
      : null,
    accPanel, dFindingsPanel());

  content.appendChild(el("div", { class: "grid-2" }, elemPanel, rightCol));
  scrollHighlightIntoView();
}

function dFindingsPanel() {
  const v = state.validation;
  const dz = v && !v.correct ? v.findings.filter((f) => f.rule[0] === "D" || f.rule[0] === "C") : [];
  const body = el("div", { class: "panel-b" });
  if (!dz.length) body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Datenfluss konsistent (D/C)."));
  else dz.forEach((f) => body.appendChild(el("div", { class: "finding" }, el("span", { class: "rule" }, f.rule), el("span", null, findingText(f, { withHint: true })))));
  return el("div", { class: "panel" }, el("div", { class: "panel-h" }, el("h2", null, "Datenfluss-Befunde")), body);
}

/**
 * Legt ein Datenelement an.
 *
 * @param {function(): void} [onCreated] Optionaler Rueckweg nach dem Anlegen --
 *        der Bindungsdialog der Schritt-Karte oeffnet sich damit erneut, jetzt
 *        mit dem frisch angelegten Element in der Liste, statt den Nutzer nach
 *        dem Anlegen ohne Bindung stehen zu lassen.
 */
function addDataElement(onCreated) {
  const name = el("input", { type: "text", placeholder: "z. B. betrag" });
  const type = el("select", null, ...DATA_TYPES.map((t) => el("option", { value: t }, typeName(t))));
  openModal("Datenelement", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field" }, "Typ", type)), async () => {
    if (!name.value.trim()) return false;
    try {
      const known = new Set(Object.keys(state.schema.data_elements || {}));
      const schema = await api.post(`/schemas/${state.schemaId}/data-elements`, { name: name.value.trim(), data_type: type.value });
      const created = Object.keys(schema.data_elements || {}).find((id) => !known.has(id));
      await refreshSchema(); render(); toast("ok", "Datenelement angelegt");
      // Erst NACH dem Schliessen dieses Dialogs fortsetzen: ``openModal``
      // leert nach dem Bestaetigen seinen Container -- ein hier sofort
      // geoeffneter Folgedialog verschwand mit.
      if (typeof onCreated === "function") setTimeout(() => onCreated(created), 0);
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * „+ Datenbindung“ der Datensicht: erst den Schritt waehlen, dann derselbe
 * Bindungsdialog wie an der Schritt-Karte (``bindDataDialog``: Element,
 * Richtung, Pflicht/optional). Frueher eine eigene, schwaechere Fassung, die
 * immer als Pflicht band -- die D1-Abhilfe „nicht als Pflicht setzen“ war dort
 * nicht moeglich.
 *
 * @param {string} [fixedNodeId] Schritt vorgeben (dann direkt der Bindungsdialog)
 */
function addDataAccess(fixedNodeId) {
  const schema = state.schema;
  if (typeof fixedNodeId === "string") { bindDataDialog(fixedNodeId); return; }
  const nodeSel = el("select", null, ...activitiesOf(schema).map((n) => el("option", { value: n.id }, nodeCaption(n))));
  openModal("Datenbindung – Schritt wählen", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Schritt", nodeSel)), async () => {
    // Der Bindungsdialog ersetzt diesen; false, sonst schloesse openModal ihn.
    bindDataDialog(nodeSel.value);
    return false;
  }, "Weiter");
}

function editDataElement(elem) {
  const name = el("input", { type: "text", value: elem.name });
  const type = el("select", null, ...DATA_TYPES.map((t) => el("option", { value: t, selected: t === elem.data_type ? "selected" : null }, typeName(t))));
  openModal("Datenelement bearbeiten", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field" }, "Typ", type)), async () => {
    if (!name.value.trim()) return false;
    return commitSchemaChange(() => api.patch(`/schemas/${state.schemaId}/data-elements/${elem.id}`, { name: name.value.trim(), data_type: type.value }), "Datenelement aktualisiert");
  }, "Speichern");
}

function resetDataElementSource(elem) {
  openModal("Quelle zur\u00FCcksetzen",
    el("p", null, `Externe Bindung von \u201E${elem.name}\u201C entfernen und wieder als Instanz-Datenelement f\u00FChren?`),
    async () => {
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${elem.id}/reset-source`, {}), "Quelle zur\u00FCckgesetzt");
    }, "Zur\u00FCcksetzen", { danger: true });
}

function deleteDataElement(elem) {
  openModal("Datenelement l\u00F6schen",
    el("p", null, `Datenelement \u201E${elem.name}\u201C und alle zugeh\u00F6rigen Lese-/Schreibbindungen und Maskenfelder l\u00F6schen?`),
    async () => {
      return commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/data-elements/${elem.id}`), "Datenelement gel\u00F6scht");
    }, "L\u00F6schen", { danger: true });
}

// --------------------------------------------------------------------------
// View: Ressourcensicht
// --------------------------------------------------------------------------

function viewOrg() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;
  const draft = isDraft(schema);
  const linked = !!schema.org_model_id;
  // A shared organisation is editable independently of this schema's lifecycle
  // (it is master data used across models); a local org follows the draft gate.
  const orgEditable = draft || linked;
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };

  const orgPanel = sharedOrgBanner(schema, draft);

  const rolePanel = rolesPanel(org, orgEditable);

  const unitPanel = orgUnitPanel(org, orgEditable);
  const licPanel = licensePanel();
  const agentPanel = agentListPanel(org, orgEditable);
  // Fire-and-forget refresh of the licensing snapshot; re-renders only when the
  // data actually changed (guarded by a signature), so there is no render loop.
  refreshLicenseInfo();

  // BZR-Zuordnung
  const ruleEntries = Object.entries(schema.staff_rules || {});
  const ruleRows = ruleEntries.map(([nid, rule]) => {
    const node = schema.nodes[nid];
    return [node ? nodeCaption(node) : nid, describeRule(rule)];
  });
  const ruleTable = ruleRows.length
    ? table(["Schritt", "Bearbeiterregel"], ruleRows,
        (i) => ruleEntries[i][0] === state.staffFocusNode ? "hl-row" : "")
    : emptyState("Noch keine Bearbeiterzuordnung.");
  const addRuleBtn = el("button", { class: "btn small", onClick: addStaffRule,
    ...lockedBy([!draft, DRAFT_ONLY_REASON],
      [activitiesOf(schema).length === 0, "Erst einen Schritt modellieren."],
      [(Object.keys(org.roles || {}).length + Object.keys(org.org_units || {}).length) === 0,
        "Erst eine Rolle oder Abteilung anlegen."]) }, "+ Zuordnung");
  const rulePanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Bearbeiterzuordnung (BZR)"), el("span", { class: "sub" }, "Z1-Z4 live"), el("span", { class: "spacer", style: "flex:1" }), addRuleBtn),
    el("div", { class: "panel-b" }, ruleTable));

  const staffFocus = state.staffFocusNode && schema.nodes[state.staffFocusNode];
  const orgFocusUnit = state.orgFocusUnit && (org.org_units || {})[state.orgFocusUnit];
  content.appendChild(el("div", { class: "grid-2" },
    el("div", null, orgPanel, licPanel, rolePanel, unitPanel, agentPanel),
    el("div", null,
      returnBar(),
      staffFocus
        ? focusBanner("Hervorgehoben: Zuordnung von \u201E" + nodeCaption(staffFocus) + "\u201C",
            () => { state.staffFocusNode = null; render(); })
        : null,
      rulePanel, zFindingsPanel(),
      orgFocusUnit
        ? focusBanner("Hervorgehoben: Abteilung \u201E" + orgFocusUnit.name + "\u201C inkl. zugeh\u00F6riger Agenten",
            () => { state.orgFocusUnit = null; state.orgFocusAgents = []; render(); })
        : null,
      orgChartPanel(org))));
  scrollHighlightIntoView();
}

// --------------------------------------------------------------------------
// Licensing / agent metering (agent page). All of this stays hidden while the
// backend runs in the default "open" mode (enforced=false): the panel and the
// per-agent badges only render once a licensor key is configured, so the
// dormant state is visually indistinguishable from before licensing existed.
// --------------------------------------------------------------------------

// Refresh the licensing snapshot in the background. Re-renders the org view
// only when the fetched data changed (signature compare) to avoid a loop; on
// any error (e.g. old backend without the endpoints) it silently gives up.
function refreshLicenseInfo() {
  Promise.all([api.get("/license/status"), api.get("/license/agents")])
    .then(([status, agents]) => {
      const sig = JSON.stringify([status, agents]);
      if (sig === state._licenseSig) return;
      state._licenseSig = sig;
      state.license = status;
      state.licenseAgents = {};
      (agents || []).forEach((a) => { state.licenseAgents[a.agent_id] = a; });
      if (state.view === "org") render();
    })
    .catch(() => {});
}

// The contingent panel: a used/total quota bar, a staged expiry banner and the
// "+5 Agenten kaufen" call-to-action. Returns null (no panel) unless licensing
// is actively enforced.
function licensePanel() {
  const lic = state.license;
  if (!lic || !lic.enforced) return null;
  const total = lic.total_slots || 0;
  const used = lic.used_slots || 0;
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const buyBtn = el("button", { class: "btn small primary", onClick: buyAgentPack }, "+5 Agenten kaufen");
  const head = el("div", { class: "panel-h" }, el("h2", null, "Lizenz"),
    el("span", { class: "sub" }, `${used}/${total} Agenten belegt`),
    el("span", { class: "spacer", style: "flex:1" }), buyBtn);
  const bar = el("div", { class: "lic-bar", title: `${used} von ${total} lizenzierten Agenten-Slots belegt` },
    el("div", { class: "lic-bar-fill", style: `width:${pct}%` }));
  const body = el("div", { class: "panel-b" }, bar);
  // Staged renewal reminder (30/14/7/1 days), computed locally from the ratchet.
  const days = lic.days_to_next_expiry;
  if (days != null && days <= 30) {
    const when = new Date((lic.next_expiry_at || 0) * 1000).toLocaleDateString("de-DE");
    body.appendChild(el("div", { class: "lic-banner" + (days <= 7 ? " urgent" : "") },
      `Ein Agenten-Paket läuft in ${days} Tag${days === 1 ? "" : "en"} ab (${when}) – jetzt verlängern.`));
  }
  if (lic.packs_active === 0 && used >= total) {
    body.appendChild(el("div", { class: "sub", style: "margin-top:8px" },
      "Free-Kontingent ausgeschöpft. Mit einem Paket weitere Agenten freischalten."));
  }
  return el("div", { class: "panel" }, head, body);
}

// Per-agent licensing badge for the Agenten table (only when enforced).
function agentLicenseBadge(agentId) {
  const v = (state.licenseAgents || {})[agentId];
  if (!v) return el("span", { class: "muted" }, "–");
  if (v.licensed) {
    const label = v.days_left != null ? `${v.days_left} Tage` : "gültig";
    return el("span", { class: "pill pill-green", title: "Lizenziert" }, label);
  }
  return el("span", { class: "pill pill-red", title: "Nicht lizenziert – Paket kaufen/verlängern" }, "ungedeckt");
}

// Start a purchase. The checkout itself runs on a separate licensor backend;
// with none configured the API reports so and we show a friendly note.
async function buyAgentPack() {
  try {
    const res = await api.post("/license/checkout", { slots: 5, months: 12 });
    if (res.checkout_url) {
      window.open(res.checkout_url, "_blank", "noopener");
      if (res.claim_token) {
        // Online auto-pull configured: keep polling the licensor in the
        // background and activate the pack automatically once it is issued,
        // so the operator does not have to copy-&-paste the token.
        toast("ok", "Kauf gestartet", ["Bitte im neuen Tab abschließen – die Lizenz wird danach automatisch eingespielt."]);
        pollClaimsUntilActivated();
      } else {
        toast("ok", "Kauf gestartet", ["Bitte im neuen Tab abschließen; die Lizenz danach unter „Aktivieren“ einspielen."]);
      }
    } else {
      toast("err", "Kauf nicht verfügbar", [res.message]);
    }
  } catch (err) {
    toastError(err);
  }
}

// Best-effort online auto-pull: after a checkout with a claim token, poll the
// backend (which in turn contacts the separate licensor) every few seconds
// until a pack is activated or no claim is left pending. Never blocks anything;
// the manual "Aktivieren" copy-&-paste path always remains available as a
// fallback. Runs at most a bounded number of passes so it cannot loop forever.
async function pollClaimsUntilActivated() {
  if (state._claimPolling) return;
  state._claimPolling = true;
  const INTERVAL_MS = 6000;
  const MAX_PASSES = 60; // ~6 min ceiling; matches the short claim TTL
  try {
    for (let i = 0; i < MAX_PASSES; i++) {
      await new Promise((r) => setTimeout(r, INTERVAL_MS));
      let res;
      try {
        res = await api.post("/license/claims/poll", {});
      } catch (_e) {
        continue; // transient; try again next pass
      }
      if (res && res.activated > 0) {
        state._licenseSig = null; // force a re-render on the next refresh
        refreshLicenseInfo();
        toast("ok", "Lizenz aktiviert", ["Das gekaufte Agenten-Paket wurde automatisch eingespielt."]);
        return;
      }
      if (!res || res.pending === 0) return; // nothing left to wait for
    }
  } finally {
    state._claimPolling = false;
  }
}

// Endpoint base for org-entity edits: the shared org registry when the schema
// is linked, otherwise the schema's embedded org. The same path suffixes
// (/roles, /org-units, /agents, ...) exist under both bases.
function orgApi(suffix) {
  const oid = state.schema && state.schema.org_model_id;
  return oid ? `/org-models/${oid}${suffix}` : `/schemas/${state.schemaId}${suffix}`;
}

function sharedOrgBanner(schema, draft) {
  const linked = !!schema.org_model_id;
  const head = el("div", { class: "panel-h" }, el("h2", null, "Organisation"),
    el("span", { class: "sub" }, linked ? "geteilt (modell\u00FCbergreifend)" : "lokal in diesem Modell"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: manageSharedOrg }, linked ? "Verwalten" : "Geteilte Organisation\u2026"));
  const body = el("div", { class: "panel-b" }, el("div", { class: "sub" }, linked
    ? "Diese Organisation wird zentral gepflegt; \u00C4nderungen wirken sofort in allen verkn\u00FCpften Modellen."
    : "Die Organisation geh\u00F6rt nur zu diesem Modell. Verkn\u00FCpfe sie, um dieselbe Organisation in mehreren Modellen zu verwenden."));
  return el("div", { class: "panel" }, head, body);
}

async function manageSharedOrg() {
  const schema = state.schema;
  const draft = isDraft(schema);
  let orgs = [];
  try { orgs = await api.get("/org-models"); } catch (err) { orgs = []; }

  if (schema.org_model_id) {
    const cur = orgs.find((o) => o.id === schema.org_model_id);
    const body = el("div", null,
      el("div", { class: "field" }, "Verkn\u00FCpft mit geteilter Organisation: ",
        el("strong", null, cur ? cur.name : schema.org_model_id)),
      el("p", { class: "sub", style: "margin-top:10px" }, draft
        ? "Beim L\u00F6sen wird die aktuelle Organisation als lokale Kopie ins Modell \u00FCbernommen."
        : "Zum L\u00F6sen der Verkn\u00FCpfung muss das Schema im Entwurf sein."));
    openModal("Geteilte Organisation", body, draft ? async () => {
      return commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/org-model`), "Verkn\u00FCpfung gel\u00F6st");
    } : null, draft ? "Verkn\u00FCpfung l\u00F6sen" : "Schliessen");
    return;
  }

  const sel = el("select", null, el("option", { value: "" }, "\u2013 vorhandene w\u00E4hlen \u2013"),
    ...orgs.map((o) => el("option", { value: o.id }, o.name)));
  const newName = el("input", { type: "text", placeholder: "z. B. Stadtverwaltung" });
  const body = el("div", null,
    el("label", { class: "field" }, "Vorhandene Organisation", sel),
    el("div", { class: "sub", style: "margin:10px 0" }, "\u2013 oder neue anlegen \u2013"),
    el("label", { class: "field" }, "Name", newName),
    draft ? null : el("p", { class: "sub", style: "margin-top:10px" }, "Verkn\u00FCpfen ist nur im Entwurf m\u00F6glich."));
  openModal("Geteilte Organisation verkn\u00FCpfen", body, async () => {
    if (!draft) { toast("err", "Nur im Entwurf m\u00F6glich"); return false; }
    try {
      let orgId = sel.value;
      if (!orgId) {
        if (!newName.value.trim()) return false;
        const created = await api.post("/org-models", { name: newName.value.trim() });
        orgId = created.id;
      }
      await api.post(`/schemas/${state.schemaId}/org-model`, { org_model_id: orgId });
      await refreshSchema(); render(); toast("ok", "Mit geteilter Organisation verkn\u00FCpft");
    } catch (err) { toastError(err); return false; }
  }, "Verkn\u00FCpfen");
}

function orgUnitPanel(org, draft) {
  const units = Object.values(org.org_units || {});
  const head = el("div", { class: "panel-h" }, el("h2", null, "Abteilungen"),
    el("span", { class: "sub" }, "Hierarchie mit Vorgesetzten"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: () => addChildOrgUnit(null), ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Abteilung"));
  const body = el("div", { class: "panel-b" });
  if (!units.length) { body.appendChild(emptyState("Noch keine Abteilung.")); return el("div", { class: "panel" }, head, body); }

  // Kinder je Elternknoten indexieren; verwaiste (unbekannter Parent) auf oberste Ebene.
  const known = org.org_units || {};
  const childrenOf = {};
  units.forEach((u) => {
    const p = u.parent_id && known[u.parent_id] ? u.parent_id : "__root__";
    (childrenOf[p] = childrenOf[p] || []).push(u);
  });
  Object.values(childrenOf).forEach((list) => list.sort((a, b) => a.name.localeCompare(b.name)));

  const tree = el("div", { class: "tree" });
  (childrenOf["__root__"] || []).forEach((u) => tree.appendChild(renderUnitNode(u, org, draft, childrenOf)));
  body.appendChild(tree);
  return el("div", { class: "panel" }, head, body);
}

function renderUnitNode(unit, org, draft, childrenOf) {
  const mgr = unit.manager_id && org.agents[unit.manager_id] ? org.agents[unit.manager_id].name : null;
  const rowCls = "tree-row" + (unit.id === state.orgFocusUnit ? " tree-row-hl" : "");
  const row = el("div", { class: rowCls },
    el("span", { class: "tree-name" }, unit.name),
    mgr
      ? el("span", { class: "tree-badge tree-badge-link", title: "Vorgesetzten in der Agentenliste hervorheben",
          onClick: () => focusOrgAgent(unit.manager_id, unit.id) }, "\u2605 " + mgr)
      : el("span", { class: "tree-badge muted-badge" }, "kein Vorgesetzter"),
    unit.mailbox
      ? el("span", { class: "tree-badge", title: "Abteilungspostfach (Gruppen-Benachrichtigung)" }, "\u2709 " + unit.mailbox)
      : null,
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: () => editManager(unit) }, "Vorgesetzter"),
    el("button", { class: "btn small", onClick: () => editUnitMailbox(unit) }, "Postfach"),
    el("button", { class: "btn small", onClick: () => moveOrgUnit(unit) }, "Umh\u00E4ngen"),
    draft ? el("button", { class: "btn small", onClick: () => addChildOrgUnit(unit.id) }, "+ Unter") : null);
  const node = el("div", { class: "tree-node" }, row);
  const kids = childrenOf[unit.id] || [];
  if (kids.length) {
    const childWrap = el("div", { class: "tree-children" });
    kids.forEach((c) => childWrap.appendChild(renderUnitNode(c, org, draft, childrenOf)));
    node.appendChild(childWrap);
  }
  return node;
}

function agentListPanel(org, draft) {
  const agents = Object.values(org.agents || {});
  // Login-Spalte (nur Admin, Passwort-Modus): wird nach dem Laden der Logins
  // gefuellt (fillLoginSlots), damit die Tabelle nicht auf /users wartet.
  const loginSlots = {};
  // The licensing badge column only appears while enforcement is active.
  const showLicense = !!(state.license && state.license.enforced);
  const head = el("div", { class: "panel-h" }, el("h2", null, "Agenten"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: addAgent, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Agent"));
  const body = el("div", { class: "panel-b" });
  if (!agents.length) body.appendChild(emptyState("Noch keine Agenten."));
  else {
    const rows = agents.map((a) => {
      const roles = (a.role_ids || []).map((r) => org.roles[r] ? org.roles[r].name : r).join(", ") || "\u2013";
      const unit = a.org_unit_id && org.org_units[a.org_unit_id] ? org.org_units[a.org_unit_id].name : "\u2013";
      const dep = a.deputy_id && org.agents[a.deputy_id] ? org.agents[a.deputy_id].name : "\u2013";
      const editBtn = el("button", { class: "btn small", onClick: () => editAgent(a), ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "Bearbeiten");
      const depBtn = el("button", { class: "btn small", onClick: () => editDeputy(a) }, "Vertreter");
      const actions = el("div", { style: "display:flex; gap:6px; justify-content:flex-end" }, editBtn, depBtn);
      // Login provisioning is an admin-only convenience available in password
      // mode; it is independent of the schema lifecycle (works on shared orgs).
      if (state.passwordLogin && hasRole("admin")) {
        const slot = el("span", { class: "login-slot" });
        loginSlots[a.id] = { slot, agent: a };
        actions.appendChild(slot);
      }
      const email = a.email
        ? a.email
        : el("span", { class: "muted" }, "–");
      const row = [a.name, email, roles, unit, dep];
      if (showLicense) row.push(agentLicenseBadge(a.id));
      row.push(actions);
      return row;
    });
    const cols = ["Agent", "E-Mail", "Rollen", "Abteilung", "Vertreter"];
    if (showLicense) cols.push("Lizenz");
    cols.push("");
    body.appendChild(table(cols, rows,
      (i) => (state.orgFocusAgents || []).includes(agents[i].id) ? "hl-row" : ""));
    if (Object.keys(loginSlots).length) fillLoginSlots(loginSlots);
  }
  return el("div", { class: "panel" }, head, body);
}

/**
 * Fuellt die Login-Spalte der Agententabelle: bestehender Login oder Knopf.
 *
 * Hat eine Person schon einen Login (``GET /users``, Feld ``agent_id``),
 * steht dort „Login: <name>“ mit Verweis auf Administration → Benutzer –
 * ein zweiter Klick legte sonst still einen weiteren Login (``name2``) an.
 * Sonst der Knopf „Login“ (``provisionLogin``). Scheitert das Laden, bleibt
 * es beim Knopf: Anlegen ist dann weiterhin moeglich, der Kern lehnt einen
 * doppelten Loginnamen ohnehin ab.
 *
 * @param {Object<string, {slot: HTMLElement, agent: object}>} slots je Agent-id
 * @returns {Promise<void>}
 */
async function fillLoginSlots(slots) {
  let users = [];
  try { users = await api.get("/users"); } catch (_e) { users = []; }
  const byAgent = {};
  (users || []).forEach((u) => { if (u.agent_id && !byAgent[u.agent_id]) byAgent[u.agent_id] = u; });
  Object.entries(slots).forEach(([agentId, { slot, agent }]) => {
    clear(slot);
    const u = byAgent[agentId];
    slot.appendChild(u
      ? el("span", { class: "muted login-exists",
          title: "Passwort zur\u00FCcksetzen oder l\u00F6schen unter Administration \u2192 Benutzer" },
          `Login: ${u.login}`)
      : el("button", { class: "btn small", onClick: () => provisionLogin(agent) }, "Login"));
  });
}

// Organigramm: the org-unit hierarchy rendered as a classic top-down org chart
// (HTML/CSS, no SVG). Clicking a box highlights that unit (in the chart and the
// Abteilungen tree) and every agent that belongs to it -- including the unit's
// supervisor -- in the Agenten table.
function orgChartPanel(org) {
  const units = Object.values(org.org_units || {});
  const head = el("div", { class: "panel-h" }, el("h2", null, "Organigramm"),
    el("span", { class: "sub" }, "Klick hebt Abteilung + Agenten hervor"));
  const body = el("div", { class: "panel-b" });
  if (!units.length) {
    body.appendChild(emptyState("Noch keine Abteilung modelliert."));
    return el("div", { class: "panel" }, head, body);
  }
  // Kinder je Elternknoten indexieren; verwaiste (unbekannter Parent) als Wurzel.
  const known = org.org_units || {};
  const childrenOf = {};
  units.forEach((u) => {
    const p = u.parent_id && known[u.parent_id] ? u.parent_id : "__root__";
    (childrenOf[p] = childrenOf[p] || []).push(u);
  });
  Object.values(childrenOf).forEach((list) => list.sort((a, b) => a.name.localeCompare(b.name)));
  const roots = childrenOf["__root__"] || [];
  body.appendChild(el("div", { class: "orgchart" },
    el("ul", null, ...roots.map((u) => orgChartNode(u, org, childrenOf)))));
  return el("div", { class: "panel" }, head, body);
}

function orgChartNode(unit, org, childrenOf) {
  const mgr = unit.manager_id && (org.agents || {})[unit.manager_id] ? org.agents[unit.manager_id].name : null;
  const memberCount = Object.values(org.agents || {}).filter((a) => a.org_unit_id === unit.id).length;
  const cls = "oc-node" + (unit.id === state.orgFocusUnit ? " selected" : "");
  const box = el("div", { class: cls, title: "Abteilung + zugeh\u00F6rige Agenten hervorheben",
    onClick: () => focusOrgUnit(unit.id) },
    el("div", { class: "oc-name" }, unit.name),
    mgr ? el("div", { class: "oc-mgr" }, "\u2605 " + mgr) : el("div", { class: "oc-mgr oc-muted" }, "kein Vorgesetzter"),
    el("div", { class: "oc-count" }, memberCount + (memberCount === 1 ? " Agent" : " Agenten")));
  const li = el("li", null, box);
  const kids = childrenOf[unit.id] || [];
  if (kids.length) li.appendChild(el("ul", null, ...kids.map((c) => orgChartNode(c, org, childrenOf))));
  return li;
}

/**
 * Bearbeiterregel in einem Satz -- mit Namen statt interner IDs.
 *
 * Ohne ``schema`` wird das gerade gewaehlte genommen; das ist in allen
 * Modellier-Sichten das richtige. Ist eine Referenz dort unbekannt (etwa in
 * einer fremden Organisation), bleibt die ID stehen, statt den Satz zu
 * verschweigen. Reine Anzeige -- keine Zustaendigkeitsentscheidung.
 *
 * @param {object} rule Bearbeiterregel (StaffRule) oder null
 * @param {object} [schema] Schema, gegen das Namen aufgeloest werden
 * @returns {string}
 */
function describeRule(rule, schema) {
  if (!rule) return "\u2013";
  const s = schema === undefined ? state.schema : schema;
  const org = (s && s.org_model) || {};
  const named = (map, id) => ((map || {})[id] || {}).name || id;
  const stepName = (id) => {
    const n = s && s.nodes ? s.nodes[id] : null;
    return n && n.label ? n.label : id;
  };
  if (rule.kind === "ROLE") return `Rolle: ${named(org.roles, rule.ref)}`;
  // „Abteilung“ wie in den Dialogen, nicht „OrgEinheit“.
  if (rule.kind === "ORG_UNIT") return `Abteilung: ${named(org.org_units, rule.ref)}${rule.recursive ? " (inkl. Unterbereiche)" : ""}`;
  // Beim Agenten lohnt der Rueckfall auf agentNameOf: kennt ihn die
  // Organisation dieses Schemas nicht, findet ihn oft das modelluebergreifende
  // Verzeichnis.
  if (rule.kind === "AGENT") {
    return `Agent: ${((org.agents || {})[rule.ref] || {}).name || agentNameOf(rule.ref)}`;
  }
  if (rule.kind === "NODE_PERFORMING_AGENT") return `Bearbeiter von \u201e${stepName(rule.ref)}\u201c`;
  if (rule.kind === "NODE_PERFORMING_AGENT_SUPERVISOR") return `Vorgesetzte:r des Bearbeiters von \u201e${stepName(rule.ref)}\u201c`;
  // Verknuepfungen im Wortlaut des Dialogs statt „AND(…, …)“.
  if (rule.operands) {
    const parts = rule.operands.map((o) => (o.operands ? `(${describeRule(o, s)})` : describeRule(o, s)));
    const word = { AND: " und ", OR: " oder ", EXCEPT: " au\u00DFer " }[rule.kind];
    return word ? parts.join(word) : `${rule.kind}(${parts.join(", ")})`;
  }
  return rule.kind;
}

/**
 * Bezieht sich eine Bearbeiterregel auf die Person, die ``nodeId`` erledigt hat?
 *
 * Solche relativen Regeln (Vier-Augen-Prinzip: "Vorgesetzte:r des Bearbeiters
 * von X") finden niemanden, wenn X per Aufsichtseingriff ohne Bearbeiter
 * abgeschlossen wurde -- dann steht kein Ausfuehrer im Vorgang, auf den sie sich
 * beziehen koennten. Rein lesend; der Client entscheidet daraus nichts, er
 * erklaert es nur.
 *
 * @param {object} rule Bearbeiterregel
 * @param {string} nodeId Knoten, auf den sich die Regel beziehen koennte
 * @returns {boolean}
 */
function ruleRefersToPerformerOf(rule, nodeId) {
  if (!rule) return false;
  if (rule.kind === "NODE_PERFORMING_AGENT" || rule.kind === "NODE_PERFORMING_AGENT_SUPERVISOR") {
    return rule.ref === nodeId;
  }
  return (rule.operands || []).some((o) => ruleRefersToPerformerOf(o, nodeId));
}

/**
 * Enthaelt die Regel (auch verschachtelt) einen Bezug auf den Ausfuehrer eines
 * anderen Schritts? Dann erklaert sich eine leere Bearbeitermenge oft daraus.
 *
 * @param {object} rule Bearbeiterregel
 * @returns {boolean}
 */
function ruleIsRelative(rule) {
  if (!rule) return false;
  if (rule.kind === "NODE_PERFORMING_AGENT" || rule.kind === "NODE_PERFORMING_AGENT_SUPERVISOR") return true;
  return (rule.operands || []).some(ruleIsRelative);
}

function zFindingsPanel() {
  const v = state.validation;
  const zs = v && !v.correct ? v.findings.filter((f) => f.rule[0] === "Z" || f.rule[0] === "A") : [];
  const body = el("div", { class: "panel-b" });
  if (!zs.length) body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Ressourcen/Bearbeiter konsistent (Z/A)."));
  else zs.forEach((f) => body.appendChild(el("div", { class: "finding" }, el("span", { class: "rule" }, f.rule), el("span", null, findingText(f, { withHint: true })))));
  return el("div", { class: "panel" }, el("div", { class: "panel-h" }, el("h2", null, "Ressourcen-Befunde")), body);
}

function addRole() {
  const name = el("input", { type: "text", placeholder: "z. B. Sachbearbeiter" });
  openModal("Rolle", el("label", { class: "field" }, "Name", name), async () => {
    if (!name.value.trim()) return false;
    return commitSchemaChange(() => api.post(orgApi(`/roles`), { name: name.value.trim() }), "Rolle angelegt");
  }, "Anlegen");
}

// Rollen-Panel mit optionalem Gruppenpostfach (Regelgruppe N): die Sammeladresse,
// an die „an das Gruppenpostfach“-Benachrichtigungen (TO_GROUP_MAILBOX) gehen. Der
// Kern prüft Syntax (N1) und – solange ein mail-gebundener Schritt sie braucht –
// Vollständigkeit (N3), lehnt das Entfernen also ggf. mit 422 ab.
function rolesPanel(org, orgEditable) {
  const roles = Object.values(org.roles || {});
  const head = el("div", { class: "panel-h" }, el("h2", null, "Rollen"),
    el("span", { class: "sub" }, "Gruppenpostfach optional"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", disabled: !orgEditable, onClick: addRole }, "+ Rolle"));
  const body = el("div", { class: "panel-b" });
  if (!roles.length) { body.appendChild(emptyState("Noch keine Rolle.")); return el("div", { class: "panel" }, head, body); }
  const rows = roles.map((r) => {
    const box = r.mailbox ? r.mailbox : el("span", { class: "muted" }, "–");
    const btn = el("button", { class: "btn small", disabled: !orgEditable,
      onClick: () => editRoleMailbox(r) }, "Postfach");
    return [r.name, box, el("div", { style: "display:flex;justify-content:flex-end" }, btn)];
  });
  body.appendChild(table(["Rolle", "Gruppenpostfach", ""], rows));
  return el("div", { class: "panel" }, head, body);
}

// Gruppenpostfach einer Rolle setzen/entfernen (PUT …/roles/{id}/mailbox). Der
// Endpunkt ist unter geteilter wie eingebetteter Organisation gleich benannt.
function editRoleMailbox(role) {
  const mailbox = el("input", { type: "email", value: role.mailbox || "",
    placeholder: "z. B. sachbearbeitung@firma.de" });
  openModal(`Gruppenpostfach – Rolle ${role.name}`, el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      "Sammeladresse für „an das Gruppenpostfach“-Benachrichtigungen. Leer lassen entfernt sie."),
    el("label", { class: "field" }, "E-Mail", mailbox)), async () => {
    return commitSchemaChange(() => api.put(orgApi(`/roles/${role.id}/mailbox`), { mailbox: mailbox.value.trim() || null }), "Gruppenpostfach gespeichert");
  }, "Speichern");
}

// Abteilungspostfach setzen/entfernen. Achtung: der Endpunkt-Pfad unterscheidet
// sich zwischen geteilter (/org-models/{id}/units/…) und eingebetteter
// (/schemas/{id}/org-units/…) Organisation, daher hier eigens aufgelöst.
function editUnitMailbox(unit) {
  const oid = state.schema && state.schema.org_model_id;
  const url = oid
    ? `/org-models/${oid}/units/${unit.id}/mailbox`
    : `/schemas/${state.schemaId}/org-units/${unit.id}/mailbox`;
  const mailbox = el("input", { type: "email", value: unit.mailbox || "",
    placeholder: "z. B. einkauf@firma.de" });
  openModal(`Abteilungspostfach – ${unit.name}`, el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      "Sammeladresse der Abteilung für „an das Gruppenpostfach“-Benachrichtigungen. Leer lassen entfernt sie."),
    el("label", { class: "field" }, "E-Mail", mailbox)), async () => {
    return commitSchemaChange(() => api.put(url, { mailbox: mailbox.value.trim() || null }), "Abteilungspostfach gespeichert");
  }, "Speichern");
}

function addAgent() {
  const org = state.schema.org_model;
  const name = el("input", { type: "text", placeholder: "z. B. Erika Muster" });
  const email = el("input", { type: "email", placeholder: "z. B. erika@firma.de" });
  const roleSel = el("select", { multiple: "multiple", size: Math.min(5, Math.max(1, Object.keys(org.roles).length)) },
    ...Object.values(org.roles).map((r) => el("option", { value: r.id }, r.name)));
  const unitSel = el("select", null, el("option", { value: "" }, "\u2013 keine \u2013"),
    ...Object.values(org.org_units || {}).map((u) => el("option", { value: u.id }, u.name)));
  const depSel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).map((a) => el("option", { value: a.id }, a.name)));
  openModal("Agent", el("div", null,
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field", style: "margin-top:10px" }, "E-Mail (pers\u00f6nliches Postfach)", email),
    el("label", { class: "field", style: "margin-top:10px" }, "Rollen (Mehrfachauswahl)", roleSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Abteilung", unitSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Vertreter", depSel)), async () => {
    if (!name.value.trim()) return false;
    const roleIds = [...roleSel.selectedOptions].map((o) => o.value);
    const payload = { name: name.value.trim(), role_ids: roleIds };
    // Leeres Feld -> keine Adresse (nur mitsenden, wenn gesetzt; N1 pr\u00fcft Syntax).
    if (email.value.trim()) payload.email = email.value.trim();
    if (unitSel.value) payload.org_unit_id = unitSel.value;
    if (depSel.value) payload.deputy_id = depSel.value;
    try { await api.post(orgApi(`/agents`), payload); await refreshSchema(); render(); toast("ok", "Agent angelegt"); }
    catch (err) {
      // A 402 means the agent contingent is full: turn the error into a
      // purchase offer rather than a dead end (only reachable when enforced).
      if (err && err.status === 402) {
        const d = describeError(err);
        toast("err", "Agenten-Kontingent ausgeschöpft", d.lines.length ? d.lines : [d.title]);
        buyAgentPack();
        return false;
      }
      toastError(err); return false;
    }
  }, "Anlegen");
}

function addChildOrgUnit(parentId) {
  const org = state.schema.org_model;
  const name = el("input", { type: "text", placeholder: "z. B. Einkauf" });
  const mgrSel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).map((a) => el("option", { value: a.id }, a.name)));
  const parentName = parentId && org.org_units[parentId] ? org.org_units[parentId].name : "\u2013 oberste Ebene \u2013";
  const parentField = el("input", { type: "text", value: parentName, disabled: "disabled" });
  openModal("Abteilung", el("div", null,
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field", style: "margin-top:10px" }, "\u00DCbergeordnet", parentField),
    el("label", { class: "field", style: "margin-top:10px" }, "Vorgesetzter", mgrSel)), async () => {
    if (!name.value.trim()) return false;
    const payload = { name: name.value.trim() };
    if (parentId) payload.parent_id = parentId;
    if (mgrSel.value) payload.manager_id = mgrSel.value;
    return commitSchemaChange(() => api.post(orgApi(`/org-units`), payload), "Abteilung angelegt");
  }, "Anlegen");
}

function moveOrgUnit(unit) {
  const org = state.schema.org_model;
  // Eigenen Knoten + alle Nachfahren ausschliessen (Zyklus verhindern; Backend prueft zusaetzlich).
  const blocked = new Set([unit.id]);
  let changed = true;
  while (changed) {
    changed = false;
    Object.values(org.org_units).forEach((u) => {
      if (u.parent_id && blocked.has(u.parent_id) && !blocked.has(u.id)) { blocked.add(u.id); changed = true; }
    });
  }
  const sel = el("select", null, el("option", { value: "" }, "\u2013 oberste Ebene \u2013"),
    ...Object.values(org.org_units).filter((u) => !blocked.has(u.id)).map((u) => el("option", { value: u.id }, u.name)));
  if (unit.parent_id) sel.value = unit.parent_id;
  openModal(`Umh\u00E4ngen: ${unit.name}`, el("label", { class: "field" }, "\u00DCbergeordnete Abteilung", sel), async () => {
    return commitSchemaChange(() => api.post(orgApi(`/org-units/${unit.id}/parent`), { parent_id: sel.value || null }), "Abteilung umgeh\u00E4ngt");
  }, "Speichern");
}

function editManager(unit) {
  const org = state.schema.org_model;
  const sel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).map((a) => el("option", { value: a.id }, a.name)));
  if (unit.manager_id) sel.value = unit.manager_id;
  openModal(`Vorgesetzter: ${unit.name}`, el("label", { class: "field" }, "Vorgesetzter", sel), async () => {
    return commitSchemaChange(() => api.post(orgApi(`/org-units/${unit.id}/manager`), { manager_id: sel.value || null }), "Vorgesetzter gesetzt");
  }, "Speichern");
}

function editAgent(agent) {
  const org = state.schema.org_model;
  const name = el("input", { type: "text", value: agent.name });
  const roleSel = el("select", { multiple: "multiple", size: Math.min(5, Math.max(1, Object.keys(org.roles).length)) },
    ...Object.values(org.roles).map((r) => {
      const o = el("option", { value: r.id }, r.name);
      if ((agent.role_ids || []).includes(r.id)) o.selected = true;
      return o;
    }));
  const unitSel = el("select", null, el("option", { value: "" }, "\u2013 keine \u2013"),
    ...Object.values(org.org_units || {}).map((u) => el("option", { value: u.id }, u.name)));
  unitSel.value = agent.org_unit_id || "";
  const email = el("input", { type: "email", value: agent.email || "",
    placeholder: "z. B. erika@firma.de" });
  openModal(`Agent bearbeiten: ${agent.name}`, el("div", null,
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field", style: "margin-top:10px" }, "E-Mail (pers\u00f6nliches Postfach)", email),
    el("label", { class: "field", style: "margin-top:10px" }, "Rollen (Mehrfachauswahl)", roleSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Abteilung", unitSel)), async () => {
    if (!name.value.trim()) return false;
    const roleIds = [...roleSel.selectedOptions].map((o) => o.value);
    // Immer mitsenden: leeres Feld -> null entfernt eine Adresse. Der Kern lehnt
    // das Entfernen ab (N3), solange ein mail-gebundener Schritt sie braucht.
    const payload = { name: name.value.trim(), role_ids: roleIds,
      org_unit_id: unitSel.value || null, email: email.value.trim() || null };
    return commitSchemaChange(() => api.patch(orgApi(`/agents/${agent.id}`), payload), "Agent gespeichert");
  }, "Speichern");
}

function editDeputy(agent) {
  const org = state.schema.org_model;
  const sel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).filter((a) => a.id !== agent.id).map((a) => el("option", { value: a.id }, a.name)));
  if (agent.deputy_id) sel.value = agent.deputy_id;
  openModal(`Vertreter: ${agent.name}`, el("label", { class: "field" }, "Vertreter", sel), async () => {
    return commitSchemaChange(() => api.post(orgApi(`/agents/${agent.id}/deputy`), { deputy_id: sel.value || null }), "Vertreter gesetzt");
  }, "Speichern");
}

// Client-side mirror of the server's suggest_login (vorname.nachname). Only used
// to preview the suggestion; the server remains the source of truth.
function suggestLoginClient(name) {
  const map = { "\u00E4": "ae", "\u00F6": "oe", "\u00FC": "ue", "\u00DF": "ss",
    "\u00C4": "ae", "\u00D6": "oe", "\u00DC": "ue" };
  const translit = (name || "").replace(/[\u00E4\u00F6\u00FC\u00DF\u00C4\u00D6\u00DC]/g, (c) => map[c]);
  const ascii = translit.normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
  const parts = ascii.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  return parts.join(".") || "user";
}

// Admin convenience (password mode): provision a login for an agent. The login
// is suggested from the name (server is authoritative); the admin picks the
// coarse RBAC roles. The one-off initial password is shown once afterwards.
function provisionLogin(agent) {
  const roleSel = el("select", { multiple: "multiple", size: 4 },
    ...Object.keys(ROLE_LABELS).map((r) => {
      const o = el("option", { value: r }, ROLE_LABELS[r]);
      if (r === "operator") o.selected = true;
      return o;
    }));
  const loginInput = el("input", { type: "text", placeholder: suggestLoginClient(agent.name) });
  openModal(`Login anlegen: ${agent.name}`, el("div", null,
    el("div", { class: "muted", style: "margin-bottom:10px" },
      "Der Login wird aus dem Namen vorgeschlagen (\u00FCberschreibbar). Es wird ein Initialpasswort erzeugt, das einmalig angezeigt wird; die Person vergibt beim ersten Login ein eigenes Passwort."),
    el("label", { class: "field" }, "Rollen (Mehrfachauswahl)", roleSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Login (optional)", loginInput)), async () => {
    const roles = [...roleSel.selectedOptions].map((o) => o.value);
    if (!roles.length) { toast("err", "Bitte mindestens eine Rolle w\u00E4hlen"); return false; }
    const payload = { agent_id: agent.id, display_name: agent.name, roles };
    if (loginInput.value.trim()) payload.login = loginInput.value.trim();
    try {
      const res = await api.post("/users", payload);
      toast("ok", "Login angelegt", [`Login: ${res.login}`]);
      // Der Zugangsdaten-Dialog ersetzt diesen; ``false``, sonst schloesse
      // openModal ihn sofort wieder (ein Container fuer alle Dialoge).
      showLoginCredentials(res);
      render();   // die Zeile zeigt jetzt „Login: …“ statt des Knopfs
      return false;
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * Nur-Lese-Feld mit Kopierknopf fuer einen einmalig gezeigten Wert
 * (Login, Initialpasswort).
 *
 * Kopiert ueber die Zwischenablage-API; wo sie fehlt oder verweigert wird
 * (unsicherer Kontext, Berechtigung), markiert der Knopf den Text und
 * versucht ``execCommand("copy")``; scheitert auch das, bittet die
 * Beschriftung, selbst zu kopieren. Der Wert bleibt immer sichtbar.
 *
 * @param {string} label Feldbeschriftung
 * @param {string} value anzuzeigender Wert
 * @returns {HTMLElement} das Feld samt Knopf
 */
function copyField(label, value) {
  const input = el("input", { type: "text", value, readonly: "readonly", class: "copy-value",
    "aria-label": label });
  const btn = el("button", { class: "btn small", type: "button", onClick: async () => {
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
        ok = true;
      }
    } catch (_e) { /* Rueckfall unten */ }
    if (!ok) {
      try { input.focus(); input.select(); ok = !!document.execCommand("copy"); } catch (_e) { ok = false; }
    }
    btn.textContent = ok ? "Kopiert \u2713" : "Bitte markieren und kopieren";
  } }, "Kopieren");
  return el("div", { class: "field copy-field" }, el("span", null, label),
    el("div", { class: "copy-row" }, input, btn));
}

/**
 * Zeigt Login und einmaliges Initialpasswort (nach Anlegen oder Zuruecksetzen).
 *
 * Ergebnisdialog (``cancel: false``): ein Knopf „Schliessen“, kein
 * „Abbrechen“ und kein Schliessen per Klick daneben – das Passwort steht nur
 * hier und ist danach nicht wieder abrufbar.
 *
 * @param {{login: string, initial_password: string}} res Antwort von
 *   ``POST /users`` bzw. ``POST /users/{login}/reset-password``
 * @param {string} [title] Dialogtitel (Standard „Zugangsdaten“)
 */
function showLoginCredentials(res, title) {
  openModal(title || "Zugangsdaten", el("div", null,
    el("div", { class: "muted", style: "margin-bottom:10px" },
      "Bitte notieren und der Person sicher mitteilen. Das Initialpasswort wird nur jetzt angezeigt; beim ersten Anmelden vergibt die Person ein eigenes."),
    copyField("Login", res.login),
    copyField("Initialpasswort", res.initial_password)),
    async () => true, "Schlie\u00DFen", { cancel: false });
}


// Bearbeiterregel (BZR) zuweisen. ``fixedNodeId`` (optional) fixiert den Schritt
// fuer den „Bearbeiter zuordnen"-Knopf des Inspektors.
/**
 * Ein Teil einer Bearbeiterregel: Art (Rolle, Abteilung, Person, Bearbeiter
 * bzw. Vorgesetzte:r eines frueheren Schritts) und Bezug.
 *
 * Eigene Funktion, damit ``addStaffRule`` zwei davon verknuepfen kann.
 * Die node-bezogenen Arten bieten die uebrigen Aufgaben-Schritte an; ob der
 * Rueckbezug gueltig ist (Z3), entscheidet der Kern.
 *
 * @param {object} schema das Schema
 * @param {() => (string|null)} currentNode liefert den Zielschritt (nicht als Bezug anbieten)
 * @returns {{node: HTMLElement, read: () => (object|null), refresh: () => void}}
 */
function staffTermPicker(schema, currentNode) {
  const org = schema.org_model || {};
  const refSel = el("select");
  const kindSel = el("select", null,
    el("option", { value: "ROLE" }, "Rolle"),
    el("option", { value: "ORG_UNIT" }, "Abteilung"),
    el("option", { value: "AGENT" }, "Agent (konkrete Person)"),
    el("option", { value: "NODE_PERFORMING_AGENT" }, "Bearbeiter eines Schritts"),
    el("option", { value: "NODE_PERFORMING_AGENT_SUPERVISOR" }, "Vorgesetzte:r des Bearbeiters eines Schritts"));
  const recBox = el("input", { type: "checkbox" });
  const recField = el("label", { class: "field" }, recBox, " Abteilung und alle Bereiche darunter");
  const NODE_KINDS = ["NODE_PERFORMING_AGENT", "NODE_PERFORMING_AGENT_SUPERVISOR"];
  let pending = 0;
  function refresh() {
    clear(refSel);
    if (NODE_KINDS.includes(kindSel.value)) {
      // Nur Schritte, die garantiert vorher laufen -- dieselbe Analyse wie Z3
      // im Kern. Frueher standen auch Schritte des anderen XOR-Zweigs
      // zur Wahl, und erst der Klick brachte die Ablehnung.
      const ticket = ++pending;
      const target = currentNode();
      refSel.appendChild(el("option", { value: "" }, "\u2026 wird geladen"));
      api.get(`/schemas/${schema.id}/nodes/${target}/performer-candidates`)
        .then((ids) => {
          if (ticket !== pending) return;  // inzwischen andere Auswahl
          clear(refSel);
          ids.forEach((id) => {
            const n = schema.nodes[id];
            if (n) refSel.appendChild(el("option", { value: id }, nodeCaption(n)));
          });
          if (!ids.length) refSel.appendChild(el("option", { value: "" }, "(kein Schritt l\u00E4uft sicher vorher)"));
        })
        .catch(() => {
          if (ticket !== pending) return;
          clear(refSel);
          refSel.appendChild(el("option", { value: "" }, "(Schritte nicht abrufbar)"));
        });
    } else {
      const src = kindSel.value === "ROLE" ? org.roles
        : kindSel.value === "AGENT" ? org.agents : org.org_units;
      Object.values(src || {}).forEach((x) => refSel.appendChild(el("option", { value: x.id }, x.name)));
    }
    recField.style.display = kindSel.value === "ORG_UNIT" ? "" : "none";
  }
  kindSel.addEventListener("change", refresh); refresh();
  return {
    node: el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Art", kindSel),
      el("label", { class: "field" }, "Bezug", refSel),
      recField),
    read: () => {
      if (!refSel.value) return null;
      const term = { kind: kindSel.value, ref: refSel.value };
      if (kindSel.value === "ORG_UNIT") term.recursive = recBox.checked;
      return term;
    },
    refresh,
  };
}

/** Verknuepfungen im Regeldialog -- Wert = StaffRuleKind oder "". */
const STAFF_COMBINATORS = [
  { value: "", label: "keine – nur diese Angabe" },
  { value: "AND", label: "UND – muss auch Folgendes erfüllen" },
  { value: "OR", label: "ODER – alternativ auch" },
  { value: "EXCEPT", label: "AUSSER – ohne Folgende" },
];

/**
 * Der erweiterte Bearbeiter-Dialog: eine Angabe oder zwei verknuepfte.
 *
 * Kombinationen (UND/ODER/AUSSER) gab es bis 2026-09 nur ueber die API, obwohl
 * die Modellierer-Anleitung sie im gefuehrten Dialog versprach. Die
 * Regel entsteht hier als ``{kind: AND|OR|EXCEPT, operands: [a, b]}``; ob sie
 * jemanden findet (Z2) oder einen gueltigen Rueckbezug hat (Z3), prueft der
 * Kern und meldet es verstaendlich. Beide Oberflaechen erreichen den Dialog
 * ueber ``bindStaffDialog``; geschrieben wird ueber ``applyStaffRuleTo``.
 *
 * @param {string} [fixedNodeId] Zielschritt; ohne ihn gibt es eine Auswahl
 */
function addStaffRule(fixedNodeId) {
  const schema = state.schema;
  const nodeId = typeof fixedNodeId === "string" ? fixedNodeId : null;
  const nodeSel = nodeId
    ? null
    : el("select", null, ...activitiesOf(schema).map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const target = () => nodeId || (nodeSel && nodeSel.value);
  const first = staffTermPicker(schema, target);
  const second = staffTermPicker(schema, target);
  const combSel = el("select", null, ...STAFF_COMBINATORS.map((c) => el("option", { value: c.value }, c.label)));
  const secondHost = el("div", { class: "staff-term-second" }, second.node);
  const syncComb = () => { secondHost.style.display = combSel.value ? "" : "none"; };
  combSel.addEventListener("change", syncComb); syncComb();
  // Mehrfachzuordnung wie im Dialog der Schritt-Karte -- dieselbe Auswahl,
  // derselbe Schreibweg. Wechselt hier der
  // Schritt, wird die Liste neu gebaut: der gewaehlte Schritt darf nicht
  // zusaetzlich in ihr stehen.
  let others = otherStepsBox(schema, target());
  const othersHost = el("div", null, others.node);
  if (nodeSel) {
    nodeSel.addEventListener("change", () => {
      first.refresh(); second.refresh();
      clear(othersHost);
      others = otherStepsBox(schema, nodeSel.value);
      if (others.node) othersHost.appendChild(others.node);
    });
  }
  openModal("Bearbeiterregel", el("div", { class: "form-grid" },
    nodeId
      // ``wide``: Schritt und Verknuepfung stehen allein in ihrer Zeile --
      // im halben Raster brach der Schrittname um und „AUSSER – ohne
      // Folgende“ war abgeschnitten.
      ? el("div", { class: "field wide" }, "Schritt: ", el("strong", null, nodeCaption(schema.nodes[nodeId])))
      : el("label", { class: "field wide" }, "Schritt", nodeSel),
    first.node,
    el("label", { class: "field wide" }, "Verknüpfen mit", combSel),
    secondHost,
    othersHost), async () => {
    const a = first.read();
    if (!a) { toast("err", "Keine Angabe verf\u00FCgbar"); return false; }
    let rule = a;
    if (combSel.value) {
      const b = second.read();
      if (!b) { toast("err", "Bitte auch die zweite Angabe w\u00E4hlen"); return false; }
      rule = { kind: combSel.value, operands: [a, b] };
    }
    return applyStaffRuleTo(target(), others.selected(), rule);
  }, "Zuordnen");
}

