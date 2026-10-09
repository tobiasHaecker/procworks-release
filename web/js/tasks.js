// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Meine Aufgaben.
 *
 * Die Aufgabenliste der Bearbeiter ueber alle Prozesse samt Abwesenheit,
 * Vertretung und den Aktionen am Schritt (uebernehmen, zuruecklegen ...),
 * dazu die Darstellung der zeitbasierten Priorisierung (Kritikalitaetsbaender,
 * Faelligkeit, Filter der Arbeitsliste).
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// View: Meine Aufgaben (Bearbeiter-Aufgabenliste)
// --------------------------------------------------------------------------

/**
 * Anzeigename eines Agenten, unabhaengig vom oben gewaehlten Prozess.
 *
 * Erst das Organisationsmodell des gewaehlten Schemas (dort ist der Name
 * sicher der des Vorgangs), dann das modelluebergreifende Verzeichnis
 * (`state.agentDirectory`, gefuellt aus ``GET /directory/agents``), zuletzt die
 * ID. Ohne den zweiten Schritt zeigte "Meine Aufgaben" interne IDs, sobald oben
 * ein Prozess mit einer anderen Organisation gewaehlt war -- die Aufgabenliste
 * reicht ueber alle Prozesse, die Namensaufloesung darf es also auch.
 *
 * @param {string} id Agenten-ID
 * @returns {string} Name oder, wenn unbekannt, die ID
 */
function agentNameOf(id) {
  const org = state.schema && state.schema.org_model;
  const a = org && org.agents ? org.agents[id] : null;
  if (a) return a.name;
  const known = state.agentDirectory[id];
  return known ? known.name : id;
}

/**
 * Laedt das modelluebergreifende Personenverzeichnis in `state.agentDirectory`.
 *
 * Best effort: schlaegt der Abruf fehl (fehlende Leserechte, alter Server),
 * bleibt das Verzeichnis leer und die Anzeige faellt auf das bisherige
 * Verhalten zurueck. Wird vor den Sichten aufgerufen, die Personen ueber
 * Prozessgrenzen hinweg zeigen.
 *
 * @returns {Promise<void>}
 */
async function loadAgentDirectory() {
  try {
    const entries = await api.get("/directory/agents");
    const dir = {};
    entries.forEach((a) => { dir[a.agent_id] = a; });
    state.agentDirectory = dir;
  } catch (e) { /* best effort -- ohne Verzeichnis bleibt es bei den IDs */ }
}

// Track the agent whose task list is on screen plus the keys of the tasks last
// shown there, so a task that arrives while the list is open can be announced
// exactly once with a self-dismissing popup.
let taskAlert = { agentId: null, keys: new Set(), primed: false };

function taskKey(t) { return `${t.instance_id}:${t.node_id}`; }

// Announce tasks that appeared since the last render of THIS agent's list while
// the tasks view stayed open. The initial load (no baseline yet) and agent
// switches only prime the baseline, so a popup pops only for genuine arrivals.
function announceNewTasks(agentId, tasks) {
  const keys = new Set(tasks.map(taskKey));
  if (taskAlert.agentId === agentId && taskAlert.primed) {
    const fresh = tasks.filter((t) => !taskAlert.keys.has(taskKey(t)));
    if (fresh.length === 1) {
      toast("info", "Neue Aufgabe eingetroffen", [fresh[0].label || fresh[0].node_id]);
    } else if (fresh.length > 1) {
      toast("info", `${fresh.length} neue Aufgaben eingetroffen`, fresh.slice(0, 5).map((t) => t.label || t.node_id));
    }
  }
  taskAlert = { agentId, keys, primed: true };
}

