// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Modellieren.
 *
 * Laden und Auswahl, Kopfleiste mit Schema-Auswahl, Prozessvorlagen und
 * die Modellieren-Sicht in beiden Oberflaechen (Schritt-Karte und
 * klassisch) samt Bindungs-Palette, Bindungsdialog und Knoten-Inspektor.
 * Jede Bindungs- und Knotenoperation ist eine eigene Funktion, die beide
 * Oberflaechen aufrufen.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// Laden / Auswahl
// --------------------------------------------------------------------------

async function loadSchemas() {
  state.schemaIds = await api.get("/schemas");
  // The list endpoint returns only IDs; fetch each name + version once so the
  // picker can show the human-readable schema name plus its revision (e.g.
  // "Urlaubsantrag (v2)") instead of the raw ID. Revisions share the same name
  // but carry a fresh ID and an incremented version, so the version makes them
  // distinguishable in the selection.
  const unknown = state.schemaIds.filter((id) => !(id in state.schemaNames));
  if (unknown.length) {
    const entries = await Promise.all(unknown.map(async (id) => {
      try { const s = await api.get(`/schemas/${id}`); return [id, s.name, s.version]; }
      catch (_e) { return [id, id, null]; }
    }));
    for (const [id, name, version] of entries) {
      state.schemaNames[id] = name;
      if (version != null) state.schemaVersions[id] = version;
    }
  }
  if (state.schemaIds.length && !state.schemaIds.includes(state.schemaId)) {
    state.schemaId = state.schemaIds[0];
  }
  if (!state.schemaIds.length) state.schemaId = null;
}

/**
 * Laedt die Namen von Prozessen nach, die seit dem Anmelden entstanden sind.
 *
 * ``loadSchemas`` holt Namen nur beim Laden der Liste; ein Prozess, den jemand
 * anderes spaeter freigab, stand in „Meine Aufgaben“ und im Monitoring bis zum
 * Neuladen als ``schema_245 (v1)``. Die Sichten, die ueber mehrere Prozesse
 * reichen, rufen dies vor dem Zeichnen mit den ids auf, die sie anzeigen.
 * @param {string[]} ids anzuzeigende Schema-ids
 * @returns {Promise<void>}
 */
async function ensureSchemaNames(ids) {
  const missing = [...new Set(ids)].filter((id) => id && !(id in state.schemaNames));
  if (!missing.length) return;
  await Promise.all(missing.map(async (id) => {
    try {
      const s = await api.get(`/schemas/${id}`);
      state.schemaNames[id] = s.name;
      state.schemaVersions[id] = s.version;
    } catch (_e) { /* nicht lesbar: dann bleibt die id stehen */ }
  }));
}

async function refreshSchema() {
  if (!state.schemaId) { state.schema = null; state.validation = null; state.hints = []; return; }
  state.schema = await api.get(`/schemas/${state.schemaId}`);
  state.validation = await api.get(`/schemas/${state.schemaId}/validation`);
  // Modellhinweise sind rein beratend – ein Fehler hier darf das Modellieren
  // nie blockieren (best-effort, leer statt Fehlermeldung).
  try {
    const report = await api.get(`/schemas/${state.schemaId}/metrics`);
    state.hints = (report && report.hints) || [];
  } catch (err) { state.hints = []; }
  // Migrationsassistent: nur fuer freigegebene Revisionen und nur fuer Rollen,
  // die migrieren duerfen. Best effort wie die Hinweise -- ohne Bericht fehlt
  // nur der Knopf, das Modellieren bleibt unberuehrt.
  state.migrationReport = null;
  if (state.schema && state.schema.lifecycle_state === "RELEASED" && hasRole("operator", "modeler", "admin")) {
    try { state.migrationReport = await api.get(`/schemas/${state.schemaId}/migration-report`); }
    catch (err) { state.migrationReport = null; }
  }
  localStorage.setItem("schemaId", state.schemaId);
}

/**
 * Fuehrt EINE Modell-Operation am Kern aus und zeigt ihr Ergebnis an.
 *
 * Das gemeinsame Geruest fast aller Bindungs-, Knoten- und Organisations-
 * aenderungen: Aufruf absetzen, bei Erfolg das Schema neu laden
 * (``refreshSchema``), neu zeichnen und eine Erfolgsmeldung zeigen; bei einer
 * Ablehnung (422/409/403) die Meldung des Kerns ueber ``toastError``. Ob die
 * Aenderung zulaessig ist, entscheidet allein der Kern (Validate-before-Commit)
 * -- der Client prueft hier nichts.
 *
 * Der Rueckgabewert passt direkt auf den Bestaetigen-Rueckruf von
 * ``openModal``: ``false`` laesst den Dialog nach einer Ablehnung offen, damit
 * die Eingabe korrigiert werden kann; ``true`` schliesst ihn.
 *
 * @param {function(): Promise<*>} call Setzt den API-Aufruf ab. Als Funktion
 *   uebergeben, damit ein Fehler erst hier im ``try`` entsteht.
 * @param {string} okTitle Titel der Erfolgsmeldung.
 * @param {string[]} [okLines] Optionale Detailzeilen der Erfolgsmeldung.
 * @returns {Promise<boolean>} ``true`` nach Erfolg, ``false`` nach einem
 *   Fehler (auch wenn erst das Neuladen des Schemas scheitert).
 */
async function commitSchemaChange(call, okTitle, okLines) {
  try {
    await call();
    await refreshSchema(); render(); toast("ok", okTitle, okLines);
    return true;
  } catch (err) {
    toastError(err);
    return false;
  }
}

async function selectSchema(id) {
  state.schemaId = id;
  state.selectedNode = null;
  await refreshSchema();
  render();
}

/**
 * Ein soeben erzeugtes Schema oeffnen -- in der Modellieren-Sicht.
 *
 * Anlegen, BPMN-Import und "Aus Vorlage" sind von jeder Sicht aus erreichbar
 * (Schema-Umschalter in der Kopfzeile). Nur zu waehlen liess die Ansicht dort
 * stehen, wo man gerade war (z. B. "Meine Aufgaben"), und das neue Modell blieb
 * unsichtbar.
 * @param {string} id Schema-ID
 */
async function openCreatedSchema(id) {
  state.view = "model";
  setActiveNav();
  await selectSchema(id);
}

/**
 * Leerzustand der Modellieren-Sicht (beide Oberflaechen): Umlaute statt
 * „ausgewaehlt“ und gleich die naechsten Schritte als Knoepfe. „Beispieldaten
 * laden“ nur fuer Admins und nur, wenn das System noch **leer** ist -- das
 * Laden loescht vorher alles (``confirmReset`` fragt zusaetzlich nach).
 * @returns {HTMLElement}
 */
function noSchemaState() {
  const box = emptyState(state.schemaIds.length
    ? "Kein Schema ausgew\u00E4hlt. Oben ein Schema w\u00E4hlen oder ein neues anlegen."
    : "Noch kein Prozess vorhanden.");
  const actions = el("div", { class: "btn-row", style: "justify-content:center;margin-top:10px" },
    hasRole("modeler", "admin")
      ? el("button", { class: "btn small primary", onClick: newSchema }, "Neuer Prozess")
      : null,
    hasRole("modeler", "admin")
      ? el("button", { class: "btn small ghost", onClick: newFromTemplate }, "Aus Vorlage")
      : null,
    hasRole("admin") && !state.schemaIds.length
      ? el("button", { class: "btn small ghost", onClick: () => confirmReset("demo") }, "Beispieldaten laden")
      : null);
  box.appendChild(actions);
  return box;
}

function activitiesOf(schema) {
  return Object.values(schema.nodes).filter((n) => n.type === NODE_TYPE.ACTIVITY);
}
function isDraft(schema) { return schema && schema.lifecycle_state === "ENTWURF"; }

function lifecyclePill(schema) {
  const s = schema.lifecycle_state;
  const cls = s === "RELEASED" ? "pill-green" : s === "ENTWURF" ? "pill-amber" : "pill-gray";
  return el("span", { class: "pill " + cls, title: s }, lifecycleLabel(s));
}

// --------------------------------------------------------------------------
// Topbar / Schema-Picker
// --------------------------------------------------------------------------

// Human-readable schema caption including its revision, e.g. "Urlaubsantrag
// (v2)". Revisions share the same name but get a fresh id and an incremented
// version, so the version is what makes them distinguishable. ``version`` may
// be passed explicitly (e.g. an instance's own schema_version, which is robust
// even when the schema is no longer in the picker); otherwise it falls back to
// the cached version for that id. The id is used as a last resort.
function schemaLabel(id, version) {
  const name = state.schemaNames[id] || id;
  const v = version != null ? version : state.schemaVersions[id];
  return v != null ? `${name} (v${v})` : name;
}

function renderSchemaPicker() {
  const picker = byId("schema-picker");
  clear(picker);
  const select = el("select", { onChange: (e) => selectSchema(e.target.value) },
    ...state.schemaIds.map((id) => {
      const o = el("option", { value: id }, schemaLabel(id));
      if (id === state.schemaId) o.selected = true;
      return o;
    }));
  if (!state.schemaIds.length) {
    select.appendChild(el("option", null, "(kein Schema)"));
    select.disabled = true;
  }
  picker.appendChild(select);
  // Anlegen, Vorlage und Import nur fuer Modellierer/Admin -- ein Bearbeiter
  // bekam nach dem Klick nur „forbidden“. Der Server bleibt
  // maßgeblich; das hier blendet nur aus, was ohnehin abgelehnt wuerde.
  if (!hasRole("modeler", "admin")) return;
  picker.appendChild(el("button", { class: "btn small", onClick: newSchema }, "+ Neu"));
  picker.appendChild(el("button", { class: "btn small ghost", onClick: newFromTemplate,
    title: "Neues Schema aus einer Vorlage erstellen" }, "Aus Vorlage"));
  picker.appendChild(el("button", { class: "btn small ghost", onClick: importBpmn }, "BPMN-Import"));
}

async function newSchema() {
  const nameInput = el("input", { type: "text", placeholder: "z. B. Urlaubsantrag" });
  openModal("Neues Schema", el("label", { class: "field" }, "Name", nameInput), async () => {
    const name = nameInput.value.trim();
    if (!name) return false;
    try {
      const schema = await api.post("/schemas", { name });
      await loadSchemas();
      await openCreatedSchema(schema.id);
      toast("ok", "Schema angelegt", [schema.name]);
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * BPMN-2.0-Import: Datei waehlen oder XML einfuegen.
 *
 * Die Datei wird nur im Browser gelesen (FileReader) und landet im selben
 * Textfeld -- derselbe Endpunkt, dieselbe Pruefung im Kern, keine neue
 * Serverflaeche. Der Name wird aus dem Dateinamen
 * vorbelegt, solange er noch leer ist.
 */
async function importBpmn() {
  const ta = el("textarea", { placeholder: "<bpmn:definitions ...>" });
  const nameInput = el("input", { type: "text", placeholder: "optionaler Name" });
  const fileInput = el("input", { type: "file", class: "bpmn-file", accept: ".bpmn,.xml,application/xml,text/xml" });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      ta.value = String(reader.result || "");
      if (!nameInput.value.trim()) nameInput.value = file.name.replace(/\.(bpmn|xml)$/i, "");
    };
    reader.onerror = () => toast("err", "Datei konnte nicht gelesen werden", [file.name]);
    reader.readAsText(file);
  });
  openModal("BPMN 2.0 importieren", el("div", { class: "row", style: "flex-direction:column;align-items:stretch" },
    el("label", { class: "field" }, "BPMN-Datei (.bpmn, .xml)", fileInput),
    el("label", { class: "field" }, "Name (optional)", nameInput),
    el("label", { class: "field" }, "BPMN-XML (aus der Datei oder eingefügt)", ta)), async () => {
    const xml = ta.value.trim();
    if (!xml) return false;
    try {
      const schema = await api.post("/bpmn-import", { xml, name: nameInput.value.trim() || null });
      await loadSchemas();
      await openCreatedSchema(schema.id);
      toast("ok", "BPMN importiert", [schema.name]);
    } catch (err) { toastError(err); return false; }
  }, "Importieren");
}

// --------------------------------------------------------------------------
// Prozessvorlagen (Templates): eingebaute Bibliothek + eigene Vorlagen
// --------------------------------------------------------------------------

// Gallery to start a new schema from a template. Fetches the merged list
// (built-in + user templates), groups it by category and lets the modeller
// instantiate one -- the server deep-copies the blueprint into a fresh draft.
async function newFromTemplate() {
  let templates;
  try {
    templates = await api.get("/templates");
  } catch (err) { toastError(err); return; }
  if (!templates.length) {
    toast("info", "Keine Vorlagen", ["Es sind noch keine Vorlagen vorhanden."]);
    return;
  }

  // One card per template; the selected card is remembered for the confirm.
  const selection = { id: null, name: null };
  const cards = templates.map((t) => {
    const badge = t.origin === "BUILTIN"
      ? el("span", { class: "pill pill-blue" }, "Vorlage")
      : el("span", { class: "pill pill-green" }, "Eigene");
    const card = el("button", { class: "tpl-card", "data-id": t.id, onClick: () => {
      selection.id = t.id; selection.name = t.name;
      // Ueber die ganze Galerie: eigene Vorlagen stecken in einer Huelle (.tpl-item).
      [...gallery.querySelectorAll(".tpl-card")].forEach((c) => c.classList.toggle("selected", c === card));
    } },
      el("div", { class: "tpl-card-h" },
        el("strong", null, t.name),
        badge),
      t.category ? el("div", { class: "tpl-cat" }, t.category + (t.step_count ? ` · ${t.step_count} Schritte` : "")) : null,
      el("div", { class: "tpl-desc" }, t.description || "—"),
      // Wer macht was: aus den Bearbeiterregeln des Bauplans abgeleitet (Kern:
      // templates.template_roles) -- passt damit immer zum Inhalt der Vorlage.
      (t.roles || []).length
        ? el("ul", { class: "tpl-roles" }, ...t.roles.map((r) =>
            el("li", null, el("strong", null, r.name), ": " + r.steps.join(", "))))
        : null);
    // Eigene Vorlagen lassen sich wieder loeschen (Modellierer-Anleitung §1a);
    // eingebaute gehoeren zum Produkt. Eigener Knopf unter der Karte -- die
    // Karte selbst ist schon ein Knopf (Auswahl).
    if (t.origin === "BUILTIN" || !hasRole("modeler", "admin")) return card;
    return el("div", { class: "tpl-item" }, card,
      el("button", { class: "btn small ghost tpl-del", type: "button",
        title: "Eigene Vorlage löschen", onClick: () => confirmTemplateDelete(t) }, "Löschen"));
  });
  const gallery = el("div", { class: "tpl-gallery" }, ...cards);

  openModal("Aus Vorlage erstellen", gallery, async () => {
    if (!selection.id) { toast("info", "Keine Vorlage gew\u00E4hlt", ["Bitte eine Vorlage ausw\u00E4hlen."]); return false; }
    try {
      const schema = await api.post(`/templates/${encodeURIComponent(selection.id)}/instantiate`,
        { name: selection.name });
      await loadSchemas();
      await openCreatedSchema(schema.id);
      toast("ok", "Aus Vorlage erstellt", [schema.name]);
    } catch (err) { toastError(err); return false; }
  }, "Erstellen");
}

/**
 * Rueckfrage und Loeschen einer eigenen Vorlage, danach wieder die Galerie.
 *
 * Der Kern lehnt eingebaute Vorlagen ab (422) und verlangt Modellierer/Admin;
 * Prozesse, die schon aus der Vorlage entstanden sind, bleiben unberuehrt.
 *
 * @param {{id: string, name: string}} t die Vorlage
 */
function confirmTemplateDelete(t) {
  openModal(`Vorlage l\u00F6schen: ${t.name}`,
    el("p", { class: "muted" },
      "Die Vorlage verschwindet aus der Galerie. Prozesse, die schon aus ihr erstellt wurden, bleiben unver\u00E4ndert."),
    async () => {
      try {
        await api.del(`/templates/${encodeURIComponent(t.id)}`);
        toast("ok", "Vorlage gel\u00F6scht", [t.name]);
        // Galerie ersetzt diesen Dialog; false, sonst schloesse openModal sie.
        newFromTemplate();
        return false;
      } catch (err) { toastError(err); return false; }
    }, "L\u00F6schen", { danger: true });
}

// Capture the current schema as a reusable *user* template. The server stores a
// self-contained, validated blueprint; only correct schemas can be captured.
async function saveAsTemplate() {
  const schema = state.schema;
  if (!schema) return;
  const nameInput = el("input", { type: "text", value: schema.name + " (Vorlage)" });
  const catInput = el("input", { type: "text", placeholder: "z. B. Personal, Einkauf, IT" });
  const descInput = el("textarea", { placeholder: "Kurzbeschreibung der Vorlage" });
  openModal("Als Vorlage speichern", el("div", { class: "row", style: "flex-direction:column;align-items:stretch" },
    el("label", { class: "field" }, "Name", nameInput),
    el("label", { class: "field" }, "Kategorie (optional)", catInput),
    el("label", { class: "field" }, "Beschreibung (optional)", descInput)), async () => {
    const name = nameInput.value.trim();
    if (!name) return false;
    try {
      const tpl = await api.post("/templates", {
        schema_id: schema.id, name,
        category: catInput.value.trim(), description: descInput.value.trim(),
      });
      toast("ok", "Als Vorlage gespeichert", [tpl.name]);
    } catch (err) { toastError(err); return false; }
  }, "Speichern");
}

// --------------------------------------------------------------------------
// View: Modellieren
// --------------------------------------------------------------------------

/**
 * Modellieren-Sicht: waehlt zwischen den beiden gleichwertigen Oberflaechen.
 *
 * * ``card``    -- Kontrollfluss als Arbeitsflaeche, alles zum gewaehlten
 *                  Schritt in einer Karte daneben (Standard).
 * * ``classic`` -- die urspruengliche Zwei-Spalten-Sicht mit Knoten-Inspektor
 *                  und Bindungs-Palette rechts.
 *
 * Beide laufen ueber **dieselben** API-Aufrufe; die Wahl ist reine Darstellung
 * und wird pro Browser gemerkt (localStorage ``modelUx``).
 */
function viewModel() {
  return modelUx() === "classic" ? viewModelClassic() : viewModelCard();
}

/** Aktive Modellier-Oberflaeche ("card" | "classic"); Default ist "card". */
function modelUx() {
  return state.modelUx === "classic" ? "classic" : "card";
}

/** Schaltet zwischen Karten- und klassischer Oberflaeche um (persistiert). */
function setModelUx(ux) {
  state.modelUx = ux === "classic" ? "classic" : "card";
  localStorage.setItem("modelUx", state.modelUx);
  // Vollbild ist an die Karten-Sicht gewoehnt (dort ist die Karte ein Overlay);
  // beim Wechsel zurueckfallen, damit die klassische Sicht nie ohne ihre
  // rechte Spalte dasteht.
  state.graphMaximized = false;
  render();
}

/**
 * Umschalter zwischen den beiden Modellier-Oberflaechen (Kopfzeile).
 *
 * @returns {HTMLElement} Der Knopf.
 */
function modelUxToggle() {
  const card = modelUx() === "card";
  return el("button", {
    class: "btn small ghost",
    title: card
      ? "Zur klassischen Ansicht wechseln (Knoten-Inspektor und Bindungs-Palette rechts)"
      : "Zur Karten-Ansicht wechseln (Kontrollfluss über die volle Breite, alles am gewählten Schritt)",
    onClick: () => setModelUx(card ? "classic" : "card"),
  }, card ? "▤ Klassische Ansicht" : "▦ Karten-Ansicht");
}

/**
 * Gemeinsame Kopfzeile beider Modellier-Oberflaechen (Name, Zustand, Aktionen).
 *
 * @param {object} schema Das aktuelle Schema.
 * @param {boolean} draft Ob es sich um einen Entwurf handelt.
 * @returns {HTMLElement} Das Kopf-Panel.
 */
function modelHeader(schema, draft) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, schema.name),
      el("span", { class: "sub" }, `v${schema.version}`),
      lifecyclePill(schema),
      validationBadge(),
      el("span", { class: "spacer", style: "flex:1" }),
      draft
        ? el("button", { class: "btn small ghost", onClick: setProcessDeadline,
            title: "Harte Frist für den gesamten Prozess (T2 – kritischer Pfad ≤ Termin)" },
            schema.deadline_seconds != null ? "Termin: " + formatDuration(schema.deadline_seconds) : "Termin …")
        : null,
      el("button", { class: "btn small ghost", onClick: exportBpmn }, "BPMN-Export"),
      hasRole("modeler", "admin")
        ? el("button", { class: "btn small ghost", onClick: saveAsTemplate,
            title: "Dieses Schema als wiederverwendbare Vorlage speichern" }, "Als Vorlage")
        : null,
      libraryToggleButton(schema),
      // Beim freigegebenen Schema fehlte in der Kopfzeile jeder Weg zurueck ins
      // Bearbeiten -- dort stand nur \u201EZur Ausfuehrung". Der Revisions-Knopf
      // steht deshalb daneben (dieselbe Operation wie im Panel unten).
      !draft && hasRole("modeler", "admin")
        ? el("button", { class: "btn small ghost", onClick: newRevision,
            title: "Bearbeitbare Entwurfskopie dieser Revision anlegen" }, "Neue Revision")
        : null,
      migrationHeaderButton(schema, draft),
      draft
        // Gruen nur, wenn freigabereif -- sonst sah der Knopf nach
        // "fertig" aus, obwohl Schritte ohne Bearbeiter die Freigabe verhindern.
        ? el("button", {
            class: "btn small" + (isReleasable(schema) ? " green" : ""),
            title: isReleasable(schema) ? "Schema freigeben (danach unver\u00E4nderlich)"
              : modelUx() === "classic"
                ? "Noch nicht freigabereif \u2013 siehe Panel \u201EKorrektheit\u201C"
                : "Noch nicht freigabereif \u2013 siehe Statusleiste",
            "data-tour": "model.release", onClick: releaseSchema }, "Freigeben")
        : el("button", { class: "btn small primary", onClick: () => { state.view = "run"; setActiveNav(); render(); } }, "Zur Ausf\u00FChrung"),
      draft && hasRole("modeler", "admin")
        ? el("button", { class: "btn small", onClick: startTestInstance, title: "Test-Instanz dieses Entwurfs starten und im 4-Quadranten-Cockpit durchspielen" }, "\u2697 Pr\u00FCfinstanz")
        : null,
      modelUxToggle()));
}

