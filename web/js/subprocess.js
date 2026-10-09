// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Subprozesse und Migration.
 *
 * Wiederverwendbare Subprozesse (Bibliothek und Datenuebergabe) und der
 * Migrationsassistent, der laufende Vorgaenge auf eine neue Revision hebt.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// Wiederverwendbare Subprozesse (Submodell-Bibliothek + Datenuebergabe)
// --------------------------------------------------------------------------

// Kopf-Button: markiert dieses Modell als wiederverwendbares Submodell. Der
// Katalog-Flag ist reine Metadatenangabe (beeinflusst die Validierung nie);
// bindbar wird ein Submodell erst nach Freigabe (siehe /subprocess-library).
function libraryToggleButton(schema) {
  if (!hasRole("modeler", "admin")) return null;
  const on = schema.is_library_subprocess === true;
  return el("button", {
    class: "btn small" + (on ? " primary" : ""),
    title: "Dieses Modell als wiederverwendbares Submodell f\u00FCr die Bibliothek markieren (nach Freigabe in anderen Modellen bindbar).",
    onClick: () => toggleLibraryFlag(!on),
  }, on ? "\u2605 Submodell" : "\u2606 Als Submodell");
}

async function toggleLibraryFlag(flag) {
  try {
    await api.post(`/schemas/${state.schemaId}/library-flag`, { is_library: flag });
    await refreshSchema();
    render();
    toast("ok", flag ? "Als Submodell markiert" : "Submodell-Markierung entfernt");
  } catch (err) { toastError(err); }
}

// Baut das Zuordnungsformular fuer die Datenuebergabe: je Datenelement des
// Ziel-Submodells eine optionale Eingabe- (parent -> child) und Ergebnis-
// Zuordnung (child -> parent). Nur typgleiche Elternelemente werden angeboten
// (H2); der Kern prueft Typkonformitaet und Erzeugungsgarantie verbindlich.
function subprocessMappingForm(target, parentSchema) {
  const parentEls = Object.values((parentSchema && parentSchema.data_elements) || {});
  const grid = el("div", { class: "form-grid" });
  const rows = [];
  if (!target.data_elements.length) {
    grid.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Das Submodell hat keine Datenelemente \u2013 es wird nur der Kontrollfluss eingebunden."));
  }
  target.data_elements.forEach((te) => {
    const options = () => [el("option", { value: "" }, "\u2013 keine \u2013"),
      ...parentEls.filter((pe) => pe.data_type === te.data_type)
        .map((pe) => el("option", { value: pe.id }, pe.name))];
    const inSel = el("select", null, ...options());
    const outSel = el("select", null, ...options());
    rows.push({ te, inSel, outSel });
    grid.appendChild(el("div", { class: "field" },
      el("div", { style: "font-weight:600;font-size:13px" }, `${te.name} (${typeName(te.data_type)})`),
      el("div", { class: "row", style: "gap:8px" },
        el("label", { class: "field", style: "flex:1" }, "Eingabe von", inSel),
        el("label", { class: "field", style: "flex:1" }, "Ergebnis nach", outSel))));
  });
  const read = () => {
    const input_mapping = {}, output_mapping = {};
    rows.forEach(({ te, inSel, outSel }) => {
      if (inSel.value) input_mapping[te.id] = inSel.value;
      if (outSel.value) output_mapping[te.id] = outSel.value;
    });
    return { input_mapping, output_mapping };
  };
  return { grid, read };
}

// Aktivitaet in einen Subprozess umwandeln ("convert") bzw. die Bindung eines
// bestehenden SUBPROCESS-Knotens aendern ("rebind"). Beides ist Correct by
// Construction: die Verbindung wird nur gesetzt, wenn das resultierende
// Gesamtmodell konsistent und lauffaehig bleibt (der Kern antwortet sonst 422).
async function openSubprocessBinding(node, mode) {
  let library;
  try { library = await api.get("/subprocess-library"); }
  catch (err) { toastError(err); return; }
  if (!library.length) {
    toast("info", "Keine freigegebenen Submodelle in der Bibliothek. Markiere zuerst ein freigegebenes Schema als Submodell.");
    return;
  }
  const bnd = (state.schema.sub_process_bindings || {})[node.id];
  const targetSel = el("select", null,
    ...library.map((t) => el("option", { value: t.id }, `${t.name} (v${t.version})`)));
  if (mode === "rebind" && bnd && library.some((t) => t.id === bnd.target_schema_id)) {
    targetSel.value = bnd.target_schema_id;
  }
  const mapHost = el("div");
  const buildMap = () => {
    const t = library.find((x) => x.id === targetSel.value);
    clear(mapHost);
    if (!t) return;
    const form = subprocessMappingForm(t, state.schema);
    mapHost._read = form.read;
    mapHost.appendChild(el("div", { class: "muted", style: "font-size:12px;margin:6px 0" },
      "Daten\u00FCbergabe: ordne die Elemente des Submodells den Datenelementen dieses Modells zu."));
    mapHost.appendChild(form.grid);
  };
  targetSel.addEventListener("change", buildMap);
  buildMap();
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Submodell aus Bibliothek", targetSel), mapHost);
  const isConvert = mode === "convert";
  openModal(isConvert ? "In Subprozess umwandeln" : "Zuordnung / Daten\u00FCbergabe \u00E4ndern",
    body, async () => {
      const t = library.find((x) => x.id === targetSel.value);
      if (!t) { toast("info", "Bitte ein Submodell w\u00E4hlen."); return false; }
      const { input_mapping, output_mapping } = mapHost._read ? mapHost._read() : { input_mapping: {}, output_mapping: {} };
      const path = isConvert ? "convert-to-subprocess" : "subprocess-binding";
      try {
        await api.post(`/schemas/${state.schemaId}/${path}`, {
          node_id: node.id,
          target_schema_id: t.id,
          target_version: t.version,
          input_mapping,
          output_mapping,
        });
        await refreshSchema();
        render();
        toast("ok", isConvert ? "Aktivit\u00E4t in Subprozess umgewandelt" : "Zuordnung aktualisiert");
      } catch (err) { toastError(err); return false; }
    }, isConvert ? "Umwandeln" : "\u00DCbernehmen");
}

/**
 * Wandelt eine Texteingabe in den Wert des Datentyps -- aber nur, wenn das
 * **eindeutig** geht. Alles andere bleibt der rohe Text, und der Kern lehnt
 * ihn mit einer D3-Meldung ab, die der Nutzer sieht.
 *
 * Frueher stand an zwei Stellen ``val === "true" || val === "1"``: „vielleicht"
 * im Ja/Nein-Feld wurde still zu ``false`` gespeichert, und der Vorgang lief
 * weiter. Ebenso machte ``parseInt`` aus „12,5"
 * still 12 und aus „abc" ``NaN``. Hier wird deshalb nie geraten.
 *
 * @param {string} dtype Datentyp des Elements (INTEGER, FLOAT, BOOLEAN, ...)
 * @param {string} raw nicht-leere Texteingabe
 * @returns {string|number|boolean} typisierter Wert oder der unveraenderte Text
 */
function coerceTypedInput(dtype, raw) {
  const text = String(raw).trim();
  if (isNumericType(dtype)) {
    const num = Number(text.replace(",", "."));
    if (text === "" || !Number.isFinite(num)) return raw;
    if (dtype === "INTEGER" && !Number.isInteger(num)) return raw;
    return num;
  }
  if (dtype === "BOOLEAN") {
    const lower = text.toLowerCase();
    if (["true", "ja", "1"].includes(lower)) return true;
    if (["false", "nein", "0"].includes(lower)) return false;
    return raw;
  }
  return raw;
}

/**
 * Zeigt ein Maskenfeld den Pflicht-Stern „*“?
 *
 * Ein Ankreuzfeld hat immer einen Wert (angehakt = ja, leer = nein); der Stern
 * versprach dort „muss angehakt sein“, ein Abschluss ohne Haken ging aber
 * durch. Deshalb gibt es bei Ankreuzfeldern keinen Stern, und der
 * Maskendesigner bietet „Pflicht“ dort nicht an. Gemeinsam fuer Vorschau des
 * Designers und Aufgabenmaske.
 * @param {{required?: boolean, widget?: string}} f Maskenfeld
 * @returns {boolean}
 */
function showsRequiredMark(f) {
  return !!f.required && f.widget !== "CHECKBOX";
}

/**
 * Widget fuer ein Datenelement **ohne** gestaltete Maske (Abschliessen ohne
 * Maske, Dialog „Instanzdaten eingeben"): nach Datentyp, nie ein Freitextfeld
 * fuer Zahl, Datum oder Ja/Nein. BOOLEAN bekommt eine Ja/Nein-Auswahl ohne
 * Vorbelegung (``YESNO``) statt eines Ankreuzfelds -- ein Ankreuzfeld liefert
 * immer einen Wert, ein uebersehenes Pflichtfeld waere dann still „Nein".
 * @param {object|undefined} elem Datenelement
 * @returns {string} Widget-Art fuer :func:`maskControl`
 */
function fallbackWidget(elem) {
  const dtype = elem ? elem.data_type : "STRING";
  if (dtype === "BOOLEAN") return "YESNO";
  if (isNumericType(dtype)) return "NUMBER";
  if (dtype === "DATE") return "DATE";
  return "TEXT";
}