async function viewTasks() {
  const content = byId("content");
  clear(content);
  // Die persoenliche Aufgabenliste haengt bewusst NICHT am oben gewaehlten
  // Prozess: sie reicht ueber alle Prozesse (/me/tasks, /agents/{id}/tasks).
  // Vorher brach sie genau daran -- war oben ein Prozess ohne eigene Agenten
  // gewaehlt, sah eine Sachbearbeiterin statt ihrer Aufgaben einen Hinweis fuer
  // Modellierer. Das Personenverzeichnis kommt
  // deshalb modelluebergreifend aus /directory/agents.
  await loadAgentDirectory();
  const agents = Object.values(state.agentDirectory).map((a) => ({ id: a.agent_id, name: a.name }));

  // A bound principal (token login) is tied to one agent: no picker, the
  // worklist comes from /me/tasks. In open dev mode we keep the agent picker.
  // A *personal* login without an agent binding (password, company account)
  // gets the picker only to look at someone's list (supervision): it may not
  // act in that person's name -- the core answers 403 since 2026-09-24 --, so
  // the rows offer just the supervision completion and the recovery actions
  // that work without an owner (see supervisionTaskActions).
  const bound = state.principal && state.principal.agent_id;
  const supervise = !bound && !mayActForOthers();
  if (!bound && !agents.length) {
    content.appendChild(emptyState(hasRole("modeler", "admin")
      ? "Keine Agenten in den Organisationsmodellen. Lege zuerst Agenten in der Ressourcensicht an."
      : "Dieser Login ist keinem Bearbeiter zugeordnet \u2013 wende dich an die Administration."));
    return;
  }
  let agentId;
  let picker;
  if (bound) {
    agentId = state.principal.agent_id;
    const who = state.principal.display_name || agentNameOf(agentId);
    picker = el("div", { class: "panel" },
      el("div", { class: "panel-h" }, el("h2", null, "Angemeldet"), el("span", { class: "sub" }, "Aufgaben f\u00FCr dich, inkl. Vertretung")),
      el("div", { class: "panel-b" }, el("div", { class: "ok-banner" }, "\u2713 Angemeldet als " + who)));
  } else {
    agentId = localStorage.getItem("agentId");
    if (!agentId || !agents.some((a) => a.id === agentId)) agentId = agents[0].id;
    const sel = el("select", null, ...agents.map((a) => el("option", { value: a.id }, a.name)));
    sel.value = agentId;
    sel.addEventListener("change", () => { localStorage.setItem("agentId", sel.value); render(); });
    picker = supervise
      ? el("div", { class: "panel" },
          el("div", { class: "panel-h" }, el("h2", null, "Aufsicht"), el("span", { class: "sub" }, "Aufgaben einer Person ansehen, inkl. Vertretung")),
          el("div", { class: "panel-b" },
            el("label", { class: "field" }, "Aufgaben ansehen von", sel),
            el("div", { class: "warn-banner", style: "margin-top:8px" },
              "Dein Login ist keinem Bearbeiter zugeordnet. Du kannst deshalb nicht im Namen dieser Person arbeiten. " +
              "Einen Schritt kannst du als Aufsichtseingriff mit Begr\u00FCndung abschlie\u00DFen; er wird unter deinem Login protokolliert.")))
      : el("div", { class: "panel" },
          el("div", { class: "panel-h" }, el("h2", null, "Bearbeiter"), el("span", { class: "sub" }, "Aufgaben f\u00FCr eine Person, inkl. Vertretung")),
          el("div", { class: "panel-b" }, el("label", { class: "field" }, "Angemeldet als", sel)));
  }
  content.appendChild(picker);

  let tasks = [];
  try {
    tasks = await api.get(bound ? "/me/tasks" : `/agents/${agentId}/tasks`);
    await ensureSchemaNames(tasks.map((t) => t.schema_id));
  }
  catch (err) { toastError(err); }

  // Announce tasks that arrived while this list was open (self-dismissing).
  announceNewTasks(agentId, tasks);

  // Z4: clientseitiger Kritikalit\u00E4ts-Filter.
  // Rein additiv \u00FCber die vorhandenen API-Felder; Wahl wird gemerkt.
  const taskFilter = normalizeTaskFilter(localStorage.getItem("taskFilter"));
  const visible = filterTasksByCriticality(tasks, taskFilter);
  const hiddenCount = tasks.length - visible.length;
  const filterSel = el("select", { class: "task-filter" },
    el("option", { value: "all" }, "Alle Aufgaben"),
    ...Object.keys(TASK_FILTERS).map((key) => el("option", { value: key }, taskFilterLabel(key))));
  filterSel.value = taskFilter;
  filterSel.addEventListener("change", () => {
    localStorage.setItem("taskFilter", filterSel.value);
    render();
  });

  const body = el("div", { class: "panel-b" });
  if (!tasks.length) {
    body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Keine offenen Aufgaben f\u00FCr " + agentNameOf(agentId) + "."));
  } else if (!visible.length) {
    body.appendChild(el("div", { class: "ok-banner" },
      `\u2713 Keine ${TASK_FILTERS[taskFilter].emptyWord} Aufgaben \u2013 ${hiddenCount} weitere unter \u201EAlle Aufgaben\u201C.`));
  } else {
    // E1 (Zustandsmaschine): Eine unübernommene Aufgabe bietet „Übernehmen“
    // an (sie verschwindet dann aus den Listen aller anderen), eine selbst
    // übernommene „Zurücklegen“. Fremd Übernommenes taucht hier gar nicht
    // erst auf – das filtert der Kern (Withdrawn-Sicht). „Erledigen“ geht
    // weiterhin auch ohne Übernahme (Ein-Klick-Fluss).
    const rows = visible.map((t) => {
      const elig = (t.eligible_agents || []).map(agentNameOf).join(", ");
      const mine = t.claimed_by === agentId;
      const status = el("div", { class: "row", style: "gap:4px" },
        mine
          ? el("span", { class: "pill pill-amber" }, "übernommen")
          : el("span", { class: "pill pill-gray" }, "angeboten"),
        // E2: Detailzustand (Pause/Scheitern) direkt am Eintrag.
        t.detail === "SUSPENDED" ? el("span", { class: "pill pill-blue" }, "angehalten") : null,
        t.detail === "FAILED"
          ? el("span", { class: "pill pill-red", title: t.detail_reason || "" }, "gescheitert")
          : null,
        // T3/E9: eine gefeuerte Eskalationsstufe ist am Eintrag ablesbar.
        t.escalated_stage > 0
          ? el("span", { class: "pill pill-red" }, `eskaliert (${t.escalated_stage})`)
          : null);
      // E2-Aktionen je Detailzustand: gescheitert → nur Wiederanlauf;
      // angehalten → Weiterarbeiten/Zurücklegen; sonst wie gehabt, für den
      // Inhaber ergänzt um Anhalten und Problem melden.
      let actions;
      if (supervise) {
        actions = supervisionTaskActions(t);
      } else if (t.detail === "FAILED") {
        actions = el("div", { class: "row", style: "gap:6px" },
          el("button", { class: "btn small", onClick: () => resetTask(t, agentId) }, "Wiederanlauf"));
      } else if (t.detail === "SUSPENDED") {
        actions = el("div", { class: "row", style: "gap:6px" },
          el("button", { class: "btn small green", onClick: () => resumeTask(t, agentId) }, "Weiterarbeiten"),
          el("button", { class: "btn small ghost", onClick: () => returnTask(t, agentId) }, "Zurücklegen"));
      } else {
        actions = el("div", { class: "row", style: "gap:6px" },
          el("button", { class: "btn small green", onClick: () => completeTask(t, agentId) }, "Erledigen"),
          mine
            ? el("button", { class: "btn small ghost", onClick: () => returnTask(t, agentId) }, "Zurücklegen")
            : el("button", { class: "btn small ghost", onClick: () => claimTask(t, agentId) }, "Übernehmen"),
          mine
            ? el("button", { class: "btn small ghost", title: "Arbeit pausieren (Frist läuft weiter)",
                onClick: () => suspendTask(t, agentId) }, "Anhalten")
            : null,
          mine
            ? el("button", { class: "btn small ghost", title: "Als gescheitert melden",
                onClick: () => failTask(t, agentId) }, "Problem")
            : null);
      }
      // Zwei gleiche Aufgaben verschiedener Vorgaenge waren nicht zu
      // unterscheiden -- darunter jetzt die benennenden Werte.
      // Ohne benennende Werte (meist beim ersten Schritt) unterscheidet die
      // Startzeit des Vorgangs.
      const taskCell = el("div", { title: `Kennung: ${t.instance_id}` }, t.label || t.node_id,
        el("div", { class: "task-context" }, instanceName(t.instance_started_at, t.context)),
        deputyNote(t, agentId));
      return [taskCell, schemaLabel(t.schema_id, t.schema_version), dueCell(t), elig, status, actions];
    });
    body.appendChild(table(["Aufgabe", "Prozess", "Fällig", "Berechtigte", "Status", ""], rows));
    if (hiddenCount > 0) {
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-top:6px" },
        `${countLabel(hiddenCount, "Aufgabe", "Aufgaben")} durch den Filter ausgeblendet.`));
    }
  }
  content.appendChild(el("div", { class: "panel", "data-tour": "tasks.list" },
    el("div", { class: "panel-h" }, el("h2", null, "Offene Aufgaben"),
      el("span", { class: "sub" },
        hiddenCount > 0 ? `${visible.length} von ${tasks.length} Eintr\u00E4gen` : countLabel(tasks.length, "Eintrag", "Eintr\u00E4ge")),
      filterSel),
    body));

  content.appendChild(await absencePanel(agentId));
}