/**
 * Modellieren im Kontrollfluss: der Graph nutzt die volle Breite, die
 * Schritt-Karte erscheint als Overlay am gewaehlten Schritt.
 */
function viewModelCard() {
  const content = byId("content");
  // Der Fokus im Bezeichnungsfeld der Schritt-Karte muss VOR dem Leeren
  // gesichert werden: die Sicht baut ihr DOM bei jedem Rendern komplett neu auf,
  // und ein Hintergrund-Rendern (Tour-Tick) mitten im Tippen wuerde sonst Fokus
  // und Schreibmarke verschlucken.
  const nameFocus = captureCardNameFocus();
  clear(content);
  if (!state.schema) {
    content.appendChild(noSchemaState());
    return;
  }
  const schema = state.schema;
  const draft = isDraft(schema);
  const header = modelHeader(schema, draft);

  // Datenherkunft-Ueberlagerung: ein gezielt hervorgehobenes Element hat
  // Vorrang, sonst die Lesestellen des gewaehlten Knotens.
  const provenance = draft || state.selectedNode || state.dataElemFocus
    ? computeProvenance(schema, { selectedNode: state.selectedNode, dataElemFocus: state.dataElemFocus })
    : [];

  const graph = renderGraph(schema, {
    onPlus: draft ? openInsertModal : null,
    selectedId: state.selectedNode,
    onSelectNode: (id) => { state.selectedNode = id; state.cardFocusSection = null; render(); },
    // Ein Klick auf ein Knoten-Badge oeffnet den passenden Abschnitt der
    // Schritt-Karte -- am Element, statt in eine andere Sicht zu wechseln.
    // Der Weg in die Vollansicht bleibt ueber die Karte erhalten.
    onOpenData: (id) => openCardSection(id, "data"),
    onOpenStaff: (id) => openCardSection(id, "staff"),
    onNodeAction: draft ? nodeRingAction : null,
    findings: findingsByNode(),
    onFinding: (id) => openCardSection(id, "findings"),
    provenance,
  });

  // Kontrollfluss-Panel mit Maximieren-Knopf oben rechts. Im Vollbild wird das
  // Panel per CSS-Klasse ``graph-max`` zu einem bildschirmfuellenden Overlay
  // (zum gemeinsamen Diskutieren); der Knopf schaltet dann auf "verlassen".
  const maxBtn = el("button", {
    class: "btn small ghost graph-max-btn",
    title: state.graphMaximized ? "Vollbild verlassen (Esc)" : "Kontrollfluss maximieren",
    onClick: () => { state.graphMaximized = !state.graphMaximized; render(); },
  }, state.graphMaximized ? "× Verkleinern" : "⛶ Vollbild");
  // Der Kontrollfluss ist die Arbeitsflaeche: er nutzt die volle Breite, die
  // Schritt-Karte liegt als Overlay darueber und erscheint nur bei Auswahl.
  const graphBody = el("div", { class: "panel-b graph-body" }, graph);
  const card = stepCard(schema, draft);
  if (card) graphBody.appendChild(card);
  const empty = canvasEmptyState(schema, draft);
  if (empty) graphBody.appendChild(empty);
  const cfPanel = el("div", { class: "panel graph-panel model-canvas" + (state.graphMaximized ? " graph-max" : "") },
    el("div", { class: "panel-h" },
      el("h2", null, "Kontrollfluss"),
      el("span", { class: "spacer", style: "flex:1" }),
      maxBtn),
    graphBody,
    modelStatusBar(schema, draft));

  content.appendChild(header);
  content.appendChild(cfPanel);

  // Nach dem (Neu-)Rendern den ausgewaehlten Knoten in die Mitte der scrollbaren
  // Canvas ruecken, statt nach links auf den Start zurueckzuspringen -- und dabei
  // die gestrichelte Datenherkunft des Knotens mit ins Bild holen. Ohne diesen
  // zweiten Bezugspunkt zentrierte die Canvas allein auf den Leseknoten und schob
  // die Schreibseite (und damit den ganzen Bogen) aus dem sichtbaren Ausschnitt:
  // die Pfeile waren gezeichnet, aber nicht zu sehen.
  const focusPos = state.selectedNode ? layoutSchema(schema).pos[state.selectedNode] : null;
  if (focusPos || graph._provBounds) {
    requestAnimationFrame(() => {
      reserveCardWidth(graph, card);
      centerCanvasOnNode(graph, focusPos, graph._provBounds);
    });
  } else {
    requestAnimationFrame(() => reserveCardWidth(graph, card));
  }
  applyCardFocus(content, nameFocus);
}

/**
 * Meldet der Canvas, wie viel Breite die Schritt-Karte rechts belegt.
 *
 * Erst nach dem Einhaengen ins Dokument steht die tatsaechliche Breite fest
 * (sie haengt am CSS). Deckt die Karte fast die ganze Canvas ab, liegt die
 * mobile Bodensheet-Darstellung vor -- dann wird **keine** seitliche Reserve
 * gesetzt, weil die Karte dort unten und nicht rechts liegt.
 *
 * @param {HTMLElement} wrap Die Canvas (.canvas-wrap) mit ihrem Pan/Zoom-Controller.
 * @param {HTMLElement|null} card Die Schritt-Karte, falls sichtbar.
 */
function reserveCardWidth(wrap, card) {
  if (!wrap || !wrap._panzoom || !wrap._panzoom.setReserve) return;
  if (!card || !card.getBoundingClientRect) { wrap._panzoom.setReserve(0); return; }
  const cw = card.getBoundingClientRect().width;
  const vw = wrap.clientWidth || 0;
  wrap._panzoom.setReserve(vw && cw > vw * 0.75 ? 0 : cw + 12);
}

/**
 * Statusleiste unter dem Kontrollfluss: Freigabe-Ampel, modellweite Befunde
 * (die ohne Knotenbezug, z. B. T2 kritischer Pfad) und der Hinweistext.
 *
 * Ersetzt das frühere Panel „Korrektheit" der rechten Spalte. Knotenbezogene
 * Befunde stehen jetzt **am Knoten** (siehe renderNodeFindingMark) und in der
 * Schritt-Karte -- hier bleibt, was zu keinem einzelnen Schritt gehoert.
 *
 * @param {object} schema Das aktuelle Schema.
 * @param {boolean} draft Ob das Schema im Entwurf ist.
 * @returns {HTMLElement} Die Leiste.
 */
function modelStatusBar(schema, draft) {
  const v = state.validation;
  const globals = globalFindings();
  const bar = el("div", { class: "model-status", "data-tour": "model.findings" });
  bar.appendChild(v && v.correct
    ? el("span", { class: "pill pill-green" }, "✓ korrekt")
    : el("span", { class: "pill pill-red" }, countLabel(v ? v.findings.length : 0, "Befund", "Befunde")));
  // Stufe B getrennt von Stufe A: „korrekt" ist eine Invariante, „freigabereif"
  // ein Reifegrad. Ein Entwurf darf unfertig sein -- er soll es nur sehen, bevor
  // er freigibt und der Vorgang spaeter beim Sachbearbeiter stillsteht.
  const notReady = draft ? releaseFindings() : [];
  if (notReady.length) {
    bar.appendChild(el("span", {
      class: "pill pill-amber",
      title: notReady.map((f) => findingText(f)).join("\n"),
    }, `${countLabel(notReady.length, "Schritt", "Schritte")} ohne Bearbeiter`));
  }
  // Modellhinweise (G-Gruppe): rein beratend, dritter Zustand neben „korrekt"
  // und „freigabereif" – bewusst neutral (grau), nie rot/amber, damit ein
  // Hinweis nicht wie ein Befund wirkt. Details im Tooltip; in der klassischen
  // Sicht listet das Korrektheits-Panel dieselben Hinweise aus.
  const hints = draft ? (state.hints || []) : [];
  if (hints.length) {
    bar.appendChild(el("span", {
      class: "pill pill-gray",
      title: hints.map((h) => `${h.code}: ${hintText(h, schema)}`).join("\n"),
    }, countLabel(hints.length, "Hinweis", "Hinweise")));
  }
  const focusElem = state.dataElemFocus && schema.data_elements[state.dataElemFocus];
  const text = focusElem
    ? el("span", null,
        "Datenherkunft von „" + focusElem.name + "“: gestrichelte Linien zeigen, wo geschrieben → wo gelesen wird. ",
        el("a", { href: "#", onClick: (e) => { e.preventDefault(); state.dataElemFocus = null; render(); } }, "Hervorhebung löschen"))
    : el("span", null, draft
      ? "Einen Schritt anklicken: die Karte daneben trägt alles, was an ihm getan werden kann. „+“ an einer Kante oder am Schritt fügt einen weiteren ein. Unzulässiges weist der Kern ab."
      : "Schema ist freigegeben und damit unveränderlich. Einen Schritt anklicken zeigt seine Bindungen und – gestrichelt – woher seine gelesenen Daten stammen. Zum Bearbeiten eine neue Revision anlegen.");
  bar.appendChild(el("span", { class: "model-status-txt" }, text));
  // Die Tastaturwege findet ohne Hinweis niemand; deshalb stehen sie
  // hier, wo sie beim Modellieren im Blick sind (mobil ausgeblendet).
  bar.appendChild(el("span", { class: "model-status-key",
    title: "Pfeiltasten bewegen die Auswahl im Kontrollfluss (← → entlang des Ablaufs, ↑ ↓ zwischen Zweigen), Enter springt in die Karte, Esc hebt die Auswahl auf" },
    "← → Schritt · ↑ ↓ Zweig · ↵ Karte · Esc abwählen"));
  globals.forEach((f) => bar.appendChild(
    el("span", { class: "model-status-find", title: findingText(f, { withHint: true }) },
      el("span", { class: "rule" }, f.rule), findingText(f))));
  return bar;
}

/**
 * Einstiegskarte fuer einen noch leeren Prozess (nur Start → Ende).
 *
 * Der Weg ist derselbe wie sonst (``openInsertModal`` auf der einzigen Kante),
 * nur auffindbar: Vor der Umstellung stand hier eine leere Flaeche mit einem
 * kleinen „+“ auf der einzigen Verbindungslinie.
 *
 * @param {object} schema Das Schema.
 * @param {boolean} draft Ob bearbeitet werden darf.
 * @returns {HTMLElement|null} Die Karte, oder null (Prozess ist nicht leer).
 */
function canvasEmptyState(schema, draft) {
  if (!draft || activitiesOf(schema).length) return null;
  const start = Object.values(schema.nodes || {}).find((n) => n.type === NODE_TYPE.START);
  if (!start) return null;
  return el("div", { class: "canvas-empty" },
    el("div", { class: "canvas-empty-t" }, "Der Prozess ist noch leer"),
    el("div", { class: "canvas-empty-b" },
      "Zwischen Start und Ende passiert bisher nichts. Der erste Schritt legt fest, womit der Vorgang beginnt."),
    el("button", { class: "btn primary", onClick: () => openInsertModal(start.id) }, "Ersten Schritt anlegen"));
}

// --------------------------------------------------------------------------
// Modellieren (klassisch): zwei Spalten -- Kontrollfluss links, Knoten-
// Inspektor + Bindungs-Palette + Korrektheit + Schema-Evolution rechts.
//
// Bleibt bewusst **gleichwertig neben** der Karten-Sicht bestehen (Umschalter
// in der Kopfzeile): Beide bedienen dieselben API-Aufrufe, tragen also keine
// eigene Korrektheitslogik, und wer die gewohnte Palette bevorzugt, verliert
// nichts. Aenderungen an einer Bindungs-/Knoten-Operation wirken automatisch in
// beiden Sichten, weil sie dieselben Funktionen aufrufen.
// --------------------------------------------------------------------------

function viewModelClassic() {
  const content = byId("content");
  clear(content);
  if (!state.schema) {
    content.appendChild(noSchemaState());
    return;
  }
  const schema = state.schema;
  const draft = isDraft(schema);
  const header = modelHeader(schema, draft);

  // Datenherkunft-Ueberlagerung: ein in der Palette gewaehltes Element hat
  // Vorrang, sonst die Lesestellen des gewaehlten Knotens.
  const provenance = draft || state.selectedNode || state.dataElemFocus
    ? computeProvenance(schema, { selectedNode: state.selectedNode, dataElemFocus: state.dataElemFocus })
    : [];

  const graph = renderGraph(schema, {
    onPlus: draft ? openInsertModal : null,
    selectedId: state.selectedNode,
    onSelectNode: (id) => { state.selectedNode = id; render(); },
    onOpenData: (id) => focusBindingView("data", id),
    onOpenStaff: (id) => focusBindingView("org", id),
    provenance,
  });

  const focusElem = state.dataElemFocus && schema.data_elements[state.dataElemFocus];
  const hint = el("div", { class: "panel-b muted", style: "font-size:12px" },
    focusElem
      ? el("span", null,
          "Datenherkunft von „" + focusElem.name + "“: gestrichelte Linien zeigen, wo geschrieben → wo gelesen wird. ",
          el("a", { href: "#", onClick: (e) => { e.preventDefault(); state.dataElemFocus = null; render(); } }, "Hervorhebung löschen"))
      : draft
        ? "Geführtes Modellieren: „+“ an einer Kante fügt einen Schritt ein. Einen Schritt anklicken, dann rechts unter „Binden“ ein Datenelement/eine Ressource mit ⊕ zuweisen. Ein angeklickter Schritt zeigt außerdem gestrichelt, woher seine gelesenen Daten stammen. Unzulässiges weist der Kern ab."
        : "Schema ist freigegeben und damit unveränderlich. Einen Schritt anklicken zeigt gestrichelt, woher seine gelesenen Daten stammen; ein Klick auf ein Datenelement rechts zeigt alle seine Lesestellen. Zum Bearbeiten über „Neue Revision“ eine Entwurfskopie anlegen oder Instanzen starten.");

  // Kontrollfluss-Panel mit Maximieren-Knopf oben rechts. Im Vollbild wird das
  // Panel per CSS-Klasse ``graph-max`` zu einem bildschirmfuellenden Overlay
  // (zum gemeinsamen Diskutieren); der Knopf schaltet dann auf "verlassen".
  const maxBtn = el("button", {
    class: "btn small ghost graph-max-btn",
    title: state.graphMaximized ? "Vollbild verlassen (Esc)" : "Kontrollfluss maximieren",
    onClick: () => { state.graphMaximized = !state.graphMaximized; render(); },
  }, state.graphMaximized ? "× Verkleinern" : "⛶ Vollbild");
  const cfPanel = el("div", { class: "panel graph-panel" + (state.graphMaximized ? " graph-max" : "") },
    el("div", { class: "panel-h" },
      el("h2", null, "Kontrollfluss"),
      el("span", { class: "spacer", style: "flex:1" }),
      maxBtn),
    el("div", { class: "panel-b graph-body" }, graph),
    hint);

  content.appendChild(header);
  content.appendChild(el("div", { class: "grid-2 model-top" },
    cfPanel,
    el("div", null, nodeInspectorPanel(), bindingPalette(schema, draft), findingsPanel(), revisionPanel())));

  // Nach dem (Neu-)Rendern den ausgewaehlten Knoten in die Mitte der scrollbaren
  // Canvas ruecken, statt nach links auf den Start zurueckzuspringen -- und dabei
  // die gestrichelte Datenherkunft des Knotens mit ins Bild holen. Ohne diesen
  // zweiten Bezugspunkt zentrierte die Canvas allein auf den Leseknoten und schob
  // die Schreibseite (und damit den ganzen Bogen) aus dem sichtbaren Ausschnitt:
  // die Pfeile waren gezeichnet, aber nicht zu sehen.
  const focusPos = state.selectedNode ? layoutSchema(schema).pos[state.selectedNode] : null;
  if (focusPos || graph._provBounds) {
    requestAnimationFrame(() => centerCanvasOnNode(graph, focusPos, graph._provBounds));
  }
}