// Widget-Factory: erzeugt fuer ein Datenelement + Widget-Typ das passende
// Eingabe-Control (control) samt Lesefunktion (read). Wird vom Eingabemasken-
// Designer, von der Laufzeit-Maske und vom Dialog „Instanzdaten eingeben"
// gemeinsam genutzt. ``read()`` liefert ``undefined`` fuer ein leeres Feld,
// sonst den typisierten Wert (``coerceTypedInput``) -- oder, wenn die Eingabe
// nicht eindeutig passt, den rohen Text, den der Kern mit D3 ablehnt.
// Zahlenfelder tragen ``step="any"``: ohne das markiert der Browser 499,99 als
// ungueltig, obwohl der Wert gespeichert wird.
function maskControl(elem, widget, options, current) {
  const dtype = elem ? elem.data_type : "STRING";
  const coerce = (raw) => coerceTypedInput(dtype, raw);
  if (widget === "YESNO") {
    const input = el("select", null,
      el("option", { value: "" }, "\u2013 bitte w\u00E4hlen \u2013"),
      el("option", { value: "true" }, "Ja"),
      el("option", { value: "false" }, "Nein"));
    if (current === true || current === false) input.value = String(current);
    return { control: input, read: () => (input.value === "" ? undefined : input.value === "true") };
  }
  if (widget === "CHECKBOX") {
    const input = el("input", { type: "checkbox" });
    if (current === true || current === "true" || current === "1") input.checked = true;
    return { control: input, read: () => input.checked };
  }
  if (widget === "TEXTAREA") {
    const input = el("textarea", { rows: "3", placeholder: elem ? elem.name : "" });
    if (current != null) input.value = String(current);
    return { control: input, read: () => (input.value === "" ? undefined : input.value) };
  }
  if (widget === "DROPDOWN") {
    const input = el("select", null,
      el("option", { value: "" }, "\u2013 bitte w\u00E4hlen \u2013"),
      ...(options || []).map((o) => el("option", { value: o }, o)));
    if (current != null) input.value = String(current);
    return { control: input, read: () => (input.value === "" ? undefined : input.value) };
  }
  const type = widget === "NUMBER" ? "number" : widget === "DATE" ? "date" : "text";
  const attrs = { type, placeholder: elem ? elem.name : "" };
  if (widget === "NUMBER") attrs.step = dtype === "INTEGER" ? "1" : dtype === "DECIMAL" ? "0.01" : "any";
  const input = el("input", attrs);
  if (current != null) input.value = String(current);
  return { control: input, read: () => (input.value === "" ? undefined : coerce(input.value)) };
}

/**
 * Pruefregeln eines Maskenfelds, soweit sie zum Bedienelement passen:
 * Unter-/Obergrenze fuer Zahlenfelder, Muster und Hoechstlaenge fuer Text.
 * Nur fuer Eingabefelder -- ein Anzeigefeld prueft nichts.
 * @param {object} f Feld des Designers
 * @returns {{min_value: (number|null), max_value: (number|null), pattern: (string|null), max_length: (number|null)}}
 */
function fieldRulesFor(f) {
  const writes = f.mode !== "READ";
  const num = writes && f.widget === "NUMBER";
  const text = writes && (f.widget === "TEXT" || f.widget === "TEXTAREA");
  const n = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v));
  return {
    min_value: num ? n(f.min_value) : null,
    max_value: num ? n(f.max_value) : null,
    pattern: text && f.pattern ? f.pattern : null,
    max_length: text ? n(f.max_length) : null,
  };
}

/**
 * Eingaben fuer die Pruefregeln eines Felds im Designer. Welche
 * erscheinen, haengt am Bedienelement; ob sie zusammenpassen (Grenzen in der
 * richtigen Reihenfolge, gueltiges Muster), prueft der Kern (U2).
 * @param {object} f Feld des Designers (wird direkt beschrieben)
 * @returns {HTMLElement[]} Zellen fuer die Feldzeile
 */
function fieldRuleCells(f) {
  if (f.mode === "READ") return [];
  const numInput = (key, placeholder) => {
    const i = el("input", { type: "number", step: "any", placeholder, value: f[key] ?? "" });
    i.addEventListener("input", () => { f[key] = i.value === "" ? null : Number(i.value); });
    return i;
  };
  if (f.widget === "NUMBER") {
    return [
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Mindestens"), numInput("min_value", "optional")),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "H\u00F6chstens"), numInput("max_value", "optional")),
    ];
  }
  if (f.widget === "TEXT" || f.widget === "TEXTAREA") {
    const pat = el("input", { type: "text", value: f.pattern || "", placeholder: "optional, z. B. [A-Z]{2}-\\d{4}" });
    pat.addEventListener("input", () => { f.pattern = pat.value.trim() || null; });
    return [
      el("div", { class: "fd-cell fd-wide" }, el("span", { class: "fd-cap" }, "Muster (regul\u00E4rer Ausdruck)"), pat),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "H\u00F6chstl\u00E4nge"), numInput("max_length", "optional")),
    ];
  }
  return [];
}

/**
 * Prueft einen Wert gegen die Pruefregeln eines Maskenfelds -- dieselben wie
 * der Kern beim Abschliessen (U4). Nur Komfort: Der Nutzer sieht das Problem
 * am Feld, bevor er absendet. Maßgeblich bleibt der Server.
 * @param {object} f Maskenfeld (FormField)
 * @param {*} val gelesener, typisierter Wert
 * @returns {string|null} deutscher Hinweis oder null
 */
function fieldRuleProblem(f, val) {
  if (typeof val === "number") {
    if (f.min_value != null && val < f.min_value) return `mindestens ${f.min_value}`;
    if (f.max_value != null && val > f.max_value) return `h\u00F6chstens ${f.max_value}`;
  }
  if (typeof val === "string") {
    if (f.max_length != null && val.length > f.max_length) return `h\u00F6chstens ${f.max_length} Zeichen`;
    if (f.pattern) {
      try { if (!new RegExp(`^(?:${f.pattern})$`).test(val)) return "passt nicht zum vorgegebenen Format"; }
      catch (e) { /* ungueltiges Muster: der Kern prueft */ }
    }
  }
  return null;
}

/**
 * Markiert ein Feld der Aufgabenmaske als fehlerhaft (oder hebt die Markierung
 * auf). Pflicht- und Regelfehler standen bisher nur in einer Meldung, nicht am
 * Feld.
 * @param {HTMLElement|undefined} wrap das ``label.field`` des Felds
 * @param {string|null} message Hinweis oder null zum Aufheben
 */
function markField(wrap, message) {
  if (!wrap) return;
  const old = wrap.querySelector(".field-error-msg");
  if (old) old.remove();
  wrap.classList.toggle("field-invalid", !!message);
  const control = wrap.querySelector("input, select, textarea");
  if (control) {
    if (message) control.setAttribute("aria-invalid", "true");
    else control.removeAttribute("aria-invalid");
  }
  if (message) wrap.appendChild(el("span", { class: "field-error-msg" }, message));
}