/**
 * „in Vertretung für <Name>“ an einer Aufgabe der Arbeitsliste.
 *
 * Eine Vertretung sah die Aufgabe einer abwesenden Kollegin ohne Hinweis,
 * warum. Der Kern nennt je Vertretung die vertretene Person
 * (``OpenTask.deputy_of``); der Name kommt aus dem Personenverzeichnis, weil
 * die Liste ueber alle Prozesse reicht.
 * @param {{deputy_of?: Object<string, string>}} task Aufgabe
 * @param {string} agentId Person, deren Liste gezeigt wird
 * @returns {HTMLElement|null} Vermerk, oder ``null``, wenn die Person die
 *   Aufgabe selbst traegt
 */
function deputyNote(task, agentId) {
  const forId = task.deputy_of && task.deputy_of[agentId];
  if (!forId) return null;
  const known = state.agentDirectory && state.agentDirectory[forId];
  return el("div", { class: "task-deputy" }, `in Vertretung f\u00FCr ${known ? known.name : forId}`);
}

/**
 * Hinweis zum Vertretungs-Status im Abwesenheits-Panel.
 *
 * @param {string|null} person Name der Person, deren Liste gezeigt wird –
 *   ``null``, wenn es die eigene ist (dann „du/deine“).
 * @param {string|null} deputy Name der Vertretung, ``null`` ohne Vertretung
 * @returns {HTMLElement} gruener bzw. gelber Hinweis. Ohne Vertretung nennt er,
 *   wer sie hinterlegen kann: Modellierer und Administratoren in der
 *   Ressourcensicht; ein Bearbeiter sieht diese Sicht nicht und wird an die
 *   Administration verwiesen.
 */