// --------------------------------------------------------------------------
// Modellieren-Seite: rechte Bindungs-Palette (Datenelemente + Ressourcen).
// Klick-zu-Binden (Safari-robust) statt Drag&Drop: zuerst links einen Schritt
// waehlen, dann in der Liste "An Schritt binden" (⊕) klicken. (Native HTML5-
// Drop-Events auf SVG-Knoten sind in Safari unzuverlaessig; Klicken ist stabil.)
// --------------------------------------------------------------------------

// Baustein: ein Paletten-Chip (eine Listenzeile) mit Klick (z. B. Herkunft
// hervorheben) und rechtsbuendigen Aktionen (Binden/Bearbeiten). Rein
// praesentational -- veraendert nie Modell oder Backend.
function paletteChip(opts) {
  const chip = el("div", {
    class: "pal-chip" + (opts.active ? " active" : ""),
    title: opts.title || "",
    onClick: opts.onClick || null,
  });
  chip.appendChild(el("span", { class: "pal-chip-label" }, opts.label));
  if (opts.sub) chip.appendChild(el("span", { class: "pal-chip-sub" }, opts.sub));
  chip.appendChild(el("span", { class: "spacer", style: "flex:1" }));
  (opts.actions || []).forEach((a) => chip.appendChild(a));
  return chip;
}

// Kleiner Icon-Knopf innerhalb eines Chips (Bearbeiten). ``stopPropagation``
// verhindert, dass der Klick den Chip selbst (Herkunft-Fokus) ausloest.
function chipAction(symbol, title, onClick) {
  return el("button", {
    class: "pal-chip-act", title,
    onClick: (e) => { e.stopPropagation(); onClick(); },
  }, symbol);
}

// Rechte Bindungs-Palette mit Tabs (Datenelemente | Ressourcen). Zeigt oben den
// aktuellen Bindungsziel-Schritt und bindet per Klick auf ⊕. Anlegen/Bearbeiten
// laeuft ueber dieselben Dialoge wie die Daten-/Ressourcensicht.
function bindingPalette(schema, draft) {
  const target = bindTarget();
  const tab = state.paletteTab === "res" ? "res" : "data";
  const selNode = state.selectedNode ? schema.nodes[state.selectedNode] : null;
  const subText = !draft ? "nur Ansicht (Schema freigegeben)"
    : target ? "Ziel: „" + nodeCaption(schema.nodes[target]) + "“"
      : selNode ? "gewählter Knoten ist kein Aktivitätsschritt"
        : "zuerst links einen Schritt wählen";
  const tabBtn = (id, label) => el("button", {
    class: "pal-tab" + (tab === id ? " active" : ""),
    "data-tour": "model.tab." + id,
    onClick: () => { state.paletteTab = id; render(); },
  }, label);
  const head = el("div", { class: "panel-h" },
    el("h2", null, "Binden"),
    el("span", { class: "sub" }, subText),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small ghost", title: "Vollansicht öffnen",
      onClick: () => focusBindingView(tab === "res" ? "org" : "data", null) }, "Vollansicht"));
  const tabs = el("div", { class: "pal-tabs" }, tabBtn("data", "Datenelemente"), tabBtn("res", "Ressourcen"));
  const body = el("div", { class: "panel-b pal-body" },
    tab === "res" ? resourcePaletteTab(schema, draft, target) : dataPaletteTab(schema, draft, target));
  // data-tour: Die Tour muss die GANZE Palette freilassen koennen -- der Tab
  // allein reicht nicht, die Bindung passiert ueber das ⊕ im Koerper darunter.
  return el("div", { class: "panel pal-panel", "data-tour": "model.palette" }, head, tabs, body);
}

// Aktueller Bindungsziel-Knoten: der gewaehlte Schritt, sofern Entwurf UND eine
// ACTIVITY (nur daran lassen sich Daten-/Bearbeiterbindungen setzen).
function bindTarget() {
  if (!isDraft(state.schema) || !state.selectedNode) return null;
  const n = state.schema.nodes[state.selectedNode];
  return n && n.type === NODE_TYPE.ACTIVITY ? n.id : null;
}

// "An Schritt binden"-Knopf (⊕). Deaktiviert, solange kein Aktivitätsschritt
// gewaehlt ist; ein Klick erklaert dann, was zu tun ist.
function bindButton(enabled, onBind) {
  return el("button", {
    class: "pal-bind" + (enabled ? "" : " disabled"),
    title: enabled ? "An gewählten Schritt binden" : "Zuerst links einen Schritt (Aktivität) wählen",
    onClick: (e) => {
      e.stopPropagation();
      if (enabled) onBind();
      else toast("info", "Zuerst links einen Schritt (Aktivität) wählen");
    },
  }, "⊕");
}