// Visueller Eingabemasken-Designer: Felder per Auswahl zusammenstellen; die
// Anordnung entsteht automatisch (geordnete Liste -> Grid). Jedes Feld wird auf
// einen Datenzugriff abgebildet, daher gilt Correctness by Construction (der
// Kern lehnt u.a. jedes Lesefeld ohne vorheriges Schreiben ab -- D1).
function openFormDesigner(nodeId) {
  const schema = state.schema;
  const elements = Object.values(schema.data_elements || {});
  if (!elements.length) {
    toast("info", "Zuerst Datenelemente in der Datensicht anlegen.");
    return;
  }
  const existing = (schema.forms || {})[nodeId];
  let title = existing ? existing.title : "";
  let columns = existing ? existing.columns || 1 : 1;
  // help_text und group werden mitgefuehrt: Frueher fehlte help_text hier, und
  // ein blosses Oeffnen + Speichern loeschte die Hilfetexte einer Maske.
  const fields = existing
    ? existing.fields.map((f) => ({
        element_id: f.element_id, widget: f.widget, label: f.label,
        mode: f.mode, required: f.required, options: (f.options || []).slice(),
        help_text: f.help_text || null, group: f.group || "",
        // Pruefregeln mitfuehren -- sonst loeschte Oeffnen + Speichern sie.
        min_value: f.min_value ?? null, max_value: f.max_value ?? null,
        pattern: f.pattern || null, max_length: f.max_length ?? null,
      }))
    : [];
  const container = el("div", { class: "form-designer" });

  const defaultField = () => {
    const elem = elements[0];
    return {
      element_id: elem.id, widget: WIDGETS_FOR_TYPE[elem.data_type][0],
      label: elem.name, mode: "WRITE", required: true, options: [], help_text: null, group: "",
    };
  };

  function previewMask() {
    if (!fields.length) return el("div", { class: "muted", style: "font-size:12px" }, "Noch keine Felder.");
    return maskLayout(fields.map((f) => {
      const elem = schema.data_elements[f.element_id];
      const { control } = maskControl(elem, f.widget, f.options, null);
      control.setAttribute("disabled", "disabled");
      return { group: f.group, node: el("label", { class: "field" },
        (f.label || (elem ? elem.name : f.element_id)) + (showsRequiredMark(f) ? " *" : ""), control) };
    }), columns);
  }

  function fieldRow(f, idx) {
    const elem = schema.data_elements[f.element_id];
    const elemSel = el("select", null,
      ...elements.map((e) => el("option", { value: e.id }, `${e.name} (${typeName(e.data_type)})`)));
    elemSel.value = f.element_id;
    elemSel.addEventListener("change", () => {
      const prev = schema.data_elements[f.element_id];
      f.element_id = elemSel.value;
      const next = schema.data_elements[f.element_id];
      // Keep the label in sync while it is still the untouched default.
      if (!f.label || (prev && f.label === prev.name)) f.label = next.name;
      if (!WIDGETS_FOR_TYPE[next.data_type].includes(f.widget)) f.widget = WIDGETS_FOR_TYPE[next.data_type][0];
      if (f.widget !== "DROPDOWN") f.options = [];
      renderDesigner();
    });
    const allowed = elem ? WIDGETS_FOR_TYPE[elem.data_type] : ["TEXT"];
    const widgetSel = el("select", null,
      ...allowed.map((w) => el("option", { value: w }, WIDGET_LABELS[w])));
    widgetSel.value = f.widget;
    widgetSel.addEventListener("change", () => {
      f.widget = widgetSel.value;
      if (f.widget !== "DROPDOWN") f.options = [];
      renderDesigner();
    });
    const labelInput = el("input", { type: "text", value: f.label });
    labelInput.addEventListener("input", () => { f.label = labelInput.value; });
    const modeSel = el("select", null,
      el("option", { value: "WRITE" }, "Eingabe (schreibt)"),
      el("option", { value: "READ" }, "Anzeige (liest)"));
    modeSel.value = f.mode;
    modeSel.addEventListener("change", () => { f.mode = modeSel.value; renderDesigner(); });
    const reqBox = el("input", { type: "checkbox" });
    reqBox.checked = f.required;
    reqBox.addEventListener("change", () => { f.required = reqBox.checked; });
    const groupInput = el("input", { type: "text", value: f.group || "", placeholder: "optional" });
    // Nur die Vorschau erneuern: ``change`` feuert beim Verlassen des Felds --
    // also schon beim Druecken auf „+ Feld hinzufuegen“. Ein Neuaufbau des
    // ganzen Designers ersetzte den Knopf dann vor dem Loslassen, und der
    // erste Klick ging verloren.
    groupInput.addEventListener("input", () => { f.group = groupInput.value.trim(); });
    groupInput.addEventListener("change", () => { f.group = groupInput.value.trim(); refreshPreview(); });

    const cells = [
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Datenelement"), elemSel),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Darstellung"), widgetSel),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Beschriftung"), labelInput),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Richtung"), modeSel),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Gruppe"), groupInput),
      // Ein Ankreuzfeld hat immer einen Wert -- „Pflicht“ waere wirkungslos
      // (siehe ``showsRequiredMark``) und wird deshalb nicht angeboten.
      f.widget === "CHECKBOX"
        ? el("div", { class: "fd-cell fd-req muted", style: "font-size:12px" },
            "Ja/Nein hat immer einen Wert")
        : el("div", { class: "fd-cell fd-req" },
            el("label", { class: "row", style: "gap:6px;align-items:center" }, reqBox, "Pflicht")),
    ];
    if (f.widget === "DROPDOWN") {
      const optInput = el("input", {
        type: "text", value: (f.options || []).join(", "), placeholder: "Option A, Option B",
      });
      optInput.addEventListener("input", () => {
        f.options = optInput.value.split(",").map((s) => s.trim()).filter((s) => s.length);
      });
      cells.push(el("div", { class: "fd-cell fd-wide" },
        el("span", { class: "fd-cap" }, "Optionen (kommagetrennt)"), optInput));
    }
    cells.push(...fieldRuleCells(f));
    cells.push(el("button", { class: "btn small danger fd-del", onClick: () => { fields.splice(idx, 1); renderDesigner(); } }, "Entfernen"));
    return el("div", { class: "fd-field" }, ...cells);
  }

  function renderDesigner() {
    clear(container);
    const titleInput = el("input", { type: "text", value: title, placeholder: "Titel der Maske (optional)" });
    titleInput.addEventListener("input", () => { title = titleInput.value; });
    container.appendChild(el("label", { class: "field" }, "Maskentitel", titleInput));
    const colSel = el("select", null,
      ...[1, 2, 3].map((n) => el("option", { value: String(n) }, n === 1 ? "1 Spalte" : `${n} Spalten`)));
    colSel.value = String(columns);
    colSel.addEventListener("change", () => { columns = Number(colSel.value); renderDesigner(); });
    container.appendChild(el("label", { class: "field" }, "Anordnung (am Smartphone immer einspaltig)", colSel));

    const list = el("div", { class: "fd-list" });
    fields.forEach((f, idx) => list.appendChild(fieldRow(f, idx)));
    container.appendChild(list);

    container.appendChild(el("button", {
      class: "btn small", onClick: () => { fields.push(defaultField()); renderDesigner(); },
    }, "+ Feld hinzuf\u00FCgen"));

    previewBox = el("div", { class: "fd-preview" },
      el("div", { class: "fd-preview-h" }, "Vorschau"),
      previewMask());
    container.appendChild(previewBox);
  }

  // Erneuert nur die Vorschau -- die Eingabefelder (und Knoepfe) bleiben, wo
  // sie sind; siehe Gruppe in fieldRow.
  let previewBox = null;
  function refreshPreview() {
    if (!previewBox) return;
    clear(previewBox);
    previewBox.appendChild(el("div", { class: "fd-preview-h" }, "Vorschau"));
    previewBox.appendChild(previewMask());
  }

  renderDesigner();
  openModal(existing ? "Eingabemaske bearbeiten" : "Eingabemaske gestalten", container, async () => {
    if (!fields.length) { toast("err", "Mindestens ein Feld ist erforderlich."); return false; }
    const payload = {
      title,
      columns,
      fields: fields.map((f) => ({
        element_id: f.element_id, widget: f.widget, label: f.label,
        mode: f.mode, required: f.required,
        options: f.widget === "DROPDOWN" ? f.options : [],
        help_text: f.help_text || null, group: f.group || "",
        // Pruefregeln nur, wo sie zum Bedienelement passen (sonst U2).
        ...fieldRulesFor(f),
      })),
    };
    try {
      await api.post(`/schemas/${state.schemaId}/nodes/${nodeId}/form`, payload);
      await refreshSchema();
      render();
      toast("ok", "Eingabemaske gespeichert");
    } catch (err) { toastError(err); return false; }
  }, "Speichern");
}

function validationBadge() {
  if (!state.validation) return el("span", null, "");
  if (state.validation.correct) return el("span", { class: "pill pill-green" }, "korrekt");
  return el("span", { class: "pill pill-red" }, countLabel(state.validation.findings.length, "Befund", "Befunde"));
}

function findingsPanel() {
  const v = state.validation;
  const body = el("div", { class: "panel-b" });
  if (!v || v.correct) {
    body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Strukturell korrekt (K/D/Z/A/C/H/F/U erf\u00FCllt)."));
  } else {
    v.findings.forEach((f) => body.appendChild(el("div", { class: "finding" },
      el("span", { class: "rule" }, f.rule),
      el("span", null, findingText(f, { withHint: true })))));
  }
  // Freigabereife (Stufe B) wie in der Statusleiste der Karten-Sicht: ein
  // Entwurf darf unfertig sein, soll es aber vor der Freigabe sehen. Dieselbe
  // Quelle (releaseFindings) -- beide Oberflaechen zeigen dasselbe.
  const notReady = isDraft(state.schema) ? releaseFindings() : [];
  if (notReady.length) {
    body.appendChild(el("div", { class: "warn-banner", style: "margin-top:8px" },
      `${countLabel(notReady.length, "Schritt", "Schritte")} ohne Bearbeiter \u2013 erst danach l\u00E4sst sich freigeben.`));
    notReady.forEach((f) => body.appendChild(el("div", { class: "finding" },
      el("span", { class: "rule" }, f.rule),
      el("span", null, findingText(f, { withHint: true })))));
  }
  // Modellhinweise (G-Gruppe, /metrics): beratend, kein Korrektheitsurteil.
  // Bewusst im selben Panel, aber klar abgesetzt \u2013 ein Hinweis ist kein Befund.
  const hints = state.hints || [];
  if (hints.length) {
    body.appendChild(el("div", { class: "hr" }));
    body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
      "Hinweise (beratend, blockieren nichts):"));
    hints.forEach((h) => body.appendChild(el("div", { class: "finding" },
      el("span", { class: "rule rule-hint" }, h.code),
      // Anklickbar: waehlt den betroffenen Schritt im Kontrollfluss aus --
      // so findet man auch gleichartige, unbenannte Knoten.
      h.node_id && state.schema && state.schema.nodes[h.node_id]
        ? el("a", { href: "#", title: "Im Kontrollfluss zeigen",
            onClick: (e) => { e.preventDefault(); state.selectedNode = h.node_id; render(); } },
            hintText(h, state.schema))
        : el("span", null, hintText(h, state.schema)))));
  }
  return el("div", { class: "panel", "data-tour": "model.findings" },
    el("div", { class: "panel-h" }, el("h2", null, "Korrektheit"), el("span", { class: "sub" }, "live vom Kern")), body);
}