function absenceDeputyBanner(person, deputy) {
  const howToSet = hasRole("modeler", "admin")
    ? "Vertretung in der Ressourcensicht setzen."
    : "Bitte die Vertretung bei der Administration hinterlegen lassen.";
  if (deputy) {
    return el("div", { class: "ok-banner" }, person
      ? `\u2713 Vertretung hinterlegt: ${deputy} erh\u00E4lt die Aufgaben von ${person} w\u00E4hrend der Abwesenheit (parallel).`
      : `\u2713 Vertretung hinterlegt: ${deputy} erh\u00E4lt deine Aufgaben w\u00E4hrend deiner Abwesenheit (parallel zu dir).`);
  }
  return el("div", { class: "warn-banner" }, person
    ? `\u26A0 F\u00FCr ${person} ist keine Vertretung hinterlegt. W\u00E4hrend der Abwesenheit bleiben die Aufgaben bei ${person} \u2013 niemand \u00FCbernimmt sie; das Monitoring zeigt solche Vorg\u00E4nge unter \u201ENur Abwesende zust\u00E4ndig\u201C. ${howToSet}`
    : `\u26A0 Keine Vertretung hinterlegt. W\u00E4hrend deiner Abwesenheit bleiben deine Aufgaben dir zugewiesen \u2013 die Instanz steht nicht still, aber niemand \u00FCbernimmt f\u00FCr dich. ${howToSet}`);
}