// Tab „Datenelemente": Liste mit Typ/Quelle. ⊕ bindet an den gewaehlten Schritt
// (Richtung/Pflicht im Dialog), ✎ bearbeitet, Klick hebt die Herkunft hervor.
function dataPaletteTab(schema, draft, target) {
  const elems = Object.values(schema.data_elements || {});
  const box = el("div");
  box.appendChild(el("div", { class: "pal-group-h" },
    el("span", null, "Datenelemente"),
    el("button", { class: "btn small", onClick: addDataElement, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Datenelement")));
  if (!elems.length) {
    box.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Noch keine Datenelemente – mit „+ Datenelement“ eines mit sprechendem Namen anlegen."));
    return box;
  }
  const wrap = el("div", { class: "pal-chips" });
  elems.forEach((d) => {
    const actions = [];
    if (draft) actions.push(bindButton(!!target, () => dropDataElementOnNode(target, { element_id: d.id, name: d.name })));
    if (draft) actions.push(chipAction("✎", "Datenelement bearbeiten", () => editDataElement(d)));
    wrap.appendChild(paletteChip({
      label: d.name,
      sub: typeName(d.data_type) + (d.source === "EXTERNAL" ? " · extern" : ""),
      title: `${d.name} (${typeName(d.data_type)}) – klicken zeigt die Herkunft im Graph`,
      active: state.dataElemFocus === d.id,
      onClick: () => { state.dataElemFocus = state.dataElemFocus === d.id ? null : d.id; render(); },
      actions,
    }));
  });
  box.appendChild(wrap);
  return box;
}

// Tab „Ressourcen": Rollen, Abteilungen und Agenten mit ⊕ (Bearbeiterzuordnung
// an den gewaehlten Schritt). Die BZR kann an eine Rolle/Abteilung oder – fuer
// die explizite Auswahl – an einen konkreten Agenten binden.
function resourcePaletteTab(schema, draft, target) {
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };
  const orgEditable = draft || !!schema.org_model_id;
  const box = el("div");
  const group = (title, addBtn, chipsWrap) => el("div", { class: "pal-group" },
    el("div", { class: "pal-group-h" }, el("span", null, title), addBtn || null), chipsWrap);

  const roles = Object.values(org.roles || {});
  const roleChips = el("div", { class: "pal-chips" });
  if (!roles.length) roleChips.appendChild(el("span", { class: "muted", style: "font-size:12px" }, "keine"));
  roles.forEach((r) => roleChips.appendChild(paletteChip({
    label: r.name, sub: "Rolle", title: `Rolle ${r.name}`,
    actions: draft ? [bindButton(!!target, () => dropResourceOnNode(target, { rkind: "ROLE", ref: r.id, name: r.name }))] : [],
  })));
  const addRoleBtn = el("button", { class: "btn small", disabled: !orgEditable, onClick: addRole }, "+ Rolle");

  const units = Object.values(org.org_units || {});
  const unitChips = el("div", { class: "pal-chips" });
  if (!units.length) unitChips.appendChild(el("span", { class: "muted", style: "font-size:12px" }, "keine"));
  units.forEach((u) => {
    const actions = [];
    if (draft) actions.push(bindButton(!!target, () => dropResourceOnNode(target, { rkind: "ORG_UNIT", ref: u.id, name: u.name })));
    if (orgEditable) actions.push(chipAction("✎", "Vorgesetzten der Abteilung setzen", () => editManager(u)));
    unitChips.appendChild(paletteChip({ label: u.name, sub: "Abteilung", title: `Abteilung ${u.name}`, actions }));
  });
  const addUnitBtn = el("button", { class: "btn small", disabled: !orgEditable, onClick: () => addChildOrgUnit(null) }, "+ Abteilung");

  const agents = Object.values(org.agents || {});
  const agentChips = el("div", { class: "pal-chips" });
  if (!agents.length) agentChips.appendChild(el("span", { class: "muted", style: "font-size:12px" }, "keine"));
  agents.forEach((a) => {
    const roleNames = (a.role_ids || []).map((id) => ((org.roles || {})[id] || {}).name).filter(Boolean).join(", ");
    const actions = [];
    if (draft) actions.push(bindButton(!!target, () => dropResourceOnNode(target, { rkind: "AGENT", ref: a.id, name: a.name })));
    if (orgEditable) actions.push(chipAction("✎", "Agent bearbeiten", () => editAgent(a)));
    agentChips.appendChild(paletteChip({
      label: a.name, sub: roleNames || "ohne Rolle", title: `Agent ${a.name}${roleNames ? " – " + roleNames : ""}`,
      actions,
    }));
  });
  const addAgentBtn = el("button", { class: "btn small", disabled: !orgEditable || !roles.length, onClick: addAgent }, "+ Agent");

  box.appendChild(group("Rollen", addRoleBtn, roleChips));
  box.appendChild(group("Abteilungen", addUnitBtn, unitChips));
  box.appendChild(group("Agenten", addAgentBtn, agentChips));
  return box;
}

// --------------------------------------------------------------------------
// Modellieren im Kontrollfluss: die Schritt-Karte
//
// Alles, was an einem Schritt getan werden kann, steht in EINER Karte, die zum
// gewaehlten Schritt gehoert und mit ihm verschwindet. Sie traegt -- wie die
// klassische Sicht -- keine Korrektheitslogik: jeder Knopf ruft dieselbe
// Funktion und damit denselben Endpunkt wie zuvor, der Kern validiert vor dem
// Commit.
// --------------------------------------------------------------------------

// Abschnitte, die ohne eigene Wahl aufgeklappt sind: das, was der Kern fuer die
// Freigabe braucht. Alles Weitere (Maske, Zeit, Mail, Klassifikation) bleibt
// eingeklappt, bis es gebraucht wird.
const CARD_DEFAULT_OPEN = ["findings", "flow", "data", "staff", "service"];

/**
 * Liest die aufgeklappten Abschnitte (localStorage), mit robustem Rueckfall.
 *
 * @returns {string[]} Ids der aufgeklappten Abschnitte.
 */
function readCardOpen() {
  if (Array.isArray(state.cardOpen)) return state.cardOpen;
  try {
    const raw = JSON.parse(localStorage.getItem("cardOpen") || "null");
    state.cardOpen = Array.isArray(raw) ? raw : CARD_DEFAULT_OPEN.slice();
  } catch (_e) {
    state.cardOpen = CARD_DEFAULT_OPEN.slice();
  }
  return state.cardOpen;
}

/** Ist der Abschnitt ``id`` aufgeklappt? */
function cardSecOpen(id) { return readCardOpen().includes(id); }

/** Merkt die aufgeklappten Abschnitte im Browser. */
function persistCardOpen() {
  try { localStorage.setItem("cardOpen", JSON.stringify(readCardOpen())); } catch (_e) { /* Privatmodus */ }
}

/** Klappt einen Abschnitt auf/zu und merkt die Wahl. */
function toggleCardSec(id) {
  const open = readCardOpen();
  state.cardOpen = open.includes(id) ? open.filter((x) => x !== id) : open.concat([id]);
  persistCardOpen();
  render();
}

/**
 * Waehlt einen Schritt und oeffnet gezielt einen Abschnitt seiner Karte.
 *
 * Einstiegspunkt fuer die Knoten-Badges, den Befund-Marker und den Schnellring:
 * Statt in eine andere Sicht zu wechseln, springt die Bedienung an genau die
 * Stelle der Karte, die gemeint ist.
 *
 * @param {string|null} nodeId Zu waehlender Knoten (null = Auswahl behalten).
 * @param {string} sec Abschnitts-Id (siehe CARD_DEFAULT_OPEN) oder "name".
 */
function openCardSection(nodeId, sec) {
  if (nodeId) state.selectedNode = nodeId;
  if (sec && sec !== "name" && !cardSecOpen(sec)) {
    state.cardOpen = readCardOpen().concat([sec]);
    persistCardOpen();
  }
  state.cardFocusSection = sec;
  render();
}

/**
 * Aktion des Schnellrings am Knoten (siehe renderNodeRing).
 *
 * Jede Aktion fuehrt auf einen bereits bestehenden Weg -- „danach einfuegen"
 * ist derselbe Dialog wie das „+" auf der Kante, nur von einem anderen
 * Ausloeser.
 *
 * @param {string} nodeId Der Knoten am Ring.
 * @param {string} key Aktions-Schluessel aus nodeRingActions.
 */
function nodeRingAction(nodeId, key) {
  if (key === "insert") { openInsertModal(nodeId); return; }
  if (key === "rename") { openCardSection(nodeId, "name"); return; }
  if (key === "bind") { openCardSection(nodeId, "data"); return; }
  if (key === "delete") { deleteNode(nodeId); return; }
  if (key === "empty-branch") { removeEmptyBranch(nodeId); }
}

// Pfeiltaste -> Himmelsrichtung fuer die Tastaturnavigation im Kontrollfluss.
const ARROW_DIRS = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };

/**
 * Nachbarknoten in einer Himmelsrichtung bestimmen -- die Grundlage der
 * Tastaturnavigation im Kontrollfluss (Stufe U4).
 *
 * Rein rechnend und layout-agnostisch: gearbeitet wird auf den Kastenmitten,
 * die das Layout liefert, nicht auf der Kantenliste. Dadurch funktioniert die
 * Navigation auch im gestapelten Rueckfall-Layout und erreicht auch Knoten, die
 * zwar sichtbar nebeneinander liegen, aber nicht direkt verbunden sind -- etwa
 * die Geschwister-Aeste einer Verzweigung, um die es bei ``↑``/``↓`` gerade
 * geht.
 *
 * Vorgehen: Kandidaten sind alle Knoten in der gewuenschten Richtung. Gewaehlt
 * wird zuerst nach dem Abstand auf der Bewegungsachse (das Spine-Layout legt
 * Knoten auf ein diskretes Raster aus Spalten und Bahnen, „naechste Spalte"
 * bzw. „naechste Bahn" ist also wohldefiniert) und erst innerhalb dieses
 * Abstands nach dem geringsten Versatz auf der Querachse. ``→`` folgt damit dem
 * Ablauf und steigt an einem Split in den Ast ab, statt den ganzen Block zu
 * ueberspringen.
 *
 * ``↑``/``↓`` bekommen zusaetzlich eine Schranke: es zaehlen nur Knoten, die
 * sich mit dem aktuellen **waagerecht ueberdecken**. Die Taste wechselt so die
 * Bahn an Ort und Stelle (Geschwister-Ast), und ohne Nachbarbahn passiert
 * nichts -- ohne diese Schranke landete man vom Spine aus in einem Zweig weit
 * hinter oder vor der aktuellen Stelle, weil die naechste Bahn nach oben dort
 * ueberall verlaeuft.
 *
 * @param {object} layout Ergebnis von layoutSchema (``{pos: {id: {x,y,w,h}}}``).
 * @param {string|null} fromId Aktuell gewaehlter Knoten. Ohne Auswahl (oder bei
 *   einer Auswahl, die es nicht mehr gibt) liefert jede Richtung den
 *   Einstiegsknoten links oben -- in einem wohlgeformten Modell der Start.
 * @param {"left"|"right"|"up"|"down"} dir Bewegungsrichtung.
 * @returns {string|null} Id des Nachbarn, oder null, wenn es in der Richtung
 *   keinen gibt (dann bleibt die Auswahl, wo sie ist).
 */
function graphNeighbor(layout, fromId, dir) {
  const pos = (layout && layout.pos) || {};
  const ids = Object.keys(pos);
  if (!ids.length) return null;
  const cx = (id) => pos[id].x + pos[id].w / 2;
  const cy = (id) => pos[id].y + pos[id].h / 2;
  if (!fromId || !pos[fromId]) {
    return ids.slice().sort((a, b) => (cx(a) - cx(b)) || (cy(a) - cy(b)))[0];
  }
  const vertical = dir === "up" || dir === "down";
  const forward = dir === "right" || dir === "down" ? 1 : -1;
  const main = vertical ? cy : cx;    // Achse, auf der bewegt wird
  const cross = vertical ? cx : cy;   // Achse, die den Gleichstand bricht
  // Knoten derselben Spalte/Bahn liegen exakt gleich; EPS faengt nur
  // Rundungsreste ab und haelt „gleiche Spalte" von „naechste Spalte" getrennt.
  const EPS = 1;
  const base = main(fromId), baseCross = cross(fromId);
  let best = null;
  ids.forEach((id) => {
    if (id === fromId) return;
    const step = (main(id) - base) * forward;
    if (step <= EPS) return;                       // liegt nicht in Bewegungsrichtung
    const off = Math.abs(cross(id) - baseCross);
    // Bahnwechsel nur senkrecht ueber-/untereinander (siehe oben).
    if (vertical && off >= (pos[fromId].w + pos[id].w) / 2 - EPS) return;
    if (!best || step < best.step - EPS || (step - best.step <= EPS && off < best.off)) {
      best = { id, step, off };
    }
  });
  return best ? best.id : null;
}

/**
 * Auswahl im Kontrollfluss per Tastatur bewegen (Stufe U4).
 *
 * Gemeinsame Operation **beider** Modellier-Oberflaechen (beide gleichwertig): sie
 * setzt nur ``state.selectedNode``; die jeweilige Sicht zeichnet sich neu und
 * rueckt den Knoten ueber ``centerCanvasOnNode`` wieder ins Bild -- inklusive
 * des Kartenrands, damit die Schritt-Karte den frisch gewaehlten Knoten nicht
 * verdeckt.
 *
 * @param {"left"|"right"|"up"|"down"} dir Bewegungsrichtung.
 * @returns {boolean} true, wenn sich die Auswahl geaendert hat (dann darf der
 *   Aufrufer die Taste als verbraucht behandeln und das Scrollen unterdruecken).
 */
function moveSelection(dir) {
  const schema = state.schema;
  if (!schema || !Object.keys(schema.nodes || {}).length) return false;
  const next = graphNeighbor(layoutSchema(schema), state.selectedNode, dir);
  if (!next || next === state.selectedNode) return false;
  state.selectedNode = next;
  state.cardFocusSection = null;   // neuer Schritt -> Karte oben beginnen
  render();
  return true;
}

/**
 * Fokus aus dem Kontrollfluss in die Schritt-Karte holen (Enter).
 *
 * Bevorzugt das Bezeichnungsfeld (der haeufigste naechste Handgriff), sonst das
 * erste Bedienelement der Karte. Ohne Karte -- klassische Sicht, keine Auswahl
 * -- passiert nichts, die Taste bleibt dann beim Browser.
 *
 * @returns {boolean} true, wenn ein Element den Fokus bekommen hat.
 */
function focusStepCard() {
  // Karte (Karten-Sicht) bzw. Knoten-Inspektor (klassische Sicht): Enter fuehrt
  // in beiden Oberflaechen in das Bearbeitungsfeld des gewaehlten Schritts.
  const card = document.querySelector(".step-card") || document.querySelector(".node-inspector");
  if (!card) return false;
  const name = byId("card-name-input") || byId("insp-name-input");
  if (name && name.focus) { name.focus(); if (name.select) name.select(); return true; }
  const first = card.querySelector("button, input, select, textarea, a[href]");
  if (first && first.focus) { first.focus(); return true; }
  return false;
}

/**
 * Ob die Taste gerade einem Eingabefeld gehoert.
 *
 * Trennt Tastenwege der Oberflaeche vom Tippen: waehrend in einem Feld
 * geschrieben wird, darf weder Escape die Karte wegraeumen noch eine Pfeiltaste
 * die Schreibmarke stehlen.
 *
 * @returns {boolean} true, wenn der Fokus in einem Text-/Auswahlfeld steht.
 */
function isTypingTarget() {
  const a = document.activeElement;
  if (!a) return false;
  if (a.isContentEditable) return true;
  const tag = a.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * Sichert Fokus und Schreibmarke des Bezeichnungsfeldes vor einem Neuaufbau.
 *
 * @returns {{nodeId: string, start: number, end: number}|null} Merker oder null.
 */
function captureCardNameFocus() {
  const inp = byId("card-name-input");
  if (!inp || document.activeElement !== inp) return null;
  return {
    nodeId: state.selectedNode,
    start: inp.selectionStart != null ? inp.selectionStart : inp.value.length,
    end: inp.selectionEnd != null ? inp.selectionEnd : inp.value.length,
  };
}

/**
 * Stellt nach dem Neuaufbau Fokus/Schreibmarke wieder her und rollt einen
 * gezielt geoeffneten Abschnitt in den sichtbaren Bereich der Karte.
 *
 * @param {HTMLElement} content Der neu aufgebaute Inhaltsbereich.
 * @param {{nodeId: string, start: number, end: number}|null} nameFocus Merker.
 */
function applyCardFocus(content, nameFocus) {
  const wanted = state.cardFocusSection;
  state.cardFocusSection = null;
  const focusName = (nameFocus && nameFocus.nodeId === state.selectedNode) || wanted === "name";
  if (focusName) {
    const inp = byId("card-name-input");
    if (inp) {
      inp.focus();
      if (nameFocus && nameFocus.nodeId === state.selectedNode) {
        try { inp.setSelectionRange(nameFocus.start, nameFocus.end); } catch (_e) { /* kein Textfeld */ }
      } else {
        inp.select();
      }
    }
  }
  if (wanted && wanted !== "name") {
    const sec = content.querySelector('.card-sec[data-sec="' + wanted + '"]');
    if (sec && sec.scrollIntoView) requestAnimationFrame(() => sec.scrollIntoView({ block: "nearest" }));
  }
}

/**
 * Ein aufklappbarer Abschnitt der Schritt-Karte.
 *
 * @param {string} id Abschnitts-Id (Merker fuer auf-/zugeklappt).
 * @param {string} title Ueberschrift.
 * @param {string|null} summary Kurzstand rechts in der Kopfzeile (z. B. "2").
 * @param {function(HTMLElement): void} fill Baut den Koerper (nur wenn offen).
 * @param {string|null} anchor Optionaler ``data-tour``-Anker fuer die Tour.
 * @returns {HTMLElement} Der Abschnitt.
 */
function cardSection(id, title, summary, fill, anchor) {
  const open = cardSecOpen(id);
  const head = el("button", {
    class: "card-sec-h" + (open ? " open" : ""),
    type: "button",
    onClick: () => toggleCardSec(id),
    title: open ? "Abschnitt zuklappen" : "Abschnitt aufklappen",
  },
    el("span", { class: "card-sec-caret" }, open ? "▾" : "▸"),
    el("span", { class: "card-sec-t" }, title),
    el("span", { class: "spacer", style: "flex:1" }),
    summary ? el("span", { class: "card-sec-sum" }, summary) : null);
  if (anchor) head.setAttribute("data-tour", anchor);
  const sec = el("section", { class: "card-sec", "data-sec": id }, head);
  if (open) {
    const body = el("div", { class: "card-sec-b" });
    fill(body);
    sec.appendChild(body);
  }
  return sec;
}

/**
 * Freigabe-Ampel eines Schritts -- eine **Anzeige**, keine Pruefung.
 *
 * Rot = der Kern beanstandet diesen Knoten, gelb = keine Beanstandung, aber
 * etwas fuer die Freigabe Noetiges fehlt noch, gruen = aus Sicht der Karte
 * vollstaendig. Verbindlich ist ausschliesslich der Kern; die gelbe Stufe ist
 * bewusst nur ein Hinweis und blockiert nie eine Aktion.
 *
 * @param {object} schema Das Schema.
 * @param {object} node Der Knoten.
 * @param {Array} findings Befunde dieses Knotens.
 * @returns {{cls: string, text: string}|null} Ampel oder null (kein Arbeitsschritt).
 */
function stepReadiness(schema, node, findings) {
  if (node.type !== NODE_TYPE.ACTIVITY && node.type !== NODE_TYPE.SUBPROCESS) return null;
  if (findings.length) return { cls: "red", text: countLabel(findings.length, "Befund", "Befunde") + " des Kerns an diesem Schritt" };
  const missing = [];
  if (node.type === NODE_TYPE.ACTIVITY) {
    const sb = (schema.service_bindings || {})[node.id];
    if (!sb) missing.push("Dienst");
    // Ein automatischer Schritt braucht keinen Bearbeiter.
    if (!(schema.staff_rules || {})[node.id] && !(sb && sb.automatic)) missing.push("Bearbeiter");
  } else if (!(schema.sub_process_bindings || {})[node.id]) {
    missing.push("Submodell");
  }
  return missing.length
    ? { cls: "amber", text: "Für die Freigabe fehlt noch: " + missing.join(", ") }
    : { cls: "green", text: "Aus Sicht dieser Karte vollständig" };
}

/**
 * Die Schritt-Karte des gewaehlten Knotens (Overlay ueber dem Kontrollfluss).
 *
 * @param {object} schema Das aktuelle Schema.
 * @param {boolean} draft Ob bearbeitet werden darf (ENTWURF).
 * @returns {HTMLElement|null} Die Karte, oder null ohne Auswahl.
 */
function stepCard(schema, draft) {
  const node = state.selectedNode ? schema.nodes[state.selectedNode] : null;
  if (!node) return null;
  const findings = findingsByNode()[node.id] || [];
  const card = el("aside", {
    class: "step-card",
    "data-tour": "model.palette",
    "aria-label": "Schritt " + nodeCaption(node),
  });
  card.appendChild(stepCardHead(schema, node, draft, findings));
  const body = el("div", { class: "step-card-b" });
  if (findings.length) {
    body.appendChild(cardSection("findings", "Befunde", String(findings.length), (b) => {
      findings.forEach((f) => b.appendChild(el("div", { class: "finding" },
        el("span", { class: "rule" }, f.rule), el("span", null, findingText(f, { withHint: true })))));
    }));
  }
  if (!draft) {
    body.appendChild(el("div", { class: "card-note" },
      "Schema ist freigegeben – zum Bearbeiten eine neue Revision anlegen (Knoten-IDs bleiben erhalten)."));
    body.appendChild(newRevisionAction());
  }
  const isStep = node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS;
  if (isStep) {
    if (draft) body.appendChild(cardSection("flow", "Ablauf", null, (b) => cardFlowSection(b, schema, node)));
    body.appendChild(cardSection("data", "Daten", cardDataSummary(schema, node),
      (b) => cardDataSection(b, schema, node, draft), "model.tab.data"));
    if (node.type === NODE_TYPE.ACTIVITY) {
      body.appendChild(cardSection("staff", "Bearbeiter", null,
        (b) => cardStaffSection(b, schema, node, draft), "model.tab.res"));
      body.appendChild(cardSection("service", "Dienst", null, (b) => cardServiceSection(b, schema, node, draft)));
      body.appendChild(cardSection("sync", "Synchronisation", null, (b) => syncBlock(b, schema, node, draft)));
      body.appendChild(cardSection("form", "Eingabemaske", null, (b) => cardFormSection(b, schema, node, draft)));
    } else {
      body.appendChild(cardSection("sub", "Submodell", null, (b) => cardSubprocessSection(b, schema, node, draft)));
    }
    body.appendChild(cardSection("time", "Zeit & Priorität", null, (b) => cardTimeSection(b, schema, node, draft)));
    if (node.type === NODE_TYPE.ACTIVITY) {
      body.appendChild(cardSection("mail", "Benachrichtigung", null, (b) => cardMailSection(b, schema, node, draft)));
    }
    body.appendChild(cardSection("class", "Klassifikation", null, (b) => cardValueClassSection(b, schema, node, draft)));
  } else if (SPLIT_TYPES.has(node.type)) {
    body.appendChild(cardSection("branches", "Verzweigung", null, (b) => cardBranchSection(b, schema, node, draft)));
  } else if (LOOP_TYPES.has(node.type)) {
    body.appendChild(cardSection("loop", "Schleife", null,
      (b) => b.appendChild(loopNodePanel(schema, node, draft))));
  } else {
    body.appendChild(el("div", { class: "card-note" },
      node.type === NODE_TYPE.AND_JOIN || node.type === NODE_TYPE.XOR_JOIN
        ? "Join-Knoten werden über ihren öffnenden Split entfernt."
        : "Start und Ende sind fester Bestandteil des Modells."));
  }
  card.appendChild(body);
  return card;
}

/** Kopf der Schritt-Karte: Typ, Bezeichnung (bearbeitbar), Ampel, Schliessen. */
function stepCardHead(schema, node, draft, findings) {
  const head = el("div", { class: "step-card-h" });
  head.appendChild(el("span", { class: "pill pill-gray", title: node.type }, nodeTypeLabel(node.type)));
  const renamable = draft && (node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS);
  if (renamable) {
    const input = el("input", { type: "text", id: "card-name-input", value: node.label || "",
      title: "Bezeichnung – Enter übernimmt" });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") renameNode(node.id, input.value); });
    head.appendChild(input);
    head.appendChild(el("button", { class: "btn small", title: "Bezeichnung übernehmen",
      onClick: () => renameNode(node.id, input.value) }, "✓"));
  } else {
    head.appendChild(el("strong", { class: "step-card-name" }, nodeCaption(node)));
  }
  const ready = stepReadiness(schema, node, findings);
  if (ready) head.appendChild(el("span", { class: "step-dot step-dot-" + ready.cls, title: ready.text }));
  head.appendChild(el("button", { class: "step-card-x", title: "Auswahl aufheben (Esc)",
    onClick: () => { state.selectedNode = null; render(); } }, "✕"));
  return head;
}

/** Abschnitt „Ablauf": Schritt danach einfuegen, entfernen, umwandeln. */
function cardFlowSection(body, schema, node) {
  body.appendChild(el("div", { class: "card-hint" },
    "Neue Schritte entstehen immer relativ zu einem bestehenden – so kann kein loses Ende entstehen."));
  const movable = node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS;
  const row = el("div", { class: "row", style: "gap:8px" },
    el("button", { class: "btn small primary", onClick: () => openInsertModal(node.id) }, "+ Schritt danach"),
    movable ? el("button", { class: "btn small", title: "Schritt an eine andere Stelle des Ablaufs verschieben – alle Bindungen bleiben erhalten",
      onClick: () => moveNodeDialog(node.id) }, "Verschieben…") : null,
    el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Entfernen"));
  body.appendChild(row);
  if (node.type === NODE_TYPE.ACTIVITY) {
    body.appendChild(el("div", { class: "card-hint", style: "margin-top:10px" },
      "Wiederverwendung: diesen Schritt durch ein freigegebenes Submodell aus der Bibliothek ersetzen – inkl. Datenübergabe."));
    body.appendChild(el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "convert") },
      "In Subprozess umwandeln"));
  }
}

/** Kurzstand des Daten-Abschnitts (Anzahl Bindungen). */
function cardDataSummary(schema, node) {
  const n = (schema.data_accesses || []).filter((a) => a.node_id === node.id).length;
  return n ? String(n) : null;
}

/** Abschnitt „Daten": bestehende Bindungen loesen, neue direkt hier binden. */
function cardDataSection(body, schema, node, draft) {
  const accesses = (schema.data_accesses || []).filter((a) => a.node_id === node.id);
  if (accesses.length) {
    const list = el("div", { class: "insp-binds" });
    accesses.forEach((a) => {
      const e = schema.data_elements[a.element_id];
      const name = e ? e.name : a.element_id;
      list.appendChild(el("div", { class: "insp-bind" },
        el("button", { class: "insp-bind-name link-like", title: "Herkunft dieses Elements im Kontrollfluss zeigen",
          onClick: () => { state.dataElemFocus = state.dataElemFocus === a.element_id ? null : a.element_id; render(); } }, name),
        el("span", { class: "insp-bind-mode mode-" + a.mode, title: a.mode }, accessModeLabel(a.mode)),
        a.mandatory ? null : el("span", { class: "muted", style: "font-size:11px" }, "optional"),
        el("span", { class: "spacer", style: "flex:1" }),
        draft
          ? el("button", { class: "insp-bind-del", title: "Bindung lösen",
              onClick: () => removeDataAccess(node.id, a.element_id, a.mode, name) }, "✕")
          : null));
    });
    body.appendChild(list);
  } else {
    body.appendChild(el("div", { class: "card-hint" },
      "Dieser Schritt liest und schreibt bisher nichts. Gebunden wird hier – Richtung und Pflicht legst du im Dialog fest."));
  }
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" });
  if (draft) {
    row.appendChild(el("button", { class: "btn small primary", onClick: () => bindDataDialog(node.id) },
      "⊕ Datenelement binden"));
  }
  row.appendChild(el("button", { class: "btn small ghost", title: "Alle Datenbindungen des Modells in der Datensicht",
    onClick: () => focusBindingView("data", node.id) }, "Vollansicht"));
  body.appendChild(row);
}

/** Abschnitt „Bearbeiter": die BZR des Schritts (Z1–Z4). */
function cardStaffSection(body, schema, node, draft) {
  const rule = (schema.staff_rules || {})[node.id];
  const sb = (schema.service_bindings || {})[node.id];
  body.appendChild(el("div", { class: rule ? "" : "card-hint" },
    rule
      ? describeRule(rule)
      : sb && sb.automatic
        ? "Automatischer Schritt – er wird ohne Bearbeiter ausgeführt."
        : "Noch niemandem zugeordnet. Ohne Zuordnung landet die Aufgabe in keiner Arbeitsliste."));
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" });
  if (draft) {
    row.appendChild(el("button", { class: "btn small primary", onClick: () => bindStaffDialog(node.id) },
      rule ? "⊕ Zuordnung ändern" : "⊕ Bearbeiter zuordnen"));
    if (rule) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeStaffRule(node.id) }, "Entfernen"));
  }
  row.appendChild(el("button", { class: "btn small ghost", title: "Organisationsmodell in der Ressourcensicht bearbeiten",
    onClick: () => focusBindingView("org", node.id) }, "Organisation"));
  body.appendChild(row);
}

/** Abschnitt „Dienst" (A1–A3): was der Schritt ausfuehrt. */
/**
 * Hinweis fuer Schritte ohne Dienst -- geteilt von Schritt-Karte und klassischem
 * Inspektor, damit beide Oberflaechen dasselbe sagen. Ein Dienst ist optional:
 * die Freigabe (Stufe B) verlangt bei interaktiven Schritten nur eine
 * Bearbeiterzuordnung (B2); B1 wird bewusst NICHT erzwungen (siehe
 * validator.check_executable). Frueherer Text behauptete das Gegenteil
 * (bis 1.17.0).
 */
const SERVICE_OPTIONAL_HINT =
  "Kein Dienst – optional. Für die Freigabe braucht ein interaktiver Schritt nur eine Bearbeiterzuordnung (B2); einen Dienst braucht nur ein automatischer Schritt.";

function cardServiceSection(body, schema, node, draft) {
  const sb = (schema.service_bindings || {})[node.id];
  if (sb) {
    const tmpl = sb.template_id ? (schema.activity_templates || {})[sb.template_id] : null;
    body.appendChild(el("div", { style: "font-size:13px" },
      el("strong", null, sb.name), " · ", sb.automatic ? "automatisch" : "interaktiv",
      tmpl ? ` · Template „${tmpl.name}“` : ""));
    if (sb.automatic && sb.automation && sb.automation !== "MANUAL_NONE") {
      body.appendChild(el("div", { class: "muted", style: "font-size:11px" },
        "Automatik: " + (AUTOMATION_LABELS[sb.automation] || sb.automation)));
    }
  } else {
    body.appendChild(el("div", { class: "card-hint" },
      SERVICE_OPTIONAL_HINT));
  }
  if (!draft) return;
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small" + (sb ? "" : " primary"), onClick: () => assignServiceFor(node.id) },
      sb ? "Dienst ändern" : "Dienst zuweisen"));
  if (sb && sb.automatic) row.appendChild(el("button", { class: "btn small", onClick: () => editAutomation(node, sb) }, "Automatik…"));
  if (sb) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeService(node.id) }, "Entfernen"));
  body.appendChild(row);
}

/** Abschnitt „Eingabemaske" (U1–U2). */
function cardFormSection(body, schema, node, draft) {
  const form = schema.forms && schema.forms[node.id];
  body.appendChild(el("div", { class: form ? "" : "card-hint", style: "font-size:13px" },
    form
      ? `${countLabel(form.fields.length, "Feld", "Felder")}${form.title ? " – „" + form.title + "“" : ""}.`
      : "Noch keine Eingabemaske – Felder per Auswahl zusammenstellen."));
  if (!draft) return;
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small", onClick: () => openFormDesigner(node.id) },
      form ? "Maske bearbeiten" : "Eingabemaske gestalten"));
  if (form) row.appendChild(el("button", { class: "btn small danger", onClick: () => deleteForm(node.id) }, "Maske entfernen"));
  body.appendChild(row);
}