/** Grund fuer jede Sperre, die nur ein Entwurf aufhebt (siehe ``lockedBy``). */
const DRAFT_ONLY_REASON = "Freigegeben \u2013 \u00E4nderbar nur in einer neuen Revision (Modellieren \u2192 \u201ENeue Revision\u201C).";

/**
 * Sperre eines Knopfs **mit Grund**: liefert ``disabled`` und den Grund als
 * ``title`` (Tooltip) fuer ``el()``.
 *
 * Gesperrte Knoepfe ohne Grund (freigegebenes Schema, fehlender Connector)
 * liessen raten, warum nichts geht. Jede Sperre nennt jetzt die erste
 * zutreffende Bedingung.
 * @param {...Array} reasons Paare ``[Bedingung, Grund]`` in Prioritaetsfolge
 * @returns {{disabled?: boolean, title?: string}} leeres Objekt, wenn nichts
 *   sperrt -- zum Einspreizen in die Attribute (``...lockedBy(...)``)
 */
function lockedBy(...reasons) {
  const hit = reasons.find(([cond]) => cond);
  return hit ? { disabled: true, title: hit[1] } : {};
}

function revisionPanel() {
  const schema = state.schema;
  if (isDraft(schema)) return el("div");
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Schema-Evolution")),
    el("div", { class: "panel-b row" },
      el("span", { class: "muted", style: "font-size:12px;flex:1" }, "Eine neue Revision erzeugt eine bearbeitbare Entwurfskopie als neue Version; die Schritte behalten ihre Kennungen, damit laufende Vorgänge sich übernehmen lassen."),
      el("button", { class: "btn small", onClick: newRevision }, "Neue Revision")));
}

/** Vorbelegte Hoechstzahl der Schleifen-Durchlaeufe im Einfuege-Dialog. */
const LOOP_MAX_DEFAULT = 10;

/** Wert der Auswahl „Neues Merkmal anlegen …“ in ``discriminatorPicker``. */
const NEW_DISCRIMINATOR = "__new__";

/**
 * Auswahl des steuernden Merkmals mit „＋ Neues Merkmal anlegen …“.
 *
 * Gemeinsam fuer die Entscheidung (XOR) und die Schleife im Einfuegedialog --
 * beide Modellier-Oberflaechen oeffnen denselben Dialog. Frueher bot nur die
 * Entscheidung die Neuanlage; wer eine Schleife ueber ein noch nicht
 * vorhandenes Ja/Nein-Merkmal bauen wollte, musste den Dialog abbrechen.
 * Angelegt wird erst beim Bestaetigen (``withDiscriminator``); ob das Merkmal
 * passt, entscheidet der Kern.
 *
 * @param {object[]} elements waehlbare vorhandene Datenelemente
 * @param {Array<[string, string]>} newTypes Typen fuer ein neues Merkmal als
 *   [Datentyp, Beschriftung]; der erste ist vorgeschlagen
 * @param {string} cls CSS-Klasse der Auswahl (Tests, Tour)
 * @param {string} placeholder Beispielname im Namensfeld
 * @returns {{select: object, box: object, nameInput: object, typeSelect: object,
 *   isNew: function(): boolean, dataType: function(): (string|undefined),
 *   choice: function(): {existing: (string|null), newName: string, newType: string}}}
 *   ``box`` (Name und Art) ist nur bei „Neues Merkmal“ sichtbar. Ohne
 *   vorhandene Elemente ist „Neues Merkmal“ vorgewaehlt. ``dataType`` liefert
 *   den Typ der aktuellen Wahl (bei Neuanlage den gewaehlten Typ).
 */
function discriminatorPicker(elements, newTypes, cls, placeholder) {
  const select = el("select", { class: cls },
    ...elements.map((d) => el("option", { value: d.id }, `${d.name} (${typeName(d.data_type)})`)),
    el("option", { value: NEW_DISCRIMINATOR }, "\uFF0B Neues Merkmal anlegen \u2026"));
  const nameInput = el("input", { type: "text", placeholder });
  const typeSelect = el("select", null, ...newTypes.map(([v, label]) => el("option", { value: v }, label)));
  const box = el("div", { class: "form-grid new-disc" },
    el("label", { class: "field" }, "Name des Merkmals", nameInput),
    el("label", { class: "field" }, "Art", typeSelect));
  if (!elements.length) select.value = NEW_DISCRIMINATOR;
  const isNew = () => select.value === NEW_DISCRIMINATOR;
  const sync = () => { box.style.display = isNew() ? "" : "none"; };
  select.addEventListener("change", sync);
  sync();
  return {
    select, box, nameInput, typeSelect, isNew,
    dataType: () => (isNew() ? typeSelect.value : (state.schema.data_elements[select.value] || {}).data_type),
    choice: () => ({ existing: isNew() ? null : select.value, newName: nameInput.value.trim(), newType: typeSelect.value }),
  };
}

/**
 * Fuehrt eine Einfuege-Operation mit ihrem Merkmal aus und legt es bei Bedarf
 * vorher an.
 *
 * Neuanlage ist eine eigene Kern-Operation (``POST …/data-elements``), danach
 * laeuft ``run`` mit der Kennung. Scheitert ``run``, wird das hier angelegte
 * Element wieder geloescht -- kein halbes Merkmal bleibt im Modell; die
 * Meldung des Kerns wird weitergereicht.
 *
 * @param {string} sid Schema
 * @param {{existing: (string|null), newName: string, newType: string}} opt
 *   ``existing`` = vorhandenes Element; sonst Name und Typ des neuen
 * @param {function(string): Promise<*>} run Operation mit der Merkmal-Kennung
 * @returns {Promise<*>} Ergebnis von ``run``
 * @throws {{detail: string}} ohne Namen fuer ein neues Merkmal; sonst den
 *   Fehler der Anlage bzw. von ``run``
 */
async function withDiscriminator(sid, opt, run) {
  let created = null;
  try {
    let disc = opt.existing;
    if (!disc) {
      if (!opt.newName) throw { detail: "Bitte einen Namen f\u00FCr das neue Merkmal angeben." };
      const before = new Set(Object.keys(state.schema.data_elements || {}));
      const schema = await api.post(`/schemas/${sid}/data-elements`, { name: opt.newName, data_type: opt.newType });
      created = Object.values(schema.data_elements || {}).find((d) => !before.has(d.id)) || null;
      if (!created) throw { detail: "Das neue Merkmal wurde nicht angelegt." };
      disc = created.id;
    }
    return await run(disc);
  } catch (err) {
    // Aufraeumen; ein Fehler dabei wird verschluckt, die eigentliche Ursache
    // ist die Meldung von oben.
    if (created) { try { await api.del(`/schemas/${sid}/data-elements/${created.id}`); } catch (e) { /* egal */ } }
    throw err;
  }
}

/**
 * Zweigzeilen des Einfuegedialogs „Bedingt (XOR)“.
 *
 * Welche Zeilen es gibt, haengt am Typ des gewaehlten Merkmals: Zahl ->
 * Stufen mit Obergrenze (die letzte ohne), Ja/Nein -> genau die Zeilen
 * „wahr“ und „falsch“, Text -> Wertzeilen plus ein fester Sonst-Zweig. Die
 * Zeilen sind reine Eingabe; ob die Partition vollstaendig und
 * ueberschneidungsfrei ist, prueft der Kern.
 *
 * @param {{dataType: function(): ?string}} condPick Merkmal-Auswahl aus
 *   ``discriminatorPicker`` (liefert den Typ des gewaehlten bzw. neuen Merkmals)
 * @returns {{rows: HTMLElement, kind: function(): ?string,
 *   rebuild: function(): void, addRow: function(): void}}
 *   ``rows`` ist der Container der Zeilen; ``kind`` liefert
 *   ``"THRESHOLD"``/``"BOOLEAN"``/``"ENUM"`` oder ``null`` (kein verzweigbarer
 *   Typ); ``rebuild`` setzt die Zeilen fuer den aktuellen Typ neu auf (leer bei
 *   ``null``); ``addRow`` fuegt eine Stufe vor der offenen obersten bzw. einen
 *   Wert vor dem Sonst-Zweig ein -- bei Ja/Nein tut es nichts.
 */