// Panel \u201EAbwesenheit / Vertretung" in \u201EMeine Aufgaben": eine gut sichtbare
// Selbstbedienung, um fuer einen Zeitraum abwesend zu sein. Waehrend der
// Abwesenheit erhaelt der eingetragene Vertreter die Aufgaben PARALLEL zum
// Agenten (der Agent wird nie entfernt -- ohne Vertretung bleibt die Aufgabe
// also beim Agenten und die Instanz steht nie still). Die eigentliche Aufloesung
// passiert im Kern; dieser Client ruft nur die Endpunkte /agents/{id}/absences.
async function absencePanel(agentId) {
  // Wie die Aufgabenliste selbst: die Person wird modelluebergreifend gesucht
  // (Verzeichnis), nicht im gerade gewaehlten Schema -- sonst haette ein
  // Bearbeiter je nach Prozessauswahl "keine Vertretung hinterlegt" gelesen,
  // obwohl eine eingetragen ist.
  const org = (state.schema && state.schema.org_model) || { agents: {} };
  const agent = (org.agents || {})[agentId] || state.agentDirectory[agentId] || {};
  const deputyId = agent.deputy_id || null;

  // Vertretungs-Status: ohne Vertreter ein deutlicher Hinweis, dass die
  // Aufgaben in der Abwesenheit beim Agenten selbst verbleiben. Sieht eine
  // Aufsicht die Liste einer anderen Person, nennt der Text diese Person.
  const own = !!(state.principal && state.principal.agent_id === agentId);
  const deputyLine = absenceDeputyBanner(own ? null : (agent.name || agentNameOf(agentId)),
    deputyId ? agentNameOf(deputyId) : null);

  // Eingabezeile: Zeitraum (von/bis, ganze Tage) plus optionale Notiz.
  const fromInp = el("input", { type: "date" });
  const toInp = el("input", { type: "date" });
  const noteInp = el("input", { type: "text", placeholder: "Notiz (optional), z. B. Urlaub" });
  const addBtn = el("button", { class: "btn small primary", onClick: submit }, "Abwesenheit eintragen");

  async function submit() {
    if (!fromInp.value || !toInp.value) { toast("err", "Bitte Start- und Enddatum w\u00E4hlen"); return; }
    if (toInp.value < fromInp.value) { toast("err", "Das Enddatum darf nicht vor dem Startdatum liegen"); return; }
    try {
      // Ganze Tage: Beginn 00:00, Ende 23:59:59 UTC (inklusiver Zeitraum).
      await api.post(`/agents/${agentId}/absences`, {
        start_at: `${fromInp.value}T00:00:00Z`,
        end_at: `${toInp.value}T23:59:59Z`,
        note: noteInp.value || "",
      });
      toast("ok", "Abwesenheit eingetragen");
      render();
    } catch (err) { toastError(err); }
  }

  let absences = [];
  try { absences = await api.get(`/agents/${agentId}/absences`); }
  catch (err) { toastError(err); }

  const now = new Date();
  const fmt = fmtDate;
  const listBody = el("div", { class: "panel-b" });
  if (!absences.length) {
    listBody.appendChild(el("div", { class: "muted", style: "font-size:13px" }, "Keine Abwesenheiten eingetragen."));
  } else {
    const rows = absences.map((a) => {
      const active = new Date(a.start_at) <= now && now <= new Date(a.end_at);
      const status = active ? el("span", { class: "pill pill-amber" }, "aktiv") : el("span", { class: "pill pill-gray" }, "geplant");
      const del = el("button", { class: "btn small danger", onClick: async () => {
        try { await api.del(`/agents/${agentId}/absences/${a.id}`); toast("ok", "Abwesenheit entfernt"); render(); }
        catch (err) { toastError(err); }
      } }, "Entfernen");
      return [`${fmt(a.start_at)} \u2013 ${fmt(a.end_at)}`, a.note || "\u2013", status, del];
    });
    listBody.appendChild(table(["Zeitraum", "Notiz", "Status", ""], rows));
  }

  return el("div", { class: "panel", "data-tour": "tasks.absence" },
    el("div", { class: "panel-h" },
      el("h2", null, "Abwesenheit / Vertretung"),
      el("span", { class: "sub" }, "Vertreter erh\u00E4lt Aufgaben w\u00E4hrend der Abwesenheit")),
    el("div", { class: "panel-b" },
      deputyLine,
      el("div", { class: "form-row", style: "display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin-top:10px" },
        el("label", { class: "field" }, "Von", fromInp),
        el("label", { class: "field" }, "Bis", toInp),
        el("label", { class: "field", style: "flex:1;min-width:180px" }, "Notiz", noteInp),
        addBtn)),
    listBody);
}

// Darf dieser Login im Namen einer anderen Person handeln (agent_id nennen)?
// Spiegelt nur die Regel des Kerns (api._may_act_for_others), damit die
// Oberfläche keine Knöpfe anbietet, die mit 403 enden -- entschieden wird im
// Kern. Ja nur im offenen Entwicklungsmodus, im Token-Modus (Integrationen)
// und für Integrations-Identitäten. Persönliche Logins (Passwort,
// Firmenkonto) handeln nur als sie selbst.
function mayActForOthers() {
  if (state.authMode === "open" || state.authMode === "token") return true;
  const roles = (state.principal && state.principal.roles) || [];
  return roles.includes("integration");
}