/** Abschnitt „Zeit & Priorität" (T1/T2 und die Arbeitslisten-Reihung). */
function cardTimeSection(body, schema, node, draft) {
  const tc = (schema.time_constraints || {})[node.id];
  const hasFrist = tc && tc.max_duration_seconds != null;
  body.appendChild(el("div", { class: "insp-h" }, "Frist (max. Dauer)"));
  body.appendChild(el("div", { class: hasFrist ? "" : "card-hint", style: "font-size:13px" },
    hasFrist ? formatDuration(tc.max_duration_seconds)
      : "keine – fließt bei gesetztem Prozess-Termin in die Terminprüfung (T2) ein."));
  if (tc && tc.target_lead_seconds != null) {
    body.appendChild(el("div", { style: "font-size:12px;margin-top:4px" },
      "Soll-Reaktionszeit ab Aktivierung: " + formatDuration(tc.target_lead_seconds)));
  }
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => setTimeConstraintFor(node.id, hasFrist ? tc : null) },
        hasFrist ? "Frist ändern" : "Frist setzen"),
      hasFrist ? el("button", { class: "btn small danger", onClick: () => removeTimeConstraint(node.id) }, "Entfernen") : null));
  }
  escalationBlock(body, schema, node, draft);
  const pr = (schema.node_priorities || {})[node.id];
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Priorität"));
  body.appendChild(el("div", { class: pr ? "" : "card-hint", style: "font-size:13px" },
    pr ? `Auswirkung ${IMPACT_LABELS[pr.impact] || pr.impact} · Dringlichkeit ${IMPACT_LABELS[pr.urgency] || pr.urgency}`
      : "normal (Standard)"));
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => setPriorityFor(node.id, pr) }, pr ? "Priorität ändern" : "Priorität setzen"),
      pr ? el("button", { class: "btn small danger", onClick: () => removePriority(node.id) }, "Entfernen") : null));
  }
}

/** Abschnitt „Benachrichtigung" (Regelgruppe N, opt-in je Aktivitaet). */
function cardMailSection(body, schema, node, draft) {
  const binding = (schema.mail_bindings || {})[node.id];
  const hasRule = !!(schema.staff_rules || {})[node.id];
  if (binding) {
    body.appendChild(el("div", { style: "font-size:13px" },
      MAIL_MODE_LABELS[binding.mode] || binding.mode,
      binding.mode === "TO_ELIGIBLE_AGENTS"
        ? (binding.include_deputies ? " · inkl. Vertreter" : " · ohne Vertreter") : ""));
    body.appendChild(el("div", { class: "muted", style: "font-size:11px;margin-top:2px" },
      "Betreff: " + (binding.subject || "–")));
  } else if (!hasRule) {
    // Abhaengigkeit erklaeren statt verstecken (N2) -- mit Sprungmarke auf den
    // Abschnitt, der das Fehlende beschafft.
    body.appendChild(el("div", { class: "card-hint" },
      "Erst einen Bearbeiter zuordnen – ohne Zuordnung gibt es keinen Empfänger (N2). ",
      el("a", { href: "#", onClick: (e) => { e.preventDefault(); openCardSection(node.id, "staff"); } },
        "Zum Abschnitt Bearbeiter")));
  } else {
    body.appendChild(el("div", { class: "card-hint" },
      "Aus – bei Aufgabeneingang wird keine Mail gesendet (Standard)."));
  }
  if (!draft) return;
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small", disabled: !hasRule && !binding,
      onClick: () => editMailBinding(node.id, binding) }, binding ? "Mail ändern" : "Mail senden…"));
  if (binding) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeMailBinding(node.id) }, "Entfernen"));
  body.appendChild(row);
}

/** Abschnitt „Klassifikation": Wertklasse (rein beratend, E3). */
function cardValueClassSection(body, schema, node, draft) {
  const sel = el("select", null,
    el("option", { value: "" }, "– keine –"),
    ...Object.entries(VALUE_CLASS_LABELS).map(([v, l]) => el("option", { value: v }, l)));
  sel.value = node.value_class || "";
  sel.disabled = !draft;
  sel.addEventListener("change", () => setValueClass(node.id, sel.value || null));
  body.appendChild(el("div", { class: "card-hint" },
    "Rein beratend – ohne Wirkung auf die Korrektheit oder die Ausführung."));
  body.appendChild(el("label", { class: "field", style: "font-size:12px;margin-top:6px" }, "Klassifikation", sel));
}

/** Abschnitt „Submodell" eines Subprozess-Knotens (H1–H4). */
function cardSubprocessSection(body, schema, node, draft) {
  const bnd = (schema.sub_process_bindings || {})[node.id];
  body.appendChild(el("div", { class: bnd ? "" : "card-hint", style: "font-size:13px" },
    bnd ? `Gebunden an Submodell „${bnd.target_schema_id}“ (v${bnd.target_version}).` : "Noch kein Submodell gebunden."));
  if (!draft) return;
  body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "rebind") },
      "Zuordnung / Datenübergabe ändern")));
}

/** Abschnitt „Verzweigung" eines Splits (K7-Zellen, leerer Zweig, entfernen). */
function cardBranchSection(body, schema, node, draft) {
  const branches = controlEdges(schema).filter((e) => e.source === node.id);
  const list = el("div", { class: "insp-binds" });
  branches.forEach((e) => {
    const target = schema.nodes[e.target];
    const empty = target && (target.type === NODE_TYPE.XOR_JOIN || target.type === NODE_TYPE.AND_JOIN);
    list.appendChild(el("div", { class: "insp-bind" },
      el("span", { class: "insp-bind-name" }, empty ? "leerer Zweig" : nodeCaption(target)),
      e.condition ? el("span", { class: "muted", style: "font-size:11px" }, conditionCaption(e.condition)) : null));
  });
  if (branches.length) body.appendChild(list);
  if (!draft) return;
  const emptyJoin = emptyBranchJoin(schema, node.id);
  if (emptyJoin) {
    body.appendChild(el("div", { class: "card-hint", style: "margin-top:8px" },
      "Ein Zweig ist leer – in ihm fällt keine Aktivität an. Entfernen löst bei nur noch einem verbleibenden Zweig die ganze Verzweigung auf."));
    body.appendChild(el("button", { class: "btn small", onClick: () => removeEmptyBranch(node.id) }, "Leeren Zweig entfernen"));
  }
  if (node.type === NODE_TYPE.AND_SPLIT) {
    body.appendChild(el("div", { class: "card-hint", style: "margin-top:8px" },
      "Querschritt: ein neuer Schritt als eigener Parallelzweig, per Sync-Kanten nach/vor gewählten Schritten (K4)."));
    body.appendChild(el("button", { class: "btn small", onClick: () => insertBetweenDialog() }, "Querschritt einfügen…"));
  }
  body.appendChild(el("div", { class: "card-hint", style: "margin-top:8px" },
    "Entfernen löscht den gesamten Block (Split, Zweige und passenden Join)."));
  body.appendChild(el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Verzweigung entfernen"));
}

// --------------------------------------------------------------------------
// Binden direkt am Schritt: Auswahl + Optionen in EINEM Dialog
// --------------------------------------------------------------------------

/**
 * Alle Knoten, die im Kontrollfluss **vor** ``nodeId`` liegen (Rueckwaerts-BFS).
 *
 * Dient nur der Sortierung/Beschriftung im Bindungsdialog: ein Datenelement,
 * das ein vorgelagerter Schritt schreibt, ist der uebliche Lese-Kandidat.
 * Verbindlich bleibt der Kern (D1–D4) -- diese Funktion entscheidet nichts.
 *
 * @param {object} schema Das Schema.
 * @param {string} nodeId Zielknoten.
 * @returns {Set<string>} Ids aller Vorgaenger (transitiv).
 */
function ancestorsOf(schema, nodeId) {
  const inEdges = {};
  controlEdges(schema).forEach((e) => { (inEdges[e.target] = inEdges[e.target] || []).push(e.source); });
  const seen = new Set();
  const queue = (inEdges[nodeId] || []).slice();
  while (queue.length) {
    const id = queue.pop();
    if (seen.has(id)) continue;
    seen.add(id);
    (inEdges[id] || []).forEach((p) => { if (!seen.has(p)) queue.push(p); });
  }
  return seen;
}

/**
 * Auswahlliste mit Suchfeld -- gemeinsamer Baustein der Bindungsdialoge.
 *
 * @param {Array<{id: string, label: string, sub: string, group: string}>} items Einträge.
 * @param {function(string): void} onPick Rueckmeldung der Auswahl (Element-Id).
 * @returns {{node: HTMLElement, get: function(): string|null}} Steuerelement.
 */
function pickList(items, onPick, initial) {
  // ``initial`` waehlt einen Eintrag vor (z. B. das soeben angelegte Element).
  let chosen = initial && items.some((it) => it.id === initial) ? initial
    : items.length ? items[0].id : null;
  const list = el("div", { class: "pick-list" });
  const search = el("input", { type: "text", placeholder: "Suchen …" });
  function build() {
    clear(list);
    const q = search.value.trim().toLowerCase();
    let group = null;
    const hits = items.filter((it) => !q
      || it.label.toLowerCase().includes(q) || (it.sub || "").toLowerCase().includes(q));
    if (!hits.length) {
      list.appendChild(el("div", { class: "muted", style: "font-size:12px;padding:8px" }, "Nichts gefunden."));
      return;
    }
    hits.forEach((it) => {
      if (it.group && it.group !== group) {
        group = it.group;
        list.appendChild(el("div", { class: "pick-group" }, group));
      }
      const row = el("button", {
        class: "pick-row" + (it.id === chosen ? " active" : ""),
        type: "button",
        onClick: () => { chosen = it.id; build(); if (onPick) onPick(chosen); },
      },
        el("span", { class: "pick-label" }, it.label),
        it.sub ? el("span", { class: "pick-sub" }, it.sub) : null);
      list.appendChild(row);
    });
  }
  search.addEventListener("input", build);
  build();
  return {
    node: el("div", { class: "pick" }, search, list),
    get: () => chosen,
  };
}

/**
 * Legt eine Datenbindung im Kern an -- der **einzige** Aufruf von
 * ``POST /schemas/{id}/data-access`` im Client.
 *
 * Alle Bindungswege (Schritt-Karte, Palette der klassischen Sicht, Datensicht,
 * das Merkmal einer neuen Verzweigung) laufen hierueber; der Kern prueft D1-D4.
 * Ein Waechter (test_every_data_binding_goes_through_one_function) haelt das so,
 * damit kein Weg wieder still auf „immer Pflicht“ oder englische Modi faellt.
 *
 * @param {string} schemaId Schema
 * @param {string} nodeId Schritt
 * @param {string} elementId Datenelement
 * @param {"READ"|"WRITE"|"READ_WRITE"} mode Richtung
 * @param {boolean} mandatory Pflichtbindung
 * @returns {Promise<object>} das geaenderte Schema (Fehler als Ausnahme)
 */
function createDataAccess(schemaId, nodeId, elementId, mode, mandatory) {
  return api.post(`/schemas/${schemaId}/data-access`, {
    node_id: nodeId, element_id: elementId, mode, mandatory,
  });
}

/**
 * Bindet ein Datenelement an einen Schritt -- Auswahl, Richtung und Pflicht in
 * einem Dialog, direkt am gewaehlten Schritt.
 *
 * Der Kern prueft die Bindung vor dem Commit (D1–D4): ein Pflichtlesen ohne
 * vorherige Schreibquelle wird mit HTTP 422 abgewiesen, das Modell bleibt
 * unveraendert und der Befund erscheint als Meldung.
 *
 * Vorgaben: Die Richtung steht auf „Schreiben“, solange kein
 * Schritt davor das gewaehlte Element setzt -- „Lesen“ fuehrte dort sicher zu
 * D1. Ueber „Neues Datenelement anlegen“ kommt man mit dem neuen Element
 * vorgewaehlt hierher zurueck (``preselect``).
 *
 * @param {string} nodeId Zielschritt.
 * @param {string} [preselect] vorzuwaehlendes Datenelement
 */
function bindDataDialog(nodeId, preselect) {
  const schema = state.schema;
  const node = schema.nodes[nodeId];
  const before = ancestorsOf(schema, nodeId);
  const writtenBefore = new Set();
  (schema.data_accesses || []).forEach((a) => {
    if (before.has(a.node_id) && (a.mode === "WRITE" || a.mode === "READ_WRITE")) writtenBefore.add(a.element_id);
  });
  const elems = Object.values(schema.data_elements || {});
  const items = elems.map((d) => ({
    id: d.id,
    label: d.name,
    sub: typeName(d.data_type) + (d.source === "EXTERNAL" ? " · extern" : "") + (writtenBefore.has(d.id) ? " · davor gesetzt" : ""),
    group: writtenBefore.has(d.id) ? "Von einem vorgelagerten Schritt gesetzt" : "Übrige Datenelemente",
  })).sort((a, b) => (a.group === b.group ? a.label.localeCompare(b.label) : a.group < b.group ? -1 : 1));
  if (!items.length) {
    // Ohne Datenelement gibt es nichts zu binden -- direkt das Anlegen anbieten
    // und danach zurueck in den Bindungsdialog.
    addDataElement((id) => bindDataDialog(nodeId, id));
    return;
  }
  const modeSel = el("select", null,
    el("option", { value: "READ" }, "Lesen (liest den Wert)"),
    el("option", { value: "WRITE" }, "Schreiben (setzt den Wert)"),
    el("option", { value: "READ_WRITE" }, "Lesen und Schreiben"));
  const defaultMode = (id) => { modeSel.value = writtenBefore.has(id) ? "READ" : "WRITE"; };
  const picker = pickList(items, defaultMode, preselect);
  defaultMode(picker.get());
  const mandBox = el("input", { type: "checkbox" });
  mandBox.checked = true;
  openModal(`Datenelement an „${nodeCaption(node)}" binden`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Was dieser Schritt liest, muss vorher jemand geschrieben haben – sonst weist der Kern die Bindung ab (D1)."),
      picker.node,
      el("div", { class: "form-grid", style: "margin-top:12px" },
        el("label", { class: "field" }, "Richtung", modeSel),
        el("label", { class: "row", style: "gap:8px;align-items:center" }, mandBox, "Pflichtbindung")),
      el("div", { style: "margin-top:10px" },
        el("button", { class: "btn small ghost", type: "button",
          onClick: () => addDataElement((id) => bindDataDialog(nodeId, id)) },
          "＋ Neues Datenelement anlegen"))),
    async () => {
      const id = picker.get();
      if (!id) { toast("err", "Kein Datenelement gewählt"); return false; }
      const elem = schema.data_elements[id];
      try {
        await createDataAccess(state.schemaId, nodeId, id, modeSel.value, mandBox.checked);
        await refreshSchema(); render();
        toast("ok", "Datenbindung gesetzt", [`${elem ? elem.name : id} (${accessModeLabel(modeSel.value)})`]);
      } catch (err) { toastError(err); return false; }
    }, "Binden");
}

/**
 * Ordnet einem Schritt Bearbeiter zu (BZR) -- Rolle, Abteilung oder Agent,
 * ausgewaehlt direkt am Schritt.
 *
 * Die node-referenzierenden Arten (Bearbeiter/Vorgesetzte:r eines frueheren
 * Schritts, Z3) haben eine eigene Referenzauswahl und laufen deshalb weiter
 * ueber den Dialog ``addStaffRule``, der von hier aus erreichbar bleibt.
 *
 * @param {string} nodeId Zielschritt.
 */
function bindStaffDialog(nodeId) {
  const schema = state.schema;
  const node = schema.nodes[nodeId];
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };
  const current = (schema.staff_rules || {})[nodeId];
  const items = [];
  Object.values(org.roles || {}).forEach((r) =>
    items.push({ id: "ROLE:" + r.id, label: r.name, sub: "Rolle", group: "Rollen" }));
  Object.values(org.org_units || {}).forEach((u) =>
    items.push({ id: "ORG_UNIT:" + u.id, label: u.name, sub: "Abteilung", group: "Abteilungen" }));
  Object.values(org.agents || {}).forEach((a) => {
    const roleNames = (a.role_ids || []).map((id) => ((org.roles || {})[id] || {}).name).filter(Boolean).join(", ");
    items.push({ id: "AGENT:" + a.id, label: a.name, sub: roleNames || "ohne Rolle", group: "Agenten" });
  });
  if (!items.length) {
    toast("info", "Noch keine Rollen/Abteilungen/Agenten", ["Zuerst in der Ressourcensicht anlegen."]);
    focusBindingView("org", nodeId);
    return;
  }
  const recBox = el("input", { type: "checkbox" });
  const recField = el("label", { class: "row", style: "gap:8px;align-items:center" },
    recBox, "Abteilung und alle Bereiche darunter");
  const syncRec = () => {
    const id = picker.get() || "";
    recField.style.display = id.startsWith("ORG_UNIT:") ? "" : "none";
  };
  const picker = pickList(items, syncRec);
  syncRec();
  const others = otherStepsBox(schema, nodeId);
  openModal(`Bearbeiter für „${nodeCaption(node)}"`,
    el("div", null,
      current ? el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        `Ersetzt die bestehende Zuordnung: ${describeRule(current)}.`) : null,
      picker.node,
      el("div", { style: "margin-top:10px" }, recField),
      others.node,
      el("div", { style: "margin-top:10px" },
        el("button", { class: "btn small ghost", type: "button", onClick: () => addStaffRule(nodeId) },
          "Erweiterte Regel (UND/ODER/AUSSER, Bearbeiter eines früheren Schritts) …"))),
    async () => {
      const picked = picker.get();
      if (!picked) { toast("err", "Nichts gewählt"); return false; }
      const [kind, ref] = [picked.slice(0, picked.indexOf(":")), picked.slice(picked.indexOf(":") + 1)];
      const rule = { kind, ref };
      if (kind === "ORG_UNIT") rule.recursive = recBox.checked;
      return applyStaffRuleTo(nodeId, others.selected(), rule);
    }, "Zuordnen");
}