function xorBranchRowsEditor(condPick) {
  const condRows = el("div", { class: "row", style: "flex-direction:column;align-items:stretch;gap:8px" });
  function discKind() {
    const type = condPick.dataType();
    if (isNumericType(type)) return "THRESHOLD";
    if (type === "BOOLEAN") return "BOOLEAN";
    if (type === "STRING") return "ENUM";
    return null;
  }
  function addThresholdRow(last) {
    condRows.appendChild(el("div", { class: "branch-row threshold-row" },
      el("input", { type: "text", class: "cond-label", placeholder: "Bezeichnung" }),
      el("input", { type: "number", class: "cond-upper", placeholder: last ? "Obergrenze leer = bis +\u221E" : "unter \u2026" })));
  }
  function addEnumRow() {
    condRows.appendChild(el("div", { class: "branch-row enum-row" },
      el("input", { type: "text", class: "cond-label", placeholder: "Bezeichnung" }),
      el("input", { type: "text", class: "cond-values", placeholder: "Werte, kommagetrennt" })));
  }
  function rebuildCondRows() {
    clear(condRows);
    const kind = discKind();
    if (kind === "THRESHOLD") { addThresholdRow(false); addThresholdRow(true); }
    else if (kind === "BOOLEAN") {
      condRows.appendChild(el("div", { class: "branch-row bool-row" },
        el("span", { class: "muted" }, "wahr"),
        el("input", { type: "text", class: "cond-label", "data-bool": "true", placeholder: "Bezeichnung" })));
      condRows.appendChild(el("div", { class: "branch-row bool-row" },
        el("span", { class: "muted" }, "falsch"),
        el("input", { type: "text", class: "cond-label", "data-bool": "false", placeholder: "Bezeichnung" })));
    } else if (kind === "ENUM") {
      addEnumRow(); addEnumRow();
      condRows.appendChild(el("div", { class: "branch-row else-row" },
        el("span", { class: "muted" }, "Sonst (otherwise)"),
        el("input", { type: "text", class: "cond-label", "data-else": "1", placeholder: "Bezeichnung" })));
    }
  }
  function addCondRow() {
    const kind = discKind();
    if (kind === "THRESHOLD") {
      const rows = condRows.querySelectorAll(".threshold-row");
      addThresholdRow(false);
      if (rows.length) condRows.insertBefore(condRows.lastChild, rows[rows.length - 1]);
    } else if (kind === "ENUM") {
      const elseRow = condRows.querySelector(".else-row");
      addEnumRow();
      if (elseRow) condRows.insertBefore(condRows.lastChild, elseRow);
    }
  }
  return { rows: condRows, kind: discKind, rebuild: rebuildCondRows, addRow: addCondRow };
}

/**
 * Felder der Wiederhol-Bedingung im Einfuegedialog „Schleife“.
 *
 * Je nach Typ des Merkmals zeigt ``rows`` genau ein Eingabemuster: Ja/Nein ->
 * Wiederhol-Wert (Kurzform), Zahl -> Vergleich und Grenze, Text -> Liste der
 * Wiederhol-Werte. Die Felder bleiben ueber einen Typwechsel hinweg dieselben
 * (ihre Eingaben gehen nicht verloren); ``rebuild`` haengt nur das passende
 * Muster ein. Was daraus im Auftrag wird, entscheidet der Aufrufer, die
 * Gueltigkeit der Partition der Kern.
 *
 * @param {{dataType: function(): ?string}} loopPick Merkmal-Auswahl aus
 *   ``discriminatorPicker``
 * @returns {{rows: HTMLElement, kind: function(): ?string,
 *   rebuild: function(): void, repeat: HTMLSelectElement,
 *   cmp: HTMLSelectElement, bound: HTMLInputElement, values: HTMLInputElement}}
 *   ``kind`` liefert ``"BOOLEAN"``/``"ENUM"``/``"THRESHOLD"`` oder ``null``,
 *   solange kein Typ feststeht (dann bleibt ``rows`` leer). Jeder andere Typ
 *   als Ja/Nein und Text gilt als Zahl.
 */
function loopConditionEditor(loopPick) {
  const loopRepeat = el("select", null,
    el("option", { value: "true" }, "wahr"),
    el("option", { value: "false" }, "falsch"));
  const loopCmp = el("select", { class: "loop-cmp" },
    el("option", { value: "gte" }, "größer/gleich der Grenze ist (≥)"),
    el("option", { value: "lt" }, "unter der Grenze liegt (<)"));
  const loopBound = el("input", { type: "number", class: "loop-bound", placeholder: "z. B. 1" });
  const loopValues = el("input", { type: "text", class: "loop-values", placeholder: "Werte, kommagetrennt – z. B. nacharbeit" });
  const loopRows = el("div", { class: "row", style: "flex-direction:column;align-items:stretch;gap:8px" });
  function loopKind() {
    const type = loopPick.dataType();
    if (!type) return null;
    if (type === "BOOLEAN") return "BOOLEAN";
    if (type === "STRING") return "ENUM";
    return "THRESHOLD";
  }
  function rebuildLoopRows() {
    clear(loopRows);
    const kind = loopKind();
    if (kind === "BOOLEAN") {
      loopRows.appendChild(el("label", { class: "field" }, "Wiederholen, solange der Wert", loopRepeat));
    } else if (kind === "THRESHOLD") {
      loopRows.appendChild(el("label", { class: "field" }, "Wiederholen, solange der Wert", loopCmp));
      loopRows.appendChild(el("label", { class: "field" }, "Grenze", loopBound));
    } else if (kind === "ENUM") {
      loopRows.appendChild(el("label", { class: "field" },
        "Wiederholen bei diesen Werten (alle anderen verlassen die Schleife)", loopValues));
    }
  }
  return { rows: loopRows, kind: loopKind, rebuild: rebuildLoopRows,
    repeat: loopRepeat, cmp: loopCmp, bound: loopBound, values: loopValues };
}

/**
 * Einfuegedialog hinter einem Knoten bzw. auf einer Kante.
 *
 * @param {string} afterNodeId Quelle der Kante (Anker)
 * @param {string} [beforeNodeId] Ziel der Kante. Nur noetig, wenn der Anker
 *   mehrere Ausgaenge hat (Anfang eines Zweigs, auch eines leeren): dann wird
 *   genau auf dieser Kante eingefuegt, und angeboten wird nur ein einzelner
 *   Schritt -- Verzweigung oder Schleife direkt am Zweiganfang kann der Kern
 *   (noch) nicht.
 */