// Aktionen einer Aufgabe in der Aufsichtssicht von „Meine Aufgaben" (Login ohne
// Bearbeiterzuordnung). Keine davon nennt eine Person: Abschließen läuft als
// Aufsichtseingriff (der Kern verlangt die Begründung, promptComplete fragt
// danach), Zurücklegen und Wiederanlauf einer fremd übernommenen Aufgabe sind
// die Eingriffe, die der Kern der Aufsicht ohnehin erlaubt.
//   t: Eintrag der Aufgabenliste (OpenTask) -- Rückgabe: Knopfzeile (Element).
function supervisionTaskActions(t) {
  if (t.detail === "FAILED") {
    return el("div", { class: "row", style: "gap:6px" },
      el("button", { class: "btn small", onClick: () => resetTask(t, null) }, "Wiederanlauf"));
  }
  if (t.detail === "SUSPENDED") {
    return el("div", { class: "row", style: "gap:6px" },
      el("button", { class: "btn small ghost", onClick: () => returnTask(t, null) }, "Zurücklegen"));
  }
  return el("div", { class: "row", style: "gap:6px" },
    el("button", { class: "btn small",
      title: "Der Abschluss wird als Aufsichtseingriff mit Begründung unter deinem Login protokolliert",
      onClick: () => completeTask(t, null) }, "Als Aufsicht abschließen"),
    t.claimed_by
      ? el("button", { class: "btn small ghost", onClick: () => returnTask(t, null) }, "Zurücklegen")
      : null);
}

/**
 * Setzt eine Arbeitslisten-Aktion (E1/E2) fuer einen offenen Schritt ab.
 *
 * Das gemeinsame Geruest von Uebernehmen, Zuruecklegen, Anhalten,
 * Weiterarbeiten, Problem melden und Wiederanlauf: ``POST
 * /instances/{id}{action}`` mit ``node_id`` und ``agent_id``, danach
 * Erfolgsmeldung und Neuzeichnen. Ob die Aktion erlaubt ist (Exklusivitaet W1,
 * Berechtigung W2, Detailzustands-Automat), entscheidet allein der Kern; eine Ablehnung
 * (409/403) erscheint nur als Meldung.
 *
 * @param {object} task Offene Aufgabe mit ``instance_id`` und ``node_id``.
 * @param {string} agentId Handelnde Person (``agent_id`` der Anfrage).
 * @param {string} action Pfadendung samt Schraegstrich, z. B. ``"/claim"``.
 * @param {string} okTitle Titel der Erfolgsmeldung.
 * @param {string[]} [okLines] Optionale Detailzeilen der Erfolgsmeldung.
 * @param {object} [extra] Weitere Felder des Anfragekoerpers (etwa ``reason``).
 * @returns {Promise<boolean>} ``true`` nach Erfolg, ``false`` nach einem
 *   Fehler -- passend fuer den Bestaetigen-Rueckruf von ``openModal``.
 */
async function postTaskAction(task, agentId, action, okTitle, okLines, extra) {
  try {
    await api.post(`/instances/${task.instance_id}${action}`,
      { node_id: task.node_id, agent_id: agentId, ...extra });
    toast("ok", okTitle, okLines);
    render();
    return true;
  } catch (err) {
    toastError(err);
    return false;
  }
}

// E1 (Zustandsmaschine): Aufgabe übernehmen bzw. zurücklegen. Geteilte
// Funktionen für jede Aufrufstelle (Aufgabenliste heute, künftige Sichten) –
// der Kern erzwingt Exklusivität (W1) und Berechtigung (W2) und antwortet bei
// Konflikten mit 409, das hier nur angezeigt wird (keine Client-Logik).
async function claimTask(task, agentId) {
  await postTaskAction(task, agentId, "/claim", "Aufgabe übernommen",
    ["Sie verschwindet aus den Listen der anderen, bis sie erledigt oder zurückgelegt ist."]);
}

async function returnTask(task, agentId) {
  await postTaskAction(task, agentId, "/return", "Aufgabe zurückgelegt",
    ["Alle Berechtigten sehen sie wieder in ihrer Liste."]);
}

// --- Detailzustände (E2): Anhalten / Weiterarbeiten / Problem / Wiederanlauf.
// Geteilte Funktionen; die Regeln (nur Inhaber, gescheitert wartet auf
// Wiederanlauf, Detailzustands-Automat) erzwingt der Kern mit 409 – hier nur Anzeige.
async function suspendTask(task, agentId) {
  await postTaskAction(task, agentId, "/suspend", "Aufgabe angehalten",
    ["Die Frist läuft weiter – Anhalten ist Transparenz, kein Fristen-Stopp."]);
}

async function resumeTask(task, agentId) {
  await postTaskAction(task, agentId, "/resume", "Weiter geht's");
}