/**
 * Setzt eine Bearbeiterregel auf einen Schritt und optional auf weitere.
 *
 * Die **eine** Stelle, an der eine Zuordnung geschrieben wird -- beide Dialoge
 * (Schritt-Karte und Ressourcensicht) rufen sie auf. Beide koennen mehrere
 * Schritte auf einmal zuordnen -- wer die Zuordnung in der Ressourcensicht
 * sucht, findet dort dieselbe Moeglichkeit.
 *
 * Jede Zuordnung bleibt eine **eigene** Kern-Operation mit eigener Prüfung --
 * keine Sammel-Abkürzung. Scheitert eine, bleiben die anderen bestehen, und die
 * Meldung nennt, welche nicht ging.
 *
 * @param {string} nodeId der Schritt des Dialogs
 * @param {string[]} extra weitere Schritte aus „Auch weiteren Schritten zuordnen"
 * @param {object} rule die Bearbeiterregel (StaffRule)
 * @returns {Promise<boolean|undefined>} false, wenn schon der erste Schritt scheiterte
 *   (dann bleibt der Dialog offen)
 */
async function applyStaffRuleTo(nodeId, extra, rule) {
  try {
    await api.post(`/schemas/${state.schemaId}/staff-rule`, { node_id: nodeId, rule });
  } catch (err) { toastError(err); return false; }
  const failed = [];
  for (const other of extra) {
    try { await api.post(`/schemas/${state.schemaId}/staff-rule`, { node_id: other, rule }); }
    catch (err) { const d = describeError(err); failed.push(`${nodeLabelOf(other)}: ${d.lines.join(" ") || d.title}`); }
  }
  await refreshSchema(); render();
  const done = 1 + extra.length - failed.length;
  if (failed.length) toast("err", `${countLabel(done, "Schritt", "Schritte")} zugeordnet, ${failed.length} nicht`, failed);
  else toast("ok", done > 1 ? `${done} Schritten zugeordnet` : "Bearbeiter zugeordnet", [describeRule(rule)]);
}

/**
 * Auswahl „Auch diesen Schritten zuordnen“ im Bearbeiter-Dialog.
 *
 * Bietet die uebrigen interaktiven Schritte an (automatische tragen keine
 * Bearbeiterregel, Z4). Schritte ohne Bearbeiter stehen oben und lassen sich mit
 * einem Klick alle waehlen -- der haeufigste Fall vor einer Freigabe (B2).
 * Wird von ``bindStaffDialog`` genutzt, das beide Oberflaechen aufrufen.
 *
 * @param {object} schema das Schema
 * @param {string} nodeId der Schritt, fuer den der Dialog offen ist
 * @returns {{node: HTMLElement|null, selected: () => string[]}}
 */
function otherStepsBox(schema, nodeId) {
  const rules = schema.staff_rules || {};
  const bindings = schema.service_bindings || {};
  const candidates = activitiesOf(schema)
    .filter((n) => n.id !== nodeId && !(bindings[n.id] && bindings[n.id].automatic))
    .sort((a, b) => Number(Boolean(rules[a.id])) - Number(Boolean(rules[b.id])));
  if (!candidates.length) return { node: null, selected: () => [] };
  const boxes = candidates.map((n) => {
    const box = el("input", { type: "checkbox", value: n.id });
    return { box, row: el("label", { class: "check-row" }, box, " " + nodeCaption(n),
      rules[n.id] ? el("span", { class: "muted", style: "font-size:11px" }, ` (ersetzt: ${describeRule(rules[n.id])})`) : null) };
  });
  const unstaffed = boxes.filter((b) => !rules[b.box.value]);
  const node = el("details", { class: "multi-steps", style: "margin-top:10px" },
    el("summary", null, "Auch weiteren Schritten zuordnen …"),
    unstaffed.length
      ? el("button", { class: "btn small ghost", type: "button", style: "margin:6px 0",
          onClick: () => unstaffed.forEach((b) => { b.box.checked = true; }) },
          `Alle ohne Bearbeiter wählen (${unstaffed.length})`)
      : null,
    el("div", { class: "check-list" }, ...boxes.map((b) => b.row)));
  return { node, selected: () => boxes.filter((b) => b.box.checked).map((b) => b.box.value) };
}

// Bindet ein Datenelement an den gewaehlten Schritt (⊕ in der Palette der
// klassischen Sicht): derselbe Dialog wie an der Schritt-Karte, mit dem Element
// vorgewaehlt -- Richtung (Lesen/Schreiben) und Pflicht/optional inklusive.
function dropDataElementOnNode(nodeId, payload) {
  bindDataDialog(nodeId, payload.element_id);
}

// Bindet eine Rolle/Abteilung an den gewaehlten Schritt (⊕ in der Palette):
// setzt dessen Bearbeiterzuordnung (BZR). Eine bestehende Zuordnung wird ersetzt
// (der Kern prueft Z1-Z4; eine unerfuellbare Regel wird abgewiesen).
function dropResourceOnNode(nodeId, payload) {
  const node = state.schema.nodes[nodeId];
  const current = (state.schema.staff_rules || {})[nodeId];
  const recBox = el("input", { type: "checkbox" });
  const recField = el("label", { class: "row", style: "gap:8px;align-items:center" },
    recBox, "Abteilung und alle Bereiche darunter");
  const label = payload.rkind === "ROLE" ? "Rolle" : payload.rkind === "AGENT" ? "Agent" : "Abteilung";
  // Wie der Bearbeiter-Dialog der Schritt-Karte: weitere Schritte und die
  // erweiterte Regel ueber dieselben Funktionen (otherStepsBox, addStaffRule,
  // applyStaffRuleTo).
  const others = otherStepsBox(state.schema, nodeId);
  openModal(`Bearbeiter für „${nodeCaption(node)}"`,
    el("div", { class: "form-grid" },
      el("div", { class: "field" }, `${label}: `, el("strong", null, payload.name)),
      current ? el("div", { class: "muted", style: "font-size:12px" },
        `Ersetzt die bestehende Zuordnung: ${describeRule(current)}.`) : null,
      payload.rkind === "ORG_UNIT" ? recField : null,
      others.node,
      el("div", null,
        el("button", { class: "btn small ghost", type: "button", onClick: () => addStaffRule(nodeId) },
          "Erweiterte Regel (UND/ODER/AUSSER, Bearbeiter eines früheren Schritts) …"))),
    async () => {
      const rule = { kind: payload.rkind, ref: payload.ref };
      if (payload.rkind === "ORG_UNIT") rule.recursive = recBox.checked;
      return applyStaffRuleTo(nodeId, others.selected(), rule);
    }, "Zuordnen");
}

// Löst eine einzelne Datenbindung eines Schritts (Inspektor „✕"). Läuft über den
// Kern (DELETE /data-access): ist die Bindung noch nötig – etwa der einzige
// Schreiber hinter einem Pflichtlesen (D1) – weist der Kern mit 422 ab und die
// Bindung bleibt bestehen.
async function removeDataAccess(nodeId, elementId, mode, name) {
  await commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/data-access/${nodeId}/${elementId}?mode=${encodeURIComponent(mode)}`), "Datenbindung gelöst", [`${name} (${mode})`]);
}

// Entfernt die Bearbeiterzuordnung (BZR) eines Schritts (Inspektor „Entfernen").
// Läuft über den Kern (DELETE /staff-rule); ein Schritt ohne Bearbeiter ist im
// Entwurf zulässig (B2 greift erst bei der Freigabe).
async function removeStaffRule(nodeId) {
  await commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/staff-rule/${nodeId}`), "Bearbeiterzuordnung entfernt");
}

// --------------------------------------------------------------------------
// Knoten-Inspektor: weitere Standardaktivitäten am Schritt
// (Dienst/A1–A3, Frist/T1, Priorität/E8, Wertklasse/E3)
// --------------------------------------------------------------------------

const VALUE_CLASS_LABELS = {
  VALUE_ADDING: "Wertschöpfend", BUSINESS_NECESSARY: "Betrieblich nötig",
  NON_VALUE_ADDING: "Nicht wertschöpfend",
};
const IMPACT_LABELS = { LOW: "niedrig", MEDIUM: "mittel", HIGH: "hoch" };
const AUTOMATION_LABELS = { EXTERNAL_TASK: "External-Task", HTTP_PUSH: "HTTP-Push" };

// Dauer (Sekunden) menschenlesbar in der groebsten glatt teilenden Einheit.
function formatDuration(sec) {
  if (sec == null) return "–";
  if (sec % 86400 === 0) return countLabel(sec / 86400, "Tag", "Tage");
  if (sec % 3600 === 0) return sec / 3600 + " Std.";
  if (sec % 60 === 0) return sec / 60 + " Min.";
  return sec + " Sek.";
}

// Dauer-Eingabe (Zahl + Einheit). ``read()`` liefert Sekunden bzw. ``null``
// (leer/ungueltig -> je nach Aufrufer "keine Angabe"/"loeschen").
function durationControls(currentSec) {
  const units = [["60", "Minuten"], ["3600", "Stunden"], ["86400", "Tage"]];
  let val = "", unit = "3600";
  if (currentSec != null) {
    if (currentSec % 86400 === 0) { unit = "86400"; val = String(currentSec / 86400); }
    else if (currentSec % 3600 === 0) { unit = "3600"; val = String(currentSec / 3600); }
    else { unit = "60"; val = String(Math.round(currentSec / 60)); }
  }
  const num = el("input", { type: "number", min: "0", step: "any", value: val, placeholder: "z. B. 4" });
  const unitSel = el("select", null, ...units.map(([v, l]) => el("option", { value: v }, l)));
  unitSel.value = unit;
  const node = el("div", { class: "row", style: "gap:8px" },
    el("label", { class: "field", style: "flex:1" }, "Dauer", num),
    el("label", { class: "field", style: "flex:1" }, "Einheit", unitSel));
  const read = () => { const n = parseFloat(num.value); if (!(n > 0)) return null; return Math.round(n * Number(unitSel.value)); };
  return { node, read };
}

// Haengt die Abschnitte Dienst/Frist/Priorität/Wertklasse an den Inspektor-Body.
// Dienst nur fuer ACTIVITY (ein SUBPROCESS wird durch sein Submodell ausgefuehrt);
// Frist/Priorität/Wertklasse gelten fuer ACTIVITY und SUBPROCESS.
function nodePerformSections(body, schema, node) {
  const isActivity = node.type === NODE_TYPE.ACTIVITY;

  if (isActivity) {
    syncBlock(body, schema, node, true);
    const sb = (schema.service_bindings || {})[node.id];
    body.appendChild(el("div", { class: "hr" }));
    body.appendChild(el("div", { class: "insp-h" }, "Dienst"));
    if (sb) {
      const tmpl = sb.template_id ? (schema.activity_templates || {})[sb.template_id] : null;
      body.appendChild(el("div", { style: "font-size:12px" },
        el("strong", null, sb.name), " · ", sb.automatic ? "automatisch" : "interaktiv",
        tmpl ? ` · Template „${tmpl.name}“` : ""));
      if (sb.automatic && sb.automation && sb.automation !== "MANUAL_NONE") {
        body.appendChild(el("div", { class: "muted", style: "font-size:11px" },
          "Automatik: " + (AUTOMATION_LABELS[sb.automation] || sb.automation)));
      }
    } else {
      body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
        SERVICE_OPTIONAL_HINT));
    }
    const row = el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => assignServiceFor(node.id) }, sb ? "Dienst ändern" : "Dienst zuweisen"));
    if (sb && sb.automatic) row.appendChild(el("button", { class: "btn small", onClick: () => editAutomation(node, sb) }, "Automatik…"));
    if (sb) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeService(node.id) }, "Entfernen"));
    body.appendChild(row);
  }

  // Frist (T1): maximale erwartete Dauer des Schritts.
  const tc = (schema.time_constraints || {})[node.id];
  const hasFrist = tc && tc.max_duration_seconds != null;
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Frist (max. Dauer)"));
  body.appendChild(el("div", { class: hasFrist ? "" : "muted", style: "font-size:12px" },
    hasFrist ? formatDuration(tc.max_duration_seconds) : "keine – fließt bei gesetztem Prozess-Termin in die Terminprüfung (T2) ein."));
  if (tc && tc.target_lead_seconds != null) {
    body.appendChild(el("div", { style: "font-size:12px;margin-top:4px" },
      "Soll-Reaktionszeit ab Aktivierung: " + formatDuration(tc.target_lead_seconds)));
  }
  body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
    // Die ganze Constraint weitergeben, nicht nur die Dauer: der Dialog liest
    // daraus BEIDE Soll-Zeiten vor (Dauer und Soll-Reaktionszeit).
    el("button", { class: "btn small", onClick: () => setTimeConstraintFor(node.id, hasFrist ? tc : null) }, hasFrist ? "Frist ändern" : "Frist setzen"),
    hasFrist ? el("button", { class: "btn small danger", onClick: () => removeTimeConstraint(node.id) }, "Entfernen") : null));
  escalationBlock(body, schema, node, true);

  // Priorität (E8): Auswirkung + Dringlichkeit -> abgeleitete Arbeitslisten-Stufe.
  const pr = (schema.node_priorities || {})[node.id];
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Priorität"));
  body.appendChild(el("div", { class: pr ? "" : "muted", style: "font-size:12px" },
    pr ? `Auswirkung ${IMPACT_LABELS[pr.impact] || pr.impact} · Dringlichkeit ${IMPACT_LABELS[pr.urgency] || pr.urgency}` : "normal (Standard)"));
  body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
    el("button", { class: "btn small", onClick: () => setPriorityFor(node.id, pr) }, pr ? "Priorität ändern" : "Priorität setzen"),
    pr ? el("button", { class: "btn small danger", onClick: () => removePriority(node.id) }, "Entfernen") : null));

  // Wertklasse (E3): rein beratende Klassifikation (keine Korrektheitswirkung).
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Wertklasse"));
  const vcSel = el("select", null,
    el("option", { value: "" }, "– keine –"),
    ...Object.entries(VALUE_CLASS_LABELS).map(([v, l]) => el("option", { value: v }, l)));
  vcSel.value = node.value_class || "";
  vcSel.addEventListener("change", () => setValueClass(node.id, vcSel.value || null));
  body.appendChild(el("label", { class: "field", style: "font-size:12px" }, "Klassifikation", vcSel));

  // E-Mail-Benachrichtigung (Regelgruppe N): opt-in je Aktivität.
  nodeMailSection(body, schema, node);
}

const MAIL_MODE_LABELS = {
  TO_ELIGIBLE_AGENTS: "an die berechtigten Bearbeiter",
  TO_GROUP_MAILBOX: "an das Gruppenpostfach (Rolle/Abteilung)",
};

// Abschnitt „E-Mail bei Aufgabeneingang“ im Aktivitäts-Inspektor. Trägt keine
// Korrektheitslogik: der Kern verlangt eine BZR (N2) und vollständige, wohlgeformte
// Adressen (N3) und lehnt sonst das Speichern mit 422 ab – der konkrete Befund
// (welcher Bearbeiter/welche Gruppe keine Adresse hat) erscheint dann im Toast.
function nodeMailSection(body, schema, node) {
  if (node.type !== NODE_TYPE.ACTIVITY) return;
  const binding = (schema.mail_bindings || {})[node.id];
  const hasRule = !!(schema.staff_rules || {})[node.id];
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "E-Mail bei Aufgabeneingang"));
  if (binding) {
    body.appendChild(el("div", { style: "font-size:12px" },
      MAIL_MODE_LABELS[binding.mode] || binding.mode,
      binding.mode === "TO_ELIGIBLE_AGENTS"
        ? (binding.include_deputies ? " · inkl. Vertreter" : " · ohne Vertreter") : ""));
    body.appendChild(el("div", { class: "muted", style: "font-size:11px;margin-top:2px" },
      "Betreff: " + (binding.subject || "–")));
  } else if (!hasRule) {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Erst einen Bearbeiter zuordnen – ohne Zuordnung gibt es keinen Empfänger (N2)."));
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Aus – bei Aufgabeneingang wird keine Mail gesendet (Standard)."));
  }
  const row = el("div", { class: "row", style: "gap:8px;margin-top:6px" },
    el("button", { class: "btn small", disabled: !hasRule && !binding,
      onClick: () => editMailBinding(node.id, binding) }, binding ? "Mail ändern" : "Mail senden…"));
  if (binding) row.appendChild(el("button", { class: "btn small danger",
    onClick: () => removeMailBinding(node.id) }, "Entfernen"));
  body.appendChild(row);
}

// Modal zum Anlegen/Ändern des Mailversands. Der Platzhalter-Picker bietet nur die
// an diesem Schritt gelesenen Datenelemente an: eine Lesebindung ist durch D2
// „kein Read ohne vorheriges Set“ bei Aufgabeneingang garantiert gesetzt, sodass
// N4 (Vorlagen-Integrität) gar nicht erst verletzt werden kann.
function editMailBinding(nodeId, current) {
  const schema = state.schema;
  const modeSel = el("select", null,
    ...Object.entries(MAIL_MODE_LABELS).map(([v, l]) => el("option", { value: v }, l)));
  modeSel.value = current ? current.mode : "TO_ELIGIBLE_AGENTS";
  const depBox = el("input", { type: "checkbox" });
  depBox.checked = current ? !!current.include_deputies : true;
  const depWrap = el("label", { class: "row", style: "gap:8px;align-items:center" },
    depBox, "Vertreter einbeziehen");
  const subject = el("input", { type: "text", value: current ? current.subject : "",
    placeholder: "z. B. Neue Aufgabe: {kundenname}" });
  const bodyArea = el("textarea", { rows: "4",
    placeholder: "Optionaler Text mit {platzhalter}" }, current ? current.body : "");
  const readable = (schema.data_accesses || [])
    .filter((a) => a.node_id === nodeId && a.mode === "READ")
    .map((a) => schema.data_elements[a.element_id]).filter(Boolean);
  let lastField = subject;
  subject.addEventListener("focus", () => { lastField = subject; });
  bodyArea.addEventListener("focus", () => { lastField = bodyArea; });
  const insert = (id) => {
    const f = lastField;
    const start = f.selectionStart != null ? f.selectionStart : f.value.length;
    const end = f.selectionEnd != null ? f.selectionEnd : start;
    f.value = f.value.slice(0, start) + "{" + id + "}" + f.value.slice(end);
    f.focus();
  };
  const picker = readable.length
    ? el("div", { class: "row", style: "gap:6px;flex-wrap:wrap" },
        ...readable.map((e) => el("button", { class: "btn small", type: "button",
          onClick: () => insert(e.id) }, "{" + e.id + "}")))
    : el("div", { class: "muted", style: "font-size:11px" },
        "Keine an diesem Schritt garantiert gesetzten Datenelemente – Platzhalter erst nach einer Lesebindung.");
  const syncDep = () => {
    depWrap.style.display = modeSel.value === "TO_ELIGIBLE_AGENTS" ? "" : "none";
  };
  modeSel.addEventListener("change", syncDep); syncDep();
  openModal(`E-Mail – ${nodeCaption(schema.nodes[nodeId])}`,
    el("div", { style: "display:flex;flex-direction:column;gap:10px" },
      el("div", { class: "muted", style: "font-size:12px" },
        "Empfänger ergeben sich aus der Bearbeiterzuordnung dieses Schritts. Gesendet wird nur, wenn jede mögliche Adresse vorhanden ist (N3)."),
      el("label", { class: "field" }, "Empfänger", modeSel),
      depWrap,
      el("label", { class: "field" }, "Betreff", subject),
      el("label", { class: "field" }, "Text (optional)", bodyArea),
      el("div", { class: "field" },
        el("span", { class: "muted", style: "font-size:11px" }, "Platzhalter einfügen:"), picker)),
    async () => {
      const binding = {
        mode: modeSel.value, include_deputies: depBox.checked,
        subject: subject.value, body: bodyArea.value,
      };
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/mail-binding`, { node_id: nodeId, binding }), "Mailversand gesetzt");
    }, "Speichern");
}

// Entfernt den Mailversand an einem Schritt (POST … mail-binding mit binding:null).
async function removeMailBinding(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/mail-binding`, { node_id: nodeId, binding: null }), "Mailversand entfernt");
}