function openInsertModal(afterNodeId, beforeNodeId) {
  const node = state.schema.nodes[afterNodeId];
  const exits = controlEdges(state.schema).filter((e) => e.source === afterNodeId);
  const branchStart = exits.length > 1 && typeof beforeNodeId === "string" ? beforeNodeId : null;
  let active = "serial";
  const serialBody = el("label", { class: "field" }, "Bezeichnung",
    el("input", { type: "text", id: "ins-label", placeholder: "z. B. Antrag pr\u00FCfen" }));
  const parBox = el("div", { class: "row", style: "flex-direction:column;align-items:stretch;gap:8px" });
  function addParRow(val) {
    parBox.appendChild(el("input", { type: "text", class: "par-branch", placeholder: "Zweig-Bezeichnung", value: val || "" }));
  }
  // --- XOR partition builder (K7): a typed discriminator drives the branches.
  const partitionable = Object.values(state.schema.data_elements).filter(
    (d) => d.source === "INSTANCE" && ["INTEGER", "FLOAT", "DECIMAL", "BOOLEAN", "STRING"].includes(d.data_type));
  // Das Merkmal muss nicht vorher woanders angelegt UND an einem Schritt
  // davor geschrieben werden, sonst schickte der Dialog den Nutzer weg. Die Wahl
  // „Neues Merkmal …“ legt es hier an, und der Schritt vor der Einfuegestelle
  // bekommt die Schreibbindung gleich mit (``writerStep``). Ob das alles
  // zusammen korrekt ist (D1, D2, K7), entscheidet weiterhin der Kern.
  const writerStep = node && node.type === "ACTIVITY" ? node : null;
  const condPick = discriminatorPicker(partitionable, [
    ["FLOAT", "Zahl (Stufen nach Grenzwerten)"],
    ["DECIMAL", "Betrag (Stufen nach Grenzwerten)"],
    ["BOOLEAN", "Ja/Nein"],
    ["STRING", "Text (Zweige nach Werten)"],
  ], "cond-disc", "z. B. Betrag");
  const condDisc = condPick.select;
  const bindBox = el("input", { type: "checkbox" });
  const bindRow = writerStep
    ? el("label", { class: "row", style: "gap:8px;align-items:center;font-size:12px" }, bindBox,
        `„${nodeCaption(writerStep)}“ setzt dieses Merkmal (Schreibbindung ergänzen)`)
    : el("div", { class: "muted", style: "font-size:12px" },
        "Vor dieser Stelle liegt kein Aufgaben-Schritt, der das Merkmal setzen könnte – es muss schon früher im Ablauf geschrieben werden.");
  function writesAlready(eid) {
    return !!writerStep && (state.schema.data_accesses || []).some((a) =>
      a.node_id === writerStep.id && a.element_id === eid && (a.mode === "WRITE" || a.mode === "READ_WRITE"));
  }
  function syncDiscChoice() {
    const isNew = condPick.isNew();
    // Vorbelegung: binden, wenn der Schritt davor das Merkmal noch nicht schreibt.
    bindBox.checked = isNew || !writesAlready(condDisc.value);
    bindBox.disabled = isNew;  // ein neues Merkmal braucht einen Schreiber
  }
  const condEditor = xorBranchRowsEditor(condPick);
  const { rows: condRows, kind: discKind } = condEditor;
  condDisc.addEventListener("change", () => { syncDiscChoice(); condEditor.rebuild(); });
  condPick.typeSelect.addEventListener("change", condEditor.rebuild);
  addParRow(); addParRow(); syncDiscChoice(); condEditor.rebuild();
  const condPanel = el("div", null,
    el("label", { class: "field" }, "Entscheiden nach (Merkmal)", condDisc),
    condPick.box,
    bindRow,
    el("div", { class: "muted", style: "font-size:12px;margin:4px 0" }, "Die Engine w\u00E4hlt den Zweig automatisch anhand des Werts \u2013 vollst\u00E4ndig und \u00FCberschneidungsfrei (K7)."),
    condRows, el("button", { class: "btn small ghost", onClick: () => condEditor.addRow() }, "+ Zweig"));
  // --- Schleife (K6): Rumpf-Bezeichnung + entscheidbares Wiederholen-Merkmal.
  // BOOLEAN nutzt die Kurzform (repeat_value); Zahlen (Schwelle) und Text
  // (Wertemenge) bauen eine Wiederhol/Verlassen-Partition (Stufe S3, K6b).
  // Die Rumpfaktivität erhält vom Kern automatisch den Pflicht-Schreibzugriff
  // auf das Merkmal (K6c: jede Iteration entscheidet auf frischen Daten).
  const loopable = Object.values(state.schema.data_elements).filter(
    (d) => d.source === "INSTANCE" && ["BOOLEAN", "INTEGER", "FLOAT", "DECIMAL", "STRING"].includes(d.data_type));
  // Neues Merkmal wie bei der Entscheidung (gemeinsame Auswahl); Ja/Nein ist
  // vorgeschlagen, wie die Anleitung es empfiehlt.
  const loopPick = discriminatorPicker(loopable, [
    ["BOOLEAN", "Ja/Nein (empfohlen)"],
    ["INTEGER", "Zahl (Wiederholen ab/unter einer Grenze)"],
    ["STRING", "Text (Wiederholen bei bestimmten Werten)"],
  ], "loop-disc", "z. B. Nacharbeit n\u00F6tig");
  const loopDisc = loopPick.select;
  const loopEditor = loopConditionEditor(loopPick);
  const { repeat: loopRepeat, cmp: loopCmp, bound: loopBound, values: loopValues,
    rows: loopRows, kind: loopKind } = loopEditor;
  loopDisc.addEventListener("change", loopEditor.rebuild);
  loopPick.typeSelect.addEventListener("change", loopEditor.rebuild);
  loopEditor.rebuild();
  // Optionale Notbremse (S3): Höchstzahl der Durchläufe. Deterministisch –
  // am Limit wird verlassen, auch wenn die Daten „wiederholen“ sagen; die
  // Zeitprüfung (T2) rechnet den Rumpf dann entsprechend oft.
  // Vorbelegt mit LOOP_MAX_DEFAULT: eine Schleife
  // ohne Obergrenze ist moeglich, soll aber eine bewusste Entscheidung sein
  // und keine Voreinstellung. Pflicht ist sie nicht -- das waere eine
  // Verschaerfung von K6b fuer den gesamten Bestand.
  const loopMax = el("input", { type: "number", class: "loop-max", min: "2",
    value: String(LOOP_MAX_DEFAULT), placeholder: "leer = unbegrenzt" });
  const loopPanel = el("div", null,
    el("label", { class: "field" }, "Bezeichnung des Wiederhol-Schritts",
      el("input", { type: "text", id: "loop-label", placeholder: "z. B. Nacharbeit erledigen" })),
    el("label", { class: "field" }, "Wiederholen-Merkmal (Datenelement)", loopDisc),
    loopPick.box,
    loopRows,
    el("label", { class: "field" }, "Höchstzahl Durchläufe (Notbremse, mind. 2 – leeren nur, wenn die Schleife wirklich unbegrenzt laufen darf)", loopMax),
    el("div", { class: "muted", style: "font-size:12px;margin:4px 0" },
      "Der Schritt läuft mindestens einmal; am Ende jeder Runde entscheidet das Merkmal automatisch, ob wiederholt wird (K6). Der Schritt schreibt das Merkmal verbindlich – jede Runde entscheidet auf frischen Daten."));
  const panels = {
    serial: serialBody,
    parallel: el("div", null, parBox, el("button", { class: "btn small ghost", onClick: () => addParRow() }, "+ Zweig")),
    conditional: condPanel,
    loop: loopPanel,
  };
  const slot = el("div", null, panels.serial);
  const tabs = branchStart
    ? el("div", { class: "tabs" }, tabBtn("Seriell", "serial", true))
    : el("div", { class: "tabs" },
        tabBtn("Seriell", "serial", true), tabBtn("Parallel (UND)", "parallel"), tabBtn("Bedingt (XOR)", "conditional"), tabBtn("Schleife", "loop"));
  function tabBtn(label, key, isActive) {
    return el("button", { class: isActive ? "active" : "", onClick: (e) => {
      active = key;
      [...tabs.children].forEach((c) => c.classList.remove("active"));
      e.target.classList.add("active");
      clear(slot); slot.appendChild(panels[key]);
    } }, label);
  }
  // Keine Variante ist mehr gesperrt: Entscheidung und Schleife legen ein
  // fehlendes Merkmal selbst an (``discriminatorPicker``), statt den Nutzer
  // aus dem Dialog in die Datensicht zu schicken.
  const body = el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" }, branchStart
      ? `Einf\u00FCgen am Anfang des Zweigs: ${branchCaption(state.schema, afterNodeId, branchStart)}`
      : `Einf\u00FCgen nach: ${nodeCaption(node)}`),
    tabs, slot);

  openModal("Schritt einf\u00FCgen", body, async () => {
    try {
      if (active === "serial") {
        const label = byId("ins-label").value.trim();
        if (!label) return false;
        const req = { label, after_node_id: afterNodeId };
        if (branchStart) req.before_node_id = branchStart;
        await api.post(`/schemas/${state.schemaId}/serial-insert`, req);
      } else if (active === "parallel") {
        const labels = [...parBox.querySelectorAll(".par-branch")].map((i) => i.value.trim()).filter(Boolean);
        if (labels.length < 2) { toast("err", "Mindestens zwei Zweige n\u00F6tig"); return false; }
        await api.post(`/schemas/${state.schemaId}/parallel-insert`, { branch_labels: labels, after_node_id: afterNodeId });
      } else if (active === "loop") {
        const label = byId("loop-label") ? byId("loop-label").value.trim() : "";
        if (!label) { toast("err", "Bezeichnung des Wiederhol-Schritts fehlt"); return false; }
        const kind = loopKind();
        const payload = { label, after_node_id: afterNodeId };
        if (kind === "BOOLEAN") {
          payload.repeat_value = loopRepeat.value === "true";
        } else if (kind === "THRESHOLD") {
          // Zwei Zellen kacheln die Zahlengerade: [-\u221E, G) und [G, +\u221E); die
          // Vergleichswahl bestimmt, welche Seite wiederholt (K6b: je eine
          // Wiederhol- und eine Verlassen-Zelle).
          const bound = loopBound.value.trim();
          if (loopBound.validity && loopBound.validity.badInput) { toast("err", "Grenze ist keine Zahl"); return false; }
          if (bound === "") { toast("err", "Grenze fehlt"); return false; }
          const g = Number(bound);
          payload.cells = loopCmp.value === "gte"
            ? [{ repeat: false, upper: g }, { repeat: true }]
            : [{ repeat: true, upper: g }, { repeat: false }];
        } else {
          const values = loopValues.value.split(",").map((v) => v.trim()).filter(Boolean);
          if (!values.length) { toast("err", "Mindestens ein Wiederhol-Wert n\u00F6tig"); return false; }
          payload.cells = [{ repeat: true, values }, { repeat: false, is_else: true }];
        }
        if (loopMax.value.trim() !== "") {
          const m = Number(loopMax.value);
          if (!Number.isInteger(m) || m < 2) { toast("err", "H\u00F6chstzahl Durchl\u00E4ufe: mindestens 2"); return false; }
          payload.max_iterations = m;
        }
        const sid = state.schemaId;
        await withDiscriminator(sid, loopPick.choice(),
          (disc) => api.post(`/schemas/${sid}/loop-insert`, { ...payload, discriminator: disc }));
      } else {
        const kind = discKind();
        if (!kind) { toast("err", "Kein Entscheidungs-Datenelement gew\u00E4hlt", ["Bitte oben ein Datenelement ausw\u00E4hlen, nach dem verzweigt wird."]); return false; }
        let branches = [];
        if (kind === "THRESHOLD") {
          // Ein Zahlenfeld liefert fuer „abc“ einen leeren Wert -- das galt
          // bisher still als „ohne Obergrenze“ und endete in einer
          // irrefuehrenden Meldung. ``badInput`` verraet die Eingabe.
          const bad = [...condRows.querySelectorAll(".threshold-row")].filter((r) =>
            r.querySelector(".cond-upper").validity && r.querySelector(".cond-upper").validity.badInput);
          bad.forEach((r) => markField(r, "keine Zahl"));
          if (bad.length) { toast("err", "Grenzwert ist keine Zahl", ["Bitte eine Zahl eingeben oder das Feld für die oberste Stufe leer lassen."]); return false; }
          const rows = [...condRows.querySelectorAll(".threshold-row")].map((r) => ({
            label: r.querySelector(".cond-label").value.trim(),
            upperRaw: r.querySelector(".cond-upper").value.trim(),
          })).filter((b) => b.label);
          if (rows.length < 2) { toast("err", "Mindestens zwei Stufen n\u00F6tig"); return false; }
          const unbounded = rows.filter((b) => b.upperRaw === "");
          if (unbounded.length !== 1) { toast("err", "Genau eine Stufe muss ohne Obergrenze (bis +\u221E) sein"); return false; }
          const bounded = rows.filter((b) => b.upperRaw !== "")
            .map((b) => ({ label: b.label, upper: Number(b.upperRaw) }))
            .sort((a, b) => a.upper - b.upper);
          branches = [...bounded, { label: unbounded[0].label }];
        } else if (kind === "BOOLEAN") {
          branches = [...condRows.querySelectorAll(".cond-label")]
            .map((i) => ({ label: i.value.trim(), bool_value: i.dataset.bool === "true" }))
            .filter((b) => b.label);
          if (branches.length !== 2) { toast("err", "Beide F\u00E4lle (wahr/falsch) ben\u00F6tigen eine Bezeichnung"); return false; }
        } else {
          branches = [...condRows.children].map((r) => {
            const labelEl = r.querySelector(".cond-label");
            const valuesEl = r.querySelector(".cond-values");
            if (!labelEl || !labelEl.value.trim()) return null;
            if (labelEl.dataset.else) return { label: labelEl.value.trim(), is_else: true };
            const values = (valuesEl ? valuesEl.value : "").split(",").map((v) => v.trim()).filter(Boolean);
            if (!values.length) return null;
            return { label: labelEl.value.trim(), values };
          }).filter(Boolean);
          if (branches.length < 2) { toast("err", "Mindestens ein Wertzweig plus Sonst-Zweig n\u00F6tig"); return false; }
        }
        await insertConditionalWithDiscriminator(afterNodeId, branches, {
          ...condPick.choice(),
          bindAt: writerStep && bindBox.checked ? writerStep.id : null,
        });
      }
      await refreshSchema();
      render();
      toast("ok", "Schritt eingef\u00FCgt");
    } catch (err) { toastError(err); return false; }
  }, "Einf\u00FCgen");
}