function failTask(task, agentId) {
  const reason = el("input", { type: "text", placeholder: "z. B. Unterlagen unvollständig" });
  openModal(`Problem melden – ${task.label || task.node_id}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Der Schritt wird als gescheitert markiert und wartet auf den Wiederanlauf – erst dann wird er wieder allen Berechtigten angeboten (mit frischer Frist). Der Vorgang bleibt dabei in einem definierten Zustand."),
      el("label", { class: "field" }, "Begründung", reason)),
    async () => {
      if (!reason.value.trim()) { toast("err", "Bitte eine Begründung angeben"); return false; }
      return postTaskAction(task, agentId, "/fail", "Problem gemeldet", undefined,
        { reason: reason.value.trim() });
    }, "Melden");
}

async function resetTask(task, agentId) {
  await postTaskAction(task, agentId, "/reset", "Wiederanlauf",
    ["Die Aufgabe wird wieder allen Berechtigten angeboten – mit frischer Frist."]);
}

async function completeTask(task, agentId) {
  // Die Instanz wird ohnehin geladen (fuer ein moegliches Ad-hoc-Schema); ihre
  // Datenwerte belegen zugleich die Maske vor -- siehe promptComplete.
  let schema;
  let inst;
  try {
    inst = await api.get(`/instances/${task.instance_id}`);
    schema = inst.ad_hoc_schema || await api.get(`/schemas/${task.schema_id}`);
  } catch (err) { toastError(err); return; }
  await promptComplete(schema, task.instance_id, task.node_id, task.label || task.node_id, agentId,
    async () => { render(); }, inst.data_values);
}


// Kurzform der abgeleiteten Arbeitslisten-Priorität (E8) für die Aufgabenkachel.
const PRIORITY_LABELS = { LOW: "niedrig", MEDIUM: "normal", HIGH: "hoch", CRITICAL: "kritisch" };
function priorityShort(p) { return PRIORITY_LABELS[p] || "bereit"; }

// --- Zeitbasierte, automatische Arbeitslisten-Priorisierung ----------------
// Rein darstellend: der Server liefert je Aufgabe (OpenTask) das abgeleitete
// Kritikalitätsband und die Restzeit; hier werden sie nur zu Text/Badge. Keine
// Korrektheitslogik im Client.

//: Anwenderverständliche Beschriftung je Kritikalitätsband.
const CRITICALITY_LABELS = {
  ON_TRACK: "im Plan", WARNING: "wird knapp", AT_RISK: "gefährdet", OVERDUE: "überfällig", NONE: "",
};
//: CSS-Klasse (Ampelfarbe) je Band; NONE bleibt ohne Badge.
const CRITICALITY_CLASS = {
  ON_TRACK: "crit-ontrack", WARNING: "crit-warning", AT_RISK: "crit-atrisk", OVERDUE: "crit-overdue",
};

//: Kritikalitäts-Filter der Arbeitsliste (Z4): Schlüssel -> gezeigte Bänder
//: und Wortformen. Einzige Quelle für Filterung, Auswahlliste und Leer-Hinweis,
//: damit kein Text andere Bänder nennt, als tatsächlich gezeigt werden. Die
//: Bandnamen in Klammern kommen aus CRITICALITY_LABELS. "all" fehlt bewusst.
//: "Kritisch" heißt gefährdet + überfällig; "wird knapp" ist eine Vorwarnung.
const TASK_FILTERS = {
  critical: { bands: ["AT_RISK", "OVERDUE"], word: "kritische", emptyWord: "kritischen" },
  overdue: { bands: ["OVERDUE"], word: "\u00FCberf\u00E4llige", emptyWord: "\u00FCberf\u00E4lligen" },
};

/**
 * Bringt einen gespeicherten Filterschlüssel auf einen gültigen Wert.
 *
 * @param {string|null} key  Wert aus localStorage (kann fehlen, veraltet oder
 *   beliebig sein).
 * @returns {string} den Schlüssel, wenn TASK_FILTERS ihn als eigene
 *   Eigenschaft kennt, sonst "all". Eigene Eigenschaft statt `in`/Index:
 *   Ein Wert wie "constructor" träfe sonst die Prototyp-Kette und ließe die
 *   Arbeitsliste abstürzen. Bewusst hasOwnProperty.call statt Object.hasOwn:
 *   Letzteres fehlt in älteren Safari-Versionen und bräche dort jeden Aufruf.
 */
function normalizeTaskFilter(key) {
  return typeof key === "string" && Object.prototype.hasOwnProperty.call(TASK_FILTERS, key) ? key : "all";
}

/**
 * Filtert eine Aufgabenliste nach dem gewählten Kritikalitäts-Filter.
 *
 * @param {Array<{time_criticality?: string}>} tasks  offene Aufgaben (OpenTask)
 *   vom Server; das Band leitet der Kern ab, hier wird nur ausgewählt.
 * @param {string} key  Filterschlüssel; "all" und jeder unbekannte Schlüssel
 *   zeigen alle Aufgaben, statt die Liste stillschweigend zu leeren.
 * @returns {Array} die sichtbaren Aufgaben in unveränderter Reihenfolge.
 *   Aufgaben ohne Band (fehlendes Feld, "NONE") fallen bei jedem echten
 *   Filter heraus.
 */
function filterTasksByCriticality(tasks, key) {
  const norm = normalizeTaskFilter(key);
  if (norm === "all") return tasks;
  const bands = TASK_FILTERS[norm].bands;
  return tasks.filter((t) => bands.includes(t && t.time_criticality));
}

/**
 * Beschriftung eines Kritikalitäts-Filters in der Auswahlliste.
 *
 * @param {string} key  Filterschlüssel.
 * @returns {string} "Nur <Wort>" und bei mehreren Bändern deren Anzeigenamen
 *   in Klammern, z. B. "Nur kritische (gefährdet + überfällig)" bzw.
 *   "Nur überfällige"; ein unbekannter Schlüssel ergibt "Alle Aufgaben".
 */
function taskFilterLabel(key) {
  const norm = normalizeTaskFilter(key);
  if (norm === "all") return "Alle Aufgaben";
  const f = TASK_FILTERS[norm];
  const names = f.bands.length > 1 ? " (" + f.bands.map((b) => CRITICALITY_LABELS[b]).join(" + ") + ")" : "";
  return "Nur " + f.word + names;
}

// Ein farbiges Kritikalitäts-Badge für eine Aufgabe, oder null ohne Soll-Zeit.
function criticalityBadge(t) {
  const band = (t && t.time_criticality) || "NONE";
  if (band === "NONE" || !CRITICALITY_LABELS[band]) return null;
  return el("span", { class: "badge " + (CRITICALITY_CLASS[band] || "") }, CRITICALITY_LABELS[band]);
}

// Menschliche Dauer (Sekunden -> "12 Min." / "2 Std. 5 Min." / "3 Tg.").
function humanDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + " Sek.";
  const min = Math.round(sec / 60);
  if (min < 60) return min + " Min.";
  const h = Math.floor(min / 60), m = min % 60;
  if (h < 24) return m ? (h + " Std. " + m + " Min.") : (h + " Std.");
  return Math.floor(h / 24) + " Tg.";
}

// Relative Fälligkeit einer Aufgabe ("in 12 Min." / "überfällig seit 3 Min.").
// "–" (Gedankenstrich), wenn die Aufgabe keine Soll-Zeit trägt.
function dueLabel(t) {
  if (!t || t.remaining_seconds == null) return "–";
  const s = Math.round(t.remaining_seconds);
  return s < 0 ? ("überfällig seit " + humanDuration(-s)) : ("in " + humanDuration(s));
}

// Die "Fällig"-Zelle: relative Restzeit plus farbiges Band-Badge und – bei
// vorhandener Soll-Zeit – der ρ-Verbrauchsbalken (Z4):
// wie viel der Soll-Zeit ist verbraucht, Farbe = Band. Rein
// abgeleitet aus den API-Feldern, kein eigener Zustand.
function dueCell(t) {
  const badge = criticalityBadge(t);
  const label = el("span", { class: "due-label" }, dueLabel(t));
  const cell = badge ? el("span", { class: "due-cell" }, label, badge) : label;
  if (t && t.target_seconds > 0 && t.elapsed_seconds != null) {
    const ratio = Math.max(0, Math.min(1, t.elapsed_seconds / t.target_seconds));
    const band = (t.time_criticality || "NONE").toLowerCase();
    return el("span", { class: "due-wrap" }, cell,
      el("span", { class: "rho-bar", title: Math.round(ratio * 100) + "% der Soll-Zeit verbraucht" },
        el("span", { class: "rho-fill rho-" + band, style: `width:${Math.round(ratio * 100)}%` })));
  }
  return cell;
}