// Dienst zuweisen/aendern. Optional an ein Activity-Repository-Template gebunden
// (dann wird ``automatic`` aus dem Executor abgeleitet und die Parameter werden
// typkonform auf Datenelemente abgebildet -> A1-A3). Ohne Template ein freier,
// benannter Dienst. Der Kern prueft die Bindung (422 bei Verstoss).
function assignServiceFor(nodeId) {
  const schema = state.schema;
  const sb = (schema.service_bindings || {})[nodeId];
  const templates = Object.values(schema.activity_templates || {});
  const nameInput = el("input", { type: "text", value: sb ? sb.name : "", placeholder: "z. B. Antrag erfassen" });
  const autoBox = el("input", { type: "checkbox" });
  autoBox.checked = sb ? !!sb.automatic : false;
  const autoField = el("label", { class: "row", style: "gap:8px;align-items:center" },
    autoBox, "Automatischer Schritt (kein Bearbeiter nötig)");
  const tmplSel = templates.length
    ? el("select", null, el("option", { value: "" }, "– ohne Template (freier Dienst) –"),
        ...templates.map((t) => el("option", { value: t.id }, `${t.name} (${t.executor})`)))
    : null;
  if (tmplSel && sb && sb.template_id) tmplSel.value = sb.template_id;
  const mapHost = el("div");
  const buildMap = () => {
    clear(mapHost); mapHost._read = null;
    if (!tmplSel || !tmplSel.value) { autoBox.disabled = false; return; }
    const t = templates.find((x) => x.id === tmplSel.value);
    if (!t) return;
    // Bei Template-Bindung wird ``automatic`` aus dem Executor abgeleitet (A2).
    autoBox.checked = t.executor !== "MANUAL"; autoBox.disabled = true;
    const form = templateMappingForm(t, schema, sb && sb.template_id === t.id ? sb.parameter_mapping : {});
    mapHost._read = form.read;
    mapHost.appendChild(el("div", { class: "muted", style: "font-size:12px;margin:6px 0" },
      "Parameter-Zuordnung: jeden Template-Parameter typkonform auf ein Datenelement abbilden (A3)."));
    mapHost.appendChild(form.grid);
  };
  if (tmplSel) tmplSel.addEventListener("change", buildMap);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name des Dienstes", nameInput),
    tmplSel ? el("label", { class: "field" }, "Template (Activity Repository)", tmplSel) : null,
    autoField, mapHost);
  buildMap();
  openModal(`Dienst – ${nodeCaption(schema.nodes[nodeId])}`, body, async () => {
    if (!nameInput.value.trim()) { toast("err", "Name darf nicht leer sein"); return false; }
    const req = { node_id: nodeId, name: nameInput.value.trim(), automatic: autoBox.checked };
    if (tmplSel && tmplSel.value) {
      req.template_id = tmplSel.value;
      req.parameter_mapping = mapHost._read ? mapHost._read() : {};
    }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/service`, req), "Dienst zugewiesen", [req.name]);
  }, "Übernehmen");
}

// Zuordnungsformular fuer die Template-Parameter (Eingaben + Ergebnisse). Bietet
// je Parameter nur typgleiche Datenelemente an; der Kern prueft A3 verbindlich.
function templateMappingForm(template, schema, current) {
  const params = [
    ...(template.inputs || []).map((p) => ({ ...p, dir: "Eingabe" })),
    ...(template.outputs || []).map((p) => ({ ...p, dir: "Ergebnis" })),
  ];
  const elems = Object.values(schema.data_elements || {});
  const grid = el("div", { class: "form-grid" });
  const rows = [];
  if (!params.length) grid.appendChild(el("div", { class: "muted", style: "font-size:12px" }, "Dieses Template hat keine Parameter."));
  params.forEach((p) => {
    const sel = el("select", null,
      el("option", { value: "" }, p.mandatory ? "– bitte wählen –" : "– keine –"),
      ...elems.filter((e) => e.data_type === p.data_type).map((e) => el("option", { value: e.id }, e.name)));
    if (current && current[p.name]) sel.value = current[p.name];
    rows.push({ name: p.name, sel });
    grid.appendChild(el("label", { class: "field" }, `${p.dir}: ${p.name} (${typeName(p.data_type)})`, sel));
  });
  const read = () => { const m = {}; rows.forEach((r) => { if (r.sel.value) m[r.name] = r.sel.value; }); return m; };
  return { grid, read };
}

async function removeService(nodeId) {
  await commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/service/${nodeId}`), "Dienst entfernt");
}

function setTimeConstraintFor(nodeId, current) {
  // ``current`` ist die getragene Constraint (oder null). Zwei getrennte
  // Soll-Zeiten: die Bearbeitungs-
  // dauer ab Start (T2/kritischer Pfad) und die optionale Reaktionszeit ab
  // Aktivierung, die die zeitbasierte Arbeitslisten-Priorisierung steuert.
  const tc = current || {};
  const dur = durationControls(tc.max_duration_seconds != null ? tc.max_duration_seconds : null);
  const lead = durationControls(tc.target_lead_seconds != null ? tc.target_lead_seconds : null);
  // Netto-Zeit (E2 Stufe C, Opt-in): hält eine Pause die Fällig-Uhr an?
  // Default aus – ohne bewusste Entscheidung des Modellierers bleibt Anhalten
  // reine Transparenz und verschiebt weder Frist noch Eskalation.
  const pauseBox = el("input", { type: "checkbox" });
  pauseBox.checked = !!tc.pause_stops_clock;
  openModal(`Frist – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Maximale erwartete Bearbeitungsdauer dieses Schritts (fließt in die Terminprüfung T2 ein)."),
      dur.node,
      el("div", { class: "muted", style: "font-size:12px;margin:12px 0 8px" },
        "Optionale Soll-Reaktionszeit ab Aktivierung: bis wann die Aufgabe angefasst/erledigt sein soll. " +
        "Steuert die automatische Reihung der Arbeitsliste (überfällige oben). Leer = die Bearbeitungsdauer gilt."),
      lead.node,
      el("label", { class: "row", style: "gap:8px;align-items:center;font-size:12px;margin-top:12px;cursor:pointer" },
        pauseBox,
        el("span", null, "Pause hält die Uhr an (Netto-Zeit): angehaltene Zeit zählt nicht auf Frist und Eskalation. " +
          "Ohne Häkchen läuft die Uhr in Pausen bewusst weiter."))),
    async () => {
      const sec = dur.read();
      if (sec == null) { toast("err", "Bitte eine Dauer > 0 angeben"); return false; }
      const leadSec = lead.read();  // null = nicht gesetzt (Fallback-Regel S)
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/time-constraint`, {
        node_id: nodeId,
        constraint: { max_duration_seconds: sec, target_lead_seconds: leadSec,
          pause_stops_clock: pauseBox.checked },
      }), "Frist gesetzt");
    }, "Speichern");
}

async function removeTimeConstraint(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/time-constraint`, { node_id: nodeId, constraint: null }), "Frist entfernt");
}

// --- Synchronisation (K4) --------------------------------------------------
// EINE geteilte Blockfunktion für beide Modellier-Oberflächen: zeigt die
// Sync-Kanten eines Schritts (wartet auf / kommt vor) und bietet Setzen/
// Lösen an. Zulässigkeit (verschiedene Zweige EINES AND-Blocks, keine
// Zyklen) prüft der Kern (K4, Validate-before-Commit) – der Client zeigt
// Befunde nur an.
function syncBlock(body, schema, node, draft) {
  if (node.type !== NODE_TYPE.ACTIVITY) return;
  const touching = syncEdges(schema).filter((e) => e.source === node.id || e.target === node.id);
  if (!touching.length && !draft) return;
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Synchronisation (parallel)"));
  if (touching.length) {
    touching.forEach((e) => {
      const otherId = e.source === node.id ? e.target : e.source;
      const other = schema.nodes[otherId];
      const caption = other ? nodeCaption(other) : otherId;
      const label = e.source === node.id
        ? `läuft vor „${caption}“ (der wartet)`
        : `wartet auf „${caption}“`;
      body.appendChild(el("div", { class: "row", style: "gap:6px;align-items:center;font-size:12px" },
        el("span", null, label),
        draft ? el("button", { class: "btn small danger",
          onClick: () => removeSyncEdgeFor(e.source, e.target) }, "Lösen") : null));
    });
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "keine – eine Sync-Kante ordnet diesen Schritt zeitlich gegen einen Schritt eines anderen Parallelzweigs (warten auf Abschluss oder Abwahl)."));
  }
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => addSyncEdgeFor(node.id) }, "Sync-Kante hinzufügen…")));
  }
}

function addSyncEdgeFor(nodeId) {
  const acts = Object.values(state.schema.nodes)
    .filter((n) => n.type === NODE_TYPE.ACTIVITY && n.id !== nodeId)
    .sort((a, b) => (a.label || "").localeCompare(b.label || ""));
  if (!acts.length) { toast("err", "Keine weitere Aktivität vorhanden"); return; }
  const target = el("select", null, ...acts.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const dir = el("select", null,
    el("option", { value: "waits" }, "dieser Schritt wartet auf den gewählten"),
    el("option", { value: "before" }, "der gewählte Schritt wartet auf diesen"));
  openModal(`Sync-Kante – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Ordnet zwei Schritte verschiedener Parallelzweige zeitlich: Der wartende Schritt beginnt erst, wenn der andere abgeschlossen (oder abgewählt) ist. Zulässig nur zwischen verschiedenen Zweigen desselben UND-Blocks (K4); der Kern prüft vor dem Speichern."),
      el("label", { class: "field" }, "Anderer Schritt", target),
      el("label", { class: "field" }, "Richtung", dir)),
    async () => {
      const src = dir.value === "waits" ? target.value : nodeId;
      const dst = dir.value === "waits" ? nodeId : target.value;
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/sync-edge`, { source_id: src, target_id: dst }), "Sync-Kante gesetzt");
    }, "Hinzufügen");
}

async function removeSyncEdgeFor(sourceId, targetId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/sync-edge/remove`, { source_id: sourceId, target_id: targetId }), "Sync-Kante gelöst");
}

// Querschritt zwischen Knotenmengen (ADEPT insertBetweenNodeSets): neuer
// Parallelzweig, per Sync-Kanten nach den Quellen und vor den Zielen.
function insertBetweenDialog() {
  const acts = Object.values(state.schema.nodes)
    .filter((n) => n.type === NODE_TYPE.ACTIVITY)
    .sort((a, b) => (a.label || "").localeCompare(b.label || ""));
  if (acts.length < 2) { toast("err", "Dafür braucht es mindestens zwei Aktivitäten"); return; }
  const label = el("input", { type: "text", placeholder: "z. B. Zwischenprüfung" });
  const mk = () => el("select", { multiple: true, size: "5" },
    ...acts.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const src = mk(), dst = mk();
  openModal("Querschritt einfügen (zwischen Knotenmengen)",
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Der neue Schritt läuft als eigener Parallelzweig dieses UND-Blocks: erst nachdem alle Quell-Schritte erledigt (oder abgewählt) sind, und vor allen Ziel-Schritten (Sync-Kanten, K4). Quellen und Ziele müssen im selben UND-Block liegen; der Kern prüft vor dem Speichern."),
      el("label", { class: "field" }, "Bezeichnung", label),
      el("label", { class: "field" }, "Nach diesen Schritten (Quellen, Mehrfachauswahl)", src),
      el("label", { class: "field" }, "Vor diesen Schritten (Ziele, Mehrfachauswahl)", dst)),
    async () => {
      const sources = [...src.selectedOptions].map((o) => o.value);
      const targets = [...dst.selectedOptions].map((o) => o.value);
      if (!label.value.trim() || !sources.length || !targets.length) {
        toast("err", "Bezeichnung, Quellen und Ziele angeben"); return false;
      }
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/insert-between`,
        { label: label.value.trim(), source_ids: sources, target_ids: targets }), "Querschritt eingefügt");
    }, "Einfügen");
}

// --- Eskalation (T3/E9) ----------------------------------------------------
// Anzeige + Bearbeitung der modellierten Fristverletzungs-Reaktion. EINE
// geteilte Blockfunktion für beide Modellier-Oberflächen (Schritt-Karte und
// klassischer Inspektor), damit die Sichten nie driften. Die Wohlgeformtheit
// (Soll-Zeit vorhanden, Stufen aufsteigend, Ziele auflösbar) prüft der Kern
// (T3a–T3c, Validate-before-Commit) – der Client zeigt Befunde nur an.
function escalationBlock(body, schema, node, draft) {
  if (node.type !== NODE_TYPE.ACTIVITY) return;
  const policy = (schema.escalation_policies || {})[node.id];
  const roles = (schema.org_model || {}).roles || {};
  const roleName = (ref) => (roles[ref] ? roles[ref].name : ref);
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Eskalation bei Fristüberschreitung"));
  if (policy && (policy.stages || []).length) {
    policy.stages.forEach((s, i) => {
      const when = s.after_seconds > 0
        ? formatDuration(s.after_seconds) + " nach Fristüberschreitung"
        : "bei Fristüberschreitung";
      const what = s.kind === "FUNCTIONAL"
        ? `zusätzlich anbieten an „${roleName(s.rule && s.rule.ref)}“`
        : `informieren: „${roleName(s.rule && s.rule.ref)}“`;
      body.appendChild(el("div", { style: "font-size:12px" }, `Stufe ${i + 1}: ${when} – ${what}`));
    });
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "keine – eine Überschreitung bleibt nur im Kritikalitätsband der Arbeitsliste sichtbar."));
  }
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => setEscalationFor(node.id, policy || null) },
        policy ? "Eskalation ändern" : "Eskalation einrichten"),
      policy ? el("button", { class: "btn small danger", onClick: () => removeEscalation(node.id) }, "Entfernen") : null));
  }
}

function setEscalationFor(nodeId, current) {
  const roles = Object.values((state.schema.org_model || {}).roles || {});
  if (!roles.length) { toast("err", "Erst Rollen in der Organisation anlegen"); return; }
  // Schlichte Spalte statt ``.row``: Dessen Umbruch (flex-wrap) mass das
  // Stufenraster bei Minimalbreite und liess darueber eine grosse Luecke.
  const rows = el("div", { style: "display:flex;flex-direction:column;gap:8px" });
  function addRow(stage) {
    const after = el("input", { type: "number", class: "esc-after", min: "0",
      value: stage ? String(Math.round(stage.after_seconds / 60)) : "0" });
    const kind = el("select", { class: "esc-kind" },
      el("option", { value: "FUNCTIONAL" }, "zusätzlich anbieten (funktional)"),
      el("option", { value: "HIERARCHICAL" }, "informieren (hierarchisch)"));
    if (stage) kind.value = stage.kind;
    const role = el("select", { class: "esc-role" },
      ...roles.map((r) => el("option", { value: r.id }, r.name)));
    if (stage && stage.rule && stage.rule.ref) role.value = stage.rule.ref;
    rows.appendChild(el("div", { class: "branch-row esc-row" },
      el("label", { class: "field" }, "Minuten nach Frist", after),
      el("label", { class: "field" }, "Art", kind),
      el("label", { class: "field" }, "Ziel-Rolle", role)));
  }
  ((current && current.stages && current.stages.length) ? current.stages : [null]).forEach(addRow);
  openModal(`Eskalation – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Überschreitet die Aufgabe ihre Soll-Zeit, feuern die Stufen nacheinander: „zusätzlich anbieten“ erweitert den Bearbeiterkreis (funktionale Eskalation), „informieren“ benachrichtigt die Ziel-Rolle, ohne sie zu berechtigen (hierarchisch). Voraussetzung ist eine Frist bzw. Soll-Reaktionszeit am Schritt."),
      rows,
      el("button", { class: "btn small ghost", onClick: () => addRow(null) }, "+ Stufe")),
    async () => {
      const stages = [...rows.querySelectorAll(".esc-row")].map((r) => ({
        after_seconds: Number(r.querySelector(".esc-after").value || "0") * 60,
        kind: r.querySelector(".esc-kind").value,
        rule: { kind: "ROLE", ref: r.querySelector(".esc-role").value },
      }));
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/escalation-policy`, { node_id: nodeId, policy: { stages } }), "Eskalation gespeichert");
    }, "Speichern");
}

async function removeEscalation(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/escalation-policy`, { node_id: nodeId, policy: null }), "Eskalation entfernt");
}

function setPriorityFor(nodeId, current) {
  const mk = (val) => {
    const s = el("select", null, ...Object.entries(IMPACT_LABELS).map(([v, l]) => el("option", { value: v }, l)));
    s.value = val || "MEDIUM"; return s;
  };
  const impact = mk(current && current.impact), urgency = mk(current && current.urgency);
  openModal(`Priorität – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Priorität = Auswirkung + Dringlichkeit; die Arbeitslisten-Reihung ergibt sich daraus."),
      el("div", { class: "form-grid" },
        el("label", { class: "field" }, "Auswirkung", impact),
        el("label", { class: "field" }, "Dringlichkeit", urgency))),
    async () => {
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/priority`, { node_id: nodeId, priority: { impact: impact.value, urgency: urgency.value } }), "Priorität gesetzt");
    }, "Speichern");
}

async function removePriority(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/priority`, { node_id: nodeId, priority: null }), "Priorität entfernt");
}

async function setValueClass(nodeId, value) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/value-class`, { node_id: nodeId, value_class: value }), value ? "Wertklasse gesetzt" : "Wertklasse entfernt");
}