/**
 * Fuegt eine Entscheidung ein und legt ihr Merkmal bei Bedarf gleich mit an
 *.
 *
 * Reihenfolge: (1) neues Datenelement anlegen, (2) Schreibbindung am Schritt
 * davor, (3) ``conditional-insert``. Jeder Schritt ist eine eigene
 * Kern-Operation mit eigener Pruefung -- keine Abkuerzung. Scheitert (2) oder
 * (3), werden die hier angelegten Teile wieder entfernt, damit kein halbes
 * Merkmal im Modell zurueckbleibt; die Fehlermeldung des Kerns wird
 * weitergereicht.
 *
 * @param {string} afterNodeId Einfuegestelle
 * @param {object[]} branches Zweige fuer ``conditional-insert``
 * @param {{existing: (string|null), newName: string, newType: string, bindAt: (string|null)}} opt
 */
async function insertConditionalWithDiscriminator(afterNodeId, branches, opt) {
  const sid = state.schemaId;
  if (!opt.existing && opt.newName && !opt.bindAt) {
    throw { detail: "Ein neues Merkmal braucht einen Schritt davor, der es setzt." };
  }
  // (1) Anlegen und dessen Aufraeumen uebernimmt ``withDiscriminator``.
  await withDiscriminator(sid, opt, async (disc) => {
    let bound = false;
    try {
      if (opt.bindAt) {
        const already = (state.schema.data_accesses || []).some((a) => a.node_id === opt.bindAt
          && a.element_id === disc && (a.mode === "WRITE" || a.mode === "READ_WRITE"));
        if (!already) {
          await createDataAccess(sid, opt.bindAt, disc, "WRITE", true);  // (2)
          bound = true;
        }
      }
      await api.post(`/schemas/${sid}/conditional-insert`, { after_node_id: afterNodeId, discriminator: disc, branches });  // (3)
    } catch (err) {
      if (bound) { try { await api.del(`/schemas/${sid}/data-access/${opt.bindAt}/${disc}?mode=WRITE`); } catch (e) { /* egal */ } }
      throw err;
    }
  });
}

/**
 * Die Schritte, denen fuer die Freigabe noch etwas fehlt (Stufe B, Regel B2).
 *
 * Kommt aus dem Kern (`GET /schemas/{id}/validation` -> `release_findings`); der
 * Client entscheidet nichts selbst, er zeigt nur an. Leeres Array = bereit.
 *
 * @returns {Array<object>} Die Stufe-B-Befunde der aktuellen Validierung.
 */
function releaseFindings() {
  const v = state.validation;
  return (v && v.release_findings) || [];
}

/**
 * Gibt das Schema frei -- sofern es freigabereif ist.
 *
 * Der Kern lehnt eine Freigabe mit Schritten ohne Bearbeiter ab (Stufe B,
 * Regel B2): ein solcher Schritt wird zur Laufzeit zwar aktiviert, taucht aber
 * in **keiner** Arbeitsliste auf (`open_tasks` ueberspringt ihn) -- der Vorgang
 * saehe gestartet aus und stuende still, und das faellt nicht dem Modellierer
 * auf, sondern spaeter dem Sachbearbeiter.
 *
 * Die Pruefung hier ist **kein zweiter Entscheider**, sondern nimmt dem Nutzer
 * den vergeblichen Weg ab: Sie nennt die betroffenen Schritte beim Namen, statt
 * ihn in ein 422 laufen zu lassen, dessen Befunde er selbst zuordnen muesste.
 * Entschieden wird ausschliesslich im Kern.
 */
async function releaseSchema() {
  const missing = releaseFindings();
  if (missing.length) {
    const names = missing.map((f) => nodeLabelOf(f.node_id)).filter(Boolean);
    // Die Meldung ist bereits eine Aufzaehlung (<ul>) -- kein eigenes „•“
    // davor, sonst standen zwei Zeichen vor jedem Namen.
    toast("err", "Freigabe noch nicht möglich", [
      missing.length === 1 ? "1 Schritt braucht noch eine Bearbeiterzuordnung:"
        : `${missing.length} Schritte brauchen noch eine Bearbeiterzuordnung:`,
      ...names,
      "Sie würden sonst in keiner Arbeitsliste erscheinen.",
    ]);
    return;
  }
  // Freigeben ist nicht umkehrbar (R0): erst nachfragen.
  openModal("Schema freigeben?", el("div", { class: "form-grid" },
    el("p", null, `„${state.schema.name}“ wird freigegeben. Danach lassen sich Vorgänge starten, das Schema selbst ist aber unveränderlich.`),
    el("p", { class: "muted" }, "Änderungen gehen dann nur noch über eine neue Revision; laufende Vorgänge lassen sich auf sie migrieren.")),
  async () => {
    try {
      await api.post(`/schemas/${state.schemaId}/release`);
      await refreshSchema();
      render();
      toast("ok", "Schema freigegeben", ["Jetzt instanziierbar."]);
    } catch (err) { toastError(err); return false; }
  }, "Freigeben");
}

/**
 * Ist das Schema laut letzter Pruefung freigabereif? Nur fuer die Farbe des
 * Knopfs -- entschieden wird beim Freigeben im Kern.
 * @param {object} schema angezeigtes Schema
 * @returns {boolean}
 */
function isReleasable(schema) {
  const v = state.validation;
  if (!v || !schema || schema.id !== state.schemaId) return false;
  return v.correct !== false && !releaseFindings().length;
}

/** Bezeichnung eines Knotens der aktuellen Sicht (fuer Meldungen). */
function nodeLabelOf(nodeId) {
  const node = nodeId && state.schema && state.schema.nodes[nodeId];
  return node ? (node.label || nodeId) : nodeId;
}

async function newRevision() {
  try {
    const rev = await api.post(`/schemas/${state.schemaId}/revision`, {});
    await loadSchemas();
    await selectSchema(rev.id);
    toast("ok", "Revision erstellt", [`${rev.name} (v${rev.version})`]);
  } catch (err) { toastError(err); }
}

// --------------------------------------------------------------------------
// Migrationsassistent (laufende Instanzen auf eine neue Revision heben)
// --------------------------------------------------------------------------

/**
 * Kopfzeilen-Knopf „Laufende Instanzen migrieren (N)“.
 *
 * Erscheint nur bei einer freigegebenen Revision, fuer die der Kern laufende
 * Instanzen frueherer Versionen meldet (``state.migrationReport``, geladen in
 * ``refreshSchema``). ``modelHeader`` ist beiden Modellier-Oberflaechen
 * gemeinsam, der Knopf existiert damit in beiden.
 *
 * @param {object} schema das angezeigte Schema
 * @param {boolean} draft ob es ein Entwurf ist
 * @returns {HTMLElement|null}
 */