// Prozessweiter Termin (T2): der Kern prueft, dass der kritische Pfad
// hineinpasst. Leer lassen entfernt den Termin.
function setProcessDeadline() {
  const dur = durationControls(state.schema.deadline_seconds != null ? state.schema.deadline_seconds : null);
  openModal("Prozess-Termin (Deadline)",
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Harte Frist für den gesamten Prozess; der Kern prüft, dass der kritische Pfad hineinpasst (T2). Leer lassen entfernt den Termin."),
      dur.node),
    async () => {
      const sec = dur.read();
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/deadline`, { deadline_seconds: sec }), sec != null ? "Termin gesetzt" : "Termin entfernt");
    }, "Speichern");
}

function centerCanvasOnNode(wrap, pos, region) {
  // ``wrap`` ist die .canvas-wrap; der Pan/Zoom-Controller verschiebt den
  // Knoten ueber eine CSS-Transformation in die Mitte des Viewports (statt
  // ueber nativen Scroll, der durch overflow:hidden entfaellt). ``region``
  // (optional) haelt zusaetzlich die gestrichelte Datenherkunft im Bild.
  if (wrap && wrap._panzoom) wrap._panzoom.centerOn(pos, region);
}

// --------------------------------------------------------------------------
// Knoten-Inspektor (Aktivitaet umbenennen / Element entfernen)
// --------------------------------------------------------------------------

const SPLIT_TYPES = new Set([NODE_TYPE.AND_SPLIT, NODE_TYPE.XOR_SPLIT]);

// Liefert die Join-Knoten-ID, wenn der XOR-Split ``splitId`` einen leeren Zweig
// traegt (ein Zweig, der direkt auf den Join zeigt, weil seine Aktivitaet
// geloescht wurde), sonst null. Ein leerer Zweig behaelt seine K7-Zelle, sodass
// nur im anderen Zweig Arbeit anfaellt.
function emptyBranchJoin(schema, splitId) {
  const node = schema.nodes[splitId];
  if (!node || node.type !== NODE_TYPE.XOR_SPLIT) return null;
  for (const e of controlEdges(schema)) {
    if (e.source !== splitId) continue;
    const t = schema.nodes[e.target];
    if (t && t.type === NODE_TYPE.XOR_JOIN) return e.target;
  }
  return null;
}

/**
 * Knopf „Neue Revision" fuer ein freigegebenes Schema (ueberall gleich benannt).
 *
 * Steht dort, wo der Nutzer auf die Wand laeuft (Knoten-Inspektor), nicht nur
 * im Panel „Schema-Evolution" ganz unten in der rechten Spalte: Der Hinweis
 * „Bearbeiten erst in einer neuen Revision moeglich" nannte bisher die Loesung,
 * ohne sie anzubieten -- man musste sie woanders suchen.
 *
 * Loest keine eigene Logik aus, sondern ruft dieselbe Operation wie das Panel
 * (``newRevision`` -> ``POST /schemas/{id}/revision``). Ohne Modellierer-/
 * Admin-Rolle bleibt nur der erklaerende Text, weil der Aufruf sonst ohnehin
 * abgelehnt wuerde.
 *
 * @returns {HTMLElement} Der Knopf, oder ein leeres Element ohne Berechtigung.
 */
function newRevisionAction() {
  if (!hasRole("modeler", "admin")) return el("div");
  return el("div", { class: "row", style: "gap:8px;margin-top:10px" },
    el("button", {
      class: "btn small primary", onClick: newRevision,
      title: "Bearbeitbare Entwurfskopie dieser Revision anlegen",
    }, "Neue Revision"));
}

function nodeInspectorPanel() {
  const schema = state.schema;
  const draft = isDraft(schema);
  const body = el("div", { class: "panel-b" });
  const node = state.selectedNode ? schema.nodes[state.selectedNode] : null;

  if (!node) {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      draft
        ? "Klicke einen Knoten an, um ihn umzubenennen oder zu entfernen."
        : "Klicke einen Knoten an, um zu ihm zu scrollen. Bearbeiten ist nur im Entwurf m\u00F6glich."));
    if (!draft) body.appendChild(newRevisionAction());
    return el("div", { class: "panel" },
      el("div", { class: "panel-h" }, el("h2", null, "Knoten")), body);
  }

  body.appendChild(el("div", { class: "row", style: "gap:8px;align-items:center;margin-bottom:10px" },
    el("span", { class: "pill pill-gray", title: node.type }, nodeTypeLabel(node.type)),
    el("strong", null, nodeCaption(node))));

  const renamable = node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS;
  if (!draft) {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Schema ist freigegeben \u2013 zum Bearbeiten eine neue Revision anlegen (Knoten-IDs bleiben erhalten)."));
    body.appendChild(newRevisionAction());
  } else if (renamable) {
    const input = el("input", { type: "text", id: "insp-name-input", value: node.label || "" });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") renameNode(node.id, input.value); });
    body.appendChild(el("label", { class: "field" }, "Bezeichnung", input));
    body.appendChild(el("div", { class: "row", style: "gap:8px" },
      el("button", { class: "btn small primary", onClick: () => renameNode(node.id, input.value) }, "Umbenennen"),
      el("button", { class: "btn small", title: "Schritt an eine andere Stelle des Ablaufs verschieben – alle Bindungen bleiben erhalten",
        onClick: () => moveNodeDialog(node.id) }, "Verschieben…"),
      el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Entfernen")));
    if (node.type === NODE_TYPE.ACTIVITY) {
      // --- Datenbindungen dieses Schritts (D1-D4) direkt am Schritt ---
      const accesses = (schema.data_accesses || []).filter((a) => a.node_id === node.id);
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "insp-h" }, "Daten"));
      if (accesses.length) {
        const list = el("div", { class: "insp-binds" });
        accesses.forEach((a) => {
          const e = schema.data_elements[a.element_id];
          list.appendChild(el("div", { class: "insp-bind" },
            el("span", { class: "insp-bind-name" }, e ? e.name : a.element_id),
            el("span", { class: "insp-bind-mode mode-" + a.mode, title: a.mode }, accessModeLabel(a.mode)),
            a.mandatory ? null : el("span", { class: "muted", style: "font-size:11px" }, "optional"),
            el("span", { class: "spacer", style: "flex:1" }),
            el("button", { class: "insp-bind-del", title: "Bindung lösen",
              onClick: () => removeDataAccess(node.id, a.element_id, a.mode, e ? e.name : a.element_id) }, "✕")));
        });
        body.appendChild(list);
      } else {
        body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
          "Noch keine Datenbindung – rechts unter „Binden“ ein Datenelement mit ⊕ an diesen Schritt zuweisen."));
      }
      // Zuweisen läuft bewusst über die Binden-Palette (⊕); hier nur die
      // aktuellen Bindungen dieses Schritts anzeigen und einzeln lösen (✕).

      // --- Bearbeiterzuordnung (Z1-Z4) direkt am Schritt ---
      const rule = (schema.staff_rules || {})[node.id];
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "insp-h" }, "Bearbeiter (BZR)"));
      body.appendChild(el("div", { class: rule ? "" : "muted", style: "font-size:12px" },
        rule ? describeRule(rule) : "Keine Zuordnung – hier zuordnen oder rechts unter „Binden“ eine Rolle/Abteilung mit ⊕ an diesen Schritt ziehen."));
      // Derselbe Dialog wie in der Schritt-Karte (bindStaffDialog: Auswahl,
      // „Erweiterte Regel“, „Auch weiteren Schritten zuordnen“) -- beide
      // Oberflaechen bleiben gleichwertig.
      body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
        el("button", { class: "btn small", onClick: () => bindStaffDialog(node.id) },
          rule ? "Ändern …" : "Zuordnen …"),
        rule ? el("button", { class: "btn small danger", onClick: () => removeStaffRule(node.id) }, "Entfernen") : null));

      // Weitere Standardaktivitäten am Schritt: Dienst, Frist, Priorität, Wertklasse.
      nodePerformSections(body, schema, node);

      const form = schema.forms && schema.forms[node.id];
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
        form
          ? `Eingabemaske: ${countLabel(form.fields.length, "Feld", "Felder")}${form.title ? " \u2013 \u201E" + form.title + "\u201C" : ""}.`
          : "Noch keine Eingabemaske \u2013 Felder per Auswahl zusammenstellen."));
      const row = el("div", { class: "row", style: "gap:8px" },
        el("button", { class: "btn small", onClick: () => openFormDesigner(node.id) },
          form ? "Maske bearbeiten" : "Eingabemaske gestalten"));
      if (form) {
        row.appendChild(el("button", { class: "btn small danger", onClick: () => deleteForm(node.id) }, "Maske entfernen"));
      }
      body.appendChild(row);
      // Wiederverwendung: Aktivitaet durch ein freigegebenes Submodell ersetzen.
      // Die Bindung gilt Correct-by-Construction: der Kern lehnt sie ab, wenn
      // das Gesamtmodell dadurch inkonsistent oder nicht lauffaehig wuerde.
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
        "Wiederverwendung: diesen Schritt durch ein freigegebenes Submodell aus der Bibliothek ersetzen \u2013 inkl. Daten\u00FCbergabe."));
      body.appendChild(el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "convert") }, "In Subprozess umwandeln"));
    } else if (node.type === NODE_TYPE.SUBPROCESS) {
      const bnd = (schema.sub_process_bindings || {})[node.id];
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
        bnd
          ? `Gebunden an Submodell \u201E${bnd.target_schema_id}\u201C (v${bnd.target_version}).`
          : "Noch kein Submodell gebunden."));
      body.appendChild(el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "rebind") },
        "Zuordnung / Daten\u00FCbergabe \u00E4ndern"));
      // Frist, Priorit\u00E4t und Wertklasse gelten auch f\u00FCr Subprozesse.
      nodePerformSections(body, schema, node);
    }
  } else if (SPLIT_TYPES.has(node.type)) {
    // Leerer Zweig (nur XOR): entsteht, wenn die letzte Aktivit\u00E4t eines Zweigs
    // entfernt wurde. Er bleibt als direkter Split\u2013Join-Zweig stehen, damit nur
    // im anderen Zweig Arbeit anf\u00E4llt; hier l\u00E4sst er sich gezielt aufl\u00F6sen.
    const emptyJoin = emptyBranchJoin(schema, node.id);
    if (emptyJoin) {
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Diese Verzweigung hat einen leeren Zweig \u2013 in ihm f\u00E4llt keine Aktivit\u00E4t an. Entfernen des leeren Zweigs l\u00F6st bei nur noch einem verbleibenden Zweig die ganze Verzweigung auf."));
      body.appendChild(el("button", { class: "btn small", onClick: () => removeEmptyBranch(node.id) }, "Leeren Zweig entfernen"));
      body.appendChild(el("div", { class: "hr" }));
    }
    if (node.type === NODE_TYPE.AND_SPLIT) {
      body.appendChild(el("button", { class: "btn small", style: "margin-bottom:8px",
        onClick: () => insertBetweenDialog() }, "Querschritt einf\u00FCgen\u2026"));
      body.appendChild(el("div", { class: "hr" }));
    }
    body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      "Verzweigung: Entfernen l\u00F6scht den gesamten Block (Split, Zweige und passenden Join)."));
    body.appendChild(el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Verzweigung entfernen"));
  } else if (LOOP_TYPES.has(node.type)) {
    body.appendChild(loopNodePanel(schema, node, draft));
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      node.type === NODE_TYPE.AND_JOIN || node.type === NODE_TYPE.XOR_JOIN
        ? "Join-Knoten werden \u00FCber ihren \u00F6ffnenden Split entfernt."
        : "Start und Ende sind fester Bestandteil des Modells."));
  }
  return el("div", { class: "panel node-inspector" },
    el("div", { class: "panel-h" }, el("h2", null, "Knoten"),
      el("span", { class: "spacer", style: "flex:1" }),
      el("button", { class: "btn small ghost", onClick: () => { state.selectedNode = null; render(); } }, "Abw\u00E4hlen")),
    body);
}

async function renameNode(nodeId, label) {
  const name = (label || "").trim();
  if (!name) { toast("err", "Bezeichnung darf nicht leer sein"); return; }
  try {
    await api.patch(`/schemas/${state.schemaId}/nodes/${nodeId}`, { label: name });
    await refreshSchema();
    render();
    toast("ok", "Aktivit\u00E4t umbenannt", [name]);
  } catch (err) { toastError(err); }
}

function deleteNode(nodeId) {
  const node = state.schema.nodes[nodeId];
  const isSplit = SPLIT_TYPES.has(node.type);
  const msg = isSplit
    ? "Den gesamten Verzweigungsblock (Split, alle Zweige und den passenden Join) entfernen?"
    : node.type === NODE_TYPE.LOOP_START
      ? "Die gesamte Schleife (Anfang, Rumpf und Ende) entfernen?"
      : `\u201E${nodeCaption(node)}\u201C aus dem Modell entfernen?`;
  openModal("Element entfernen", el("div", { class: "muted", style: "font-size:13px" }, msg), async () => {
    try {
      await api.del(`/schemas/${state.schemaId}/nodes/${nodeId}`);
      state.selectedNode = null;
      await refreshSchema();
      render();
      toast("ok", "Element entfernt");
    } catch (err) { toastError(err); return false; }
  }, "Entfernen", { danger: true });
}

// --- Schritt verschieben (moveNode) ---------------------------------------
// Löst den Schritt aus seiner Position und fügt ihn hinter dem gewählten Anker
// wieder ein. Anders als Löschen + Neuanlegen bleibt die Knoten-ID erhalten,
// alle Bindungen (Daten, Bearbeiter, Dienst, Maske, Zeit, Benachrichtigung)
// reisen mit. Der Kern validiert vor dem Commit (D1/Z3/T2/N4 …) und lehnt ein
// unzulässiges Ziel mit lokalisierten Befunden ab – die Zielliste hier ist nur
// eine Anzeige-Vorauswahl, keine Korrektheitsentscheidung. Geteilte Funktion
// beider Modellier-Oberflächen (Schritt-Karte und klassische Sicht) – niemals
// je Sicht ausformulieren, sonst driften die Oberflächen auseinander.

// Plausible Anker: alle Knoten mit genau einem Ausgang (ein Split kann nie
// Anker sein), außer dem Schritt selbst, dem Ende und dem aktuellen
// Vorgänger (das wäre ein No-op).
function moveTargetsFor(nodeId) {
  const schema = state.schema;
  const outCount = {};
  controlEdges(schema).forEach((e) => { outCount[e.source] = (outCount[e.source] || 0) + 1; });
  const pred = controlEdges(schema).find((e) => e.target === nodeId);
  return Object.values(schema.nodes).filter((n) =>
    n.id !== nodeId &&
    n.type !== NODE_TYPE.END &&
    outCount[n.id] === 1 &&
    !(pred && n.id === pred.source));
}

function moveNodeDialog(nodeId) {
  const node = state.schema.nodes[nodeId];
  const targets = moveTargetsFor(nodeId);
  if (!targets.length) { toast("err", "Keine gültige Zielposition vorhanden"); return; }
  const sel = el("select", null,
    ...targets.map((n) => el("option", { value: n.id }, "nach „" + nodeCaption(n) + "“")));
  openModal("Schritt verschieben",
    el("div", null,
      el("div", { class: "muted", style: "font-size:13px;margin-bottom:8px" },
        `„${nodeCaption(node)}“ an eine andere Stelle des Ablaufs verschieben. ` +
        "Alle Bindungen des Schritts bleiben erhalten; ein Ziel, das eine Regel verletzen würde " +
        "(z. B. Lesen vor Schreiben), wird mit Begründung abgelehnt."),
      el("label", { class: "field" }, "Neue Position", sel)),
    async () => {
      try {
        await api.post(`/schemas/${state.schemaId}/nodes/${nodeId}/move`, { after_node_id: sel.value });
        await refreshSchema();
        render();
        toast("ok", "Schritt verschoben", [nodeCaption(node)]);
      } catch (err) { toastError(err); return false; }
    }, "Verschieben");
}

// --- Schleifenblock (K6) ---------------------------------------------------
// Beschreibung + Aktionen der Schleifen-Begrenzer, geteilt von beiden
// Modellier-Oberflächen (Schritt-Karte und klassischer Knoten-Inspektor).
// LOOP_START bietet das Entfernen der ganzen Schleife an (der Kern löscht
// Anfang, Rumpf, Ende und Entscheidung als Einheit); LOOP_END zeigt die
// strukturierte Abbruchentscheidung an – geändert wird sie nicht hier,
// sondern über das Wiederholen-Merkmal (Datenelement) selbst.
function loopNodePanel(schema, node, draft) {
  const box = el("div", null);
  if (node.type === NODE_TYPE.LOOP_END) {
    const d = (schema.loop_decisions || {})[node.id];
    const condition = loopConditionCaption(schema, d, 24);
    box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      d ? `Schleifenende: wiederholt wird, solange ${condition} ist. Der Rumpf läuft mindestens einmal; die Entscheidung fällt in jeder Runde automatisch aus den Daten (K6).`
        : "Schleifenende ohne hinterlegte Entscheidung."));
    if (d && d.max_iterations) {
      box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        `Notbremse: höchstens ${d.max_iterations} Durchläufe – am Limit wird die Schleife automatisch verlassen, und die Zeitprüfung (T2) rechnet den Rumpf ${d.max_iterations}-fach.`));
    }
    box.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Entfernt wird die Schleife über ihren Schleifenanfang."));
    return box;
  }
  box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
    "Schleifenanfang: Der Block bis zum Schleifenende wird wiederholt, bis das Wiederholen-Merkmal die Schleife beendet. Entfernen löscht die gesamte Schleife (Anfang, Rumpf, Ende)."));
  if (draft) {
    box.appendChild(el("button", { class: "btn small danger",
      onClick: () => deleteNode(node.id) }, "Schleife entfernen"));
  }
  return box;
}

// Leeren Zweig eines XOR-Splits gezielt entfernen. Bleibt danach nur ein Zweig
// übrig, löst der Kern die ganze Verzweigung auf (Split/Join verschwinden, der
// verbleibende Zweig wird inline eingefügt). Der Kern validiert vor dem Commit;
// ein ungültiges Ergebnis (z. B. verlorener Auffang-Zweig) wird abgelehnt.
function removeEmptyBranch(splitId) {
  openModal("Leeren Zweig entfernen",
    el("div", { class: "muted", style: "font-size:13px" },
      "Den leeren Zweig dieser Verzweigung entfernen? Verbleibt nur noch ein Zweig, wird die gesamte Verzweigung aufgelöst."),
    async () => {
      try {
        await api.post(`/schemas/${state.schemaId}/nodes/${splitId}/remove-empty-branch`, {});
        state.selectedNode = null;
        await refreshSchema();
        render();
        toast("ok", "Leerer Zweig entfernt");
      } catch (err) { toastError(err); return false; }
    }, "Entfernen", { danger: true });
}

async function deleteForm(nodeId) {
  openModal("Eingabemaske entfernen",
    el("div", { class: "muted", style: "font-size:13px" },
      "Die Eingabemaske dieses Schritts entfernen? Die zugeh\u00F6rigen Datenzugriffe der Maske werden mit gel\u00F6scht."),
    async () => {
      try {
        await api.del(`/schemas/${state.schemaId}/nodes/${nodeId}/form`);
        await refreshSchema();
        render();
        toast("ok", "Eingabemaske entfernt");
      } catch (err) { toastError(err); return false; }
    }, "Entfernen", { danger: true });
}