function migrationHeaderButton(schema, draft) {
  const report = state.migrationReport;
  if (draft || !report || report.target_schema_id !== schema.id || !report.candidates.length) return null;
  const ok = report.candidates.filter((c) => c.migratable).length;
  return el("button", {
    class: "btn small primary", "data-tour": "model.migrate",
    title: `${countLabel(report.candidates.length, "laufende Instanz", "laufende Instanzen")} früherer Versionen, ${ok} davon sofort migrierbar`,
    onClick: () => openMigrationAssistant(schema.id, null),
  }, `Laufende Instanzen migrieren (${report.candidates.length})`);
}

/**
 * Hinweis in der Instanz-Ansicht, wenn eine neuere freigegebene Version existiert.
 * @param {object} inst die angezeigte Instanz
 * @returns {Promise<HTMLElement|null>}
 */
async function instanceMigrationPanel(inst) {
  // Die Laufzeit-Sichten zeichnen im Takt neu (Revisions-Poll); die Antwort
  // wird deshalb je Instanz+Version 30 s gemerkt, statt bei jedem Neuzeichnen
  // alle Schemata serverseitig durchzusehen.
  const key = `${inst.id}|${inst.schema_id}`;
  const cache = state.migrationTargetCache || (state.migrationTargetCache = {});
  let target = cache[key] && Date.now() - cache[key].at < 30000 ? cache[key].target : undefined;
  if (target === undefined) {
    try { target = await api.get(`/instances/${inst.id}/migration-target`); } catch (e) { return null; }
    cache[key] = { at: Date.now(), target };
  }
  if (!target || !target.schema_id) return null;
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Neue Version verfügbar"),
      el("span", { class: "sub" }, `v${inst.schema_version} → v${target.version}`)),
    el("div", { class: "panel-b row" },
      el("span", { class: "muted", style: "font-size:12px;flex:1" },
        "Diese Instanz läuft auf einer älteren Version. Ob sie wechseln kann, prüft der Kern – bereits Erledigtes bleibt unverändert."),
      el("button", { class: "btn small primary", onClick: () => openMigrationAssistant(target.schema_id, [inst.id]) },
        "Auf neue Version migrieren")));
}

/** Eingabeart eines Startwert-Felds nach Datentyp. */
function startValueWidget(elem) {
  if (!elem) return "TEXT";
  if (elem.data_type === "BOOLEAN") return "CHECKBOX";
  if (isNumericType(elem.data_type)) return "NUMBER";
  return "TEXT";
}

/**
 * Der Migrationsassistent.
 *
 * Zeigt je laufender Instanz frueherer Versionen, ob sie auf ``targetId``
 * wechseln kann, und warum nicht (Klartext aus dem Meldungskatalog). Fehlen
 * Pflichtdaten, die die neue Version verlangt und kein spaeterer Schritt mehr
 * liefert (M4), fragt er Startwerte ab -- einmal fuer alle; der Kern setzt sie
 * nur dort ein, wo die Instanz den Wert noch nicht hat. „Erneut prüfen“ ist ein
 * Trockenlauf; migriert wird erst mit dem Bestaetigen, und zwar jede Instanz
 * einzeln und atomar: eine abgelehnte bleibt unveraendert, die anderen wechseln.
 *
 * Der Client entscheidet nichts -- er zeigt nur an, was der Kern meldet.
 *
 * @param {string} targetId Schema-ID der freigegebenen Zielversion
 * @param {string[]|null} onlyIds auf diese Instanzen beschraenken (null = alle)
 */
async function openMigrationAssistant(targetId, onlyIds) {
  let report, target;
  try {
    [report, target] = await Promise.all([
      api.get(`/schemas/${targetId}/migration-report`),
      api.get(`/schemas/${targetId}`),
    ]);
  } catch (err) { toastError(err); return; }
  const cands = onlyIds ? report.candidates.filter((c) => onlyIds.includes(c.instance_id)) : report.candidates;
  if (!cands.length) {
    toast("info", "Nichts zu migrieren", ["Es laufen keine Instanzen früherer Versionen."]);
    return;
  }
  // Benennende Werte statt nackter Kennungen (best effort; ohne sie steht
  // „Vorgang“ mit der Kennung darunter).
  let migTitles = {};
  try { migTitles = await api.get("/instance-titles"); } catch (_e) { migTitles = {}; }

  // Startwerte fuer fehlende Pflichtdaten (Vereinigung ueber alle Kandidaten).
  const inputs = {};
  const missing = [...new Set(cands.flatMap((c) => c.missing_data))];
  const valueBox = el("div", { class: "form-grid" });
  missing.forEach((eid) => {
    const elem = target.data_elements[eid];
    const { control, read } = maskControl(elem, startValueWidget(elem), null, undefined);
    inputs[eid] = { read, elem };
    valueBox.appendChild(el("label", { class: "field" }, `${elem ? elem.name : eid} (${elem ? typeName(elem.data_type) : "?"})`, control));
  });
  const readMapping = () => {
    const mapping = {};
    for (const [eid, { read }] of Object.entries(inputs)) {
      const v = read();
      if (v === undefined || (typeof v === "number" && Number.isNaN(v))) continue;
      mapping[eid] = v;
    }
    return mapping;
  };

  // Tabelle: Auswahl, Instanz, Version, Ergebnis der Pruefung.
  const checks = {};
  const statusCells = {};
  const rowsById = {};
  const migRows = cands.map((c) => {
    const box = el("input", { type: "checkbox" });
    box.checked = c.migratable;
    checks[c.instance_id] = box;
    statusCells[c.instance_id] = el("div", { class: "mig-status" });
    return [box, instanceNameCell(c.instance_id, c.started_at, migTitles[c.instance_id]),
      `v${c.schema_version}`, statusCells[c.instance_id]];
  });
  const migTable = table(["Auswahl", "Instanz", "Version", "Ergebnis"], migRows, null, { class: "mig-table" });
  migTable.querySelectorAll("tbody tr").forEach((tr, i) => { rowsById[cands[i].instance_id] = tr; });
  const showStatus = (iid, findings) => {
    const cell = statusCells[iid];
    clear(cell);
    if (!findings.length) { cell.appendChild(el("span", { class: "pill pill-green" }, "migrierbar")); return; }
    cell.appendChild(el("span", { class: "pill pill-amber" }, "bleibt auf ihrer Version"));
    findings.forEach((f) => cell.appendChild(el("div", { class: "mig-reason" }, findingText(f, { withHint: true }))));
  };
  cands.forEach((c) => showStatus(c.instance_id, c.findings));

  const recheck = el("button", { class: "btn small", onClick: async () => {
    try {
      const res = await api.post(`/schemas/${targetId}/migrate-instances`,
        { instance_ids: cands.map((c) => c.instance_id), data_mapping: readMapping(), execute: false });
      res.results.forEach((r) => { showStatus(r.instance_id, r.findings); checks[r.instance_id].checked = !r.findings.length; });
    } catch (err) { toastError(err); }
  } }, "Mit diesen Startwerten erneut prüfen");

  const body = el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      `Ziel: ${target.name} v${target.version}. Bereits Erledigtes bleibt, wie es ist; eine Instanz, die nicht passt, läuft sicher auf ihrer Version weiter.`),
    missing.length
      ? el("div", { class: "mig-values" },
          el("div", { style: "font-weight:600;font-size:13px;margin-bottom:6px" }, "Startwerte für neue Pflichtdaten"),
          valueBox, recheck)
      : null,
    migTable);

  openModal(`Instanzen auf v${target.version} migrieren`, body, async () => {
    const ids = cands.map((c) => c.instance_id).filter((iid) => checks[iid].checked);
    if (!ids.length) { toast("info", "Keine Instanz ausgewählt"); return false; }
    try {
      const res = await api.post(`/schemas/${targetId}/migrate-instances`,
        { instance_ids: ids, data_mapping: readMapping(), execute: true });
      const moved = res.results.filter((r) => r.migrated);
      const kept = res.results.filter((r) => !r.migrated);
      kept.forEach((r) => showStatus(r.instance_id, r.findings));
      // Bereits migrierte verschwinden aus dem offenen Dialog -- ein zweiter
      // Durchgang soll nur noch die uebrigen betreffen.
      moved.forEach((r) => {
        rowsById[r.instance_id].remove();
        const i = cands.findIndex((c) => c.instance_id === r.instance_id);
        if (i >= 0) cands.splice(i, 1);
      });
      state.migrationTargetCache = {};
      toast(kept.length ? "err" : "ok", `${countLabel(moved.length, "Instanz", "Instanzen")} migriert` + (kept.length ? `, ${kept.length} nicht` : ""),
        kept.map((r) => `${r.instance_id}: ${r.findings.map((f) => findingText(f)).join(" ")}`));
      if (state.instance && moved.some((r) => r.instance_id === state.instance.id)) await loadInstance(state.instance.id);
      if (state.schemaId) await refreshSchema();
      render();
      return kept.length ? false : undefined;   // bei Rest offen lassen, damit die Gruende lesbar bleiben
    } catch (err) { toastError(err); return false; }
  }, "Ausgewählte migrieren");
}

async function exportBpmn() {
  try {
    const xml = await api.raw(`/schemas/${state.schemaId}/bpmn`);
    const blob = new Blob([xml], { type: "application/xml" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `${state.schema.name}.bpmn` });
    document.body.appendChild(a); a.click(); a.remove();
  } catch (err) { toastError(err); }
}

