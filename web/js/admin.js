// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Administration.
 *
 * Die Administrationssicht (nur Rolle Administrator): Sicherungen,
 * Mail-Ausgang, Zuruecksetzen, Benutzer und die Texte des Audit-Protokolls.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// View: Administration (nur Rolle Administrator)
// --------------------------------------------------------------------------

// Buendelt alle rein administrativen Betriebstaetigkeiten in einer eigenen,
// nur fuer Administratoren sichtbaren Sicht (die Nav-Sichtbarkeit steuert
// VIEW_ROLES.admin + applyRoleNav). Bisher lagen diese Panels am unteren Ende
// des Monitorings; sie sind hier zentral zusammengefasst:
//   - Datensicherung: Zustand der automatischen Sicherung ansehen und bei
//     Bedarf sofort eine Sicherung anstossen (GET/POST /admin/backups[/run-now]).
//   - Wartung: gesamtes System auf Null zuruecksetzen bzw. Beispieldaten laden
//     (POST /admin/reset) -- destruktiv, daher mit Bestaetigung.
// Die Sicht ist rein an der Betriebsschicht orientiert und traegt keine
// Korrektheitslogik -- jede Aktion laeuft ueber die bestehenden Kern-Endpunkte.
async function viewAdmin() {
  const content = byId("content");
  clear(content);

  // Doppelte Absicherung: Non-Admins sehen die Nav-Kachel gar nicht erst
  // (applyRoleNav), aber falls die Sicht doch aktiv wird (z. B. persistierte
  // View nach Rollenwechsel), zeigen wir nur einen Hinweis statt Aktionen.
  if (!hasRole("admin")) {
    content.appendChild(el("div", { class: "panel" },
      el("div", { class: "panel-b" },
        emptyState("Dieser Bereich ist Administratoren vorbehalten."))));
    return;
  }

  // Benutzer (nur Passwort-Modus): die Logins verwalten -- auflisten,
  // Passwort zuruecksetzen, loeschen. Rollen aendert die Oberflaeche bewusst
  // nicht; neue Logins entstehen an der Person in der Ressourcensicht.
  if (state.passwordLogin) {
    const usersBody = el("div", { class: "panel-b" });
    content.appendChild(el("div", { class: "panel", "data-panel": "admin.users" },
      el("div", { class: "panel-h" },
        el("h2", null, "Benutzer"),
        el("span", { class: "sub" }, "Logins \u00B7 Passwort zur\u00FCcksetzen \u00B7 l\u00F6schen")),
      usersBody));
    loadUsersPanel(usersBody);
  }

  // Sicherungen (nur Ansicht): zeigt den Zustand der automatischen
  // Datensicherung und erlaubt, sofort eine Sicherung anzustossen. Die
  // Oberflaeche fuehrt selbst KEIN pg_dump aus -- sie setzt nur einen
  // Ausloese-Marker; der Sicherungsdienst fuehrt sie kurz darauf aus.
  const backupsBody = el("div", { class: "panel-b" });
  content.appendChild(el("div", { class: "panel", "data-tour": "admin.backups" },
    el("div", { class: "panel-h" },
      el("h2", null, "Sicherungen"),
      el("span", { class: "sub" }, "Datensicherung \u00B7 Zustand und Sofort-Sicherung")),
    backupsBody));
  loadBackupsPanel(backupsBody);

  // E-Mail-Ausgang (nur Ansicht): der Zustand der durablen Mail-Outbox der
  // modellierten Benachrichtigungen (Regelgruppe N) -- was ausstehend, in
  // Wiederholung, zugestellt oder als Dead-Letter gescheitert ist. Ein manueller
  // Versand stoesst faellige Eintraege erneut an (POST /admin/mail-outbox/dispatch).
  const mailBody = el("div", { class: "panel-b" });
  content.appendChild(el("div", { class: "panel", "data-tour": "admin.mail" },
    el("div", { class: "panel-h" },
      el("h2", null, "E-Mail-Ausgang"),
      el("span", { class: "sub" }, "Benachrichtigungen · Zustand und erneuter Versand")),
    mailBody));
  loadMailOutboxPanel(mailBody);

  // Wartung: destruktiv, daher unter den Sicherungen. Nur Administratoren
  // duerfen zuruecksetzen oder Beispieldaten laden (POST /admin/reset).
  content.appendChild(el("div", { class: "panel", "data-tour": "admin.maintenance" },
    el("div", { class: "panel-h" },
      el("h2", null, "Wartung"),
      el("span", { class: "sub" }, "Daten zur\u00FCcksetzen \u00B7 Beispiel laden")),
    el("div", { class: "panel-b" },
      el("p", { class: "muted" },
        "Setzt das gesamte System zur\u00FCck. Die Beispieldaten zeigen alle Funktionen anhand zweier Prozesse, einer Organisation und drei Vorg\u00E4ngen (zwei laufend, einer abgeschlossen). Das Order-to-Cash-Beispiel ist der gro\u00DFe Datensatz: sechs Prozesse (Haupt-, Teil- und Folgeprozesse) vom Angebot bis zum Mahnwesen, mit neun Auftragsvorg\u00E4ngen an unterschiedlichen Stellen (samt Teil- und Folgeprozessen 21 Vorg\u00E4nge). Dieser Vorgang l\u00F6scht alle vorhandenen Daten unwiderruflich."),
      el("div", { style: "display:flex; gap:10px; margin-top:12px; flex-wrap:wrap;" },
        el("button", { class: "btn primary", onClick: () => confirmReset("demo") }, "Beispieldaten laden"),
        el("button", { class: "btn", onClick: () => confirmReset("o2c") }, "Order-to-Cash-Beispiel laden"),
        el("button", { class: "btn danger", onClick: () => confirmReset("wipe") }, "Auf Null zur\u00FCcksetzen")))));
}

// Groessenangabe menschenlesbar (Bytes -> KB/MB/GB ...).
function fmtBytes(n) {
  if (n == null) return "\u2013";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, x = n;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  return fmtNumber(x, i ? 1 : 0) + " " + units[i];
}

// Fuellt den Sicherungen-Bereich asynchron (der Monitoring-Render bleibt sync).
async function loadBackupsPanel(body) {
  clear(body);
  body.appendChild(el("p", { class: "muted" }, "Wird geladen \u2026"));
  try {
    const status = await api.get("/admin/backups");
    clear(body);
    body.appendChild(renderBackupsBody(status, body));
  } catch (err) {
    clear(body);
    const d = describeError(err);
    body.appendChild(el("p", { class: "muted" }, d.title));
  }
}

// Baut den Inhalt des Sicherungen-Panels aus dem Statusobjekt.
function renderBackupsBody(status, body) {
  const frag = document.createDocumentFragment();

  if (!status || !status.available) {
    frag.appendChild(el("p", { class: "muted" },
      "F\u00FCr diese Installation ist keine Sicherungs\u00FCbersicht eingerichtet. " +
      "Im geb\u00FCndelten Stack sichert der Dienst automatisch t\u00E4glich; Details im Betriebs-Backup-Leitfaden."));
    return frag;
  }

  const info = el("div", { class: "muted", style: "margin-bottom:10px;" });
  info.appendChild(el("div", null, "Letzte erfolgreiche Sicherung: " +
    (status.last_success ? fmtTimestamp(status.last_success) : "\u2013 (noch keine)")));
  if (status.last_verify_success) {
    info.appendChild(el("div", null, "Letzter erfolgreicher Selbsttest: " + fmtTimestamp(status.last_verify_success)));
  }
  frag.appendChild(info);

  const backups = status.backups || [];
  if (backups.length) {
    const rows = backups.map((b) => [
      b.file,
      b.created_at ? fmtTimestamp(b.created_at) : "\u2013",
      b.app_version || "\u2013",
      fmtBytes(b.size_bytes),
      b.encrypted ? "ja" : "nein",
    ]);
    frag.appendChild(table(["Datei", "Zeitpunkt", "Version", "Gr\u00F6\u00DFe", "Verschl\u00FCsselt"], rows));
  } else {
    frag.appendChild(el("p", { class: "muted" }, "Noch keine Sicherungen vorhanden."));
  }

  frag.appendChild(el("div", { style: "display:flex; gap:10px; margin-top:12px; flex-wrap:wrap;" },
    el("button", { class: "btn", onClick: () => triggerBackupNow(body) }, "Jetzt sichern"),
    el("button", { class: "btn small", onClick: () => loadBackupsPanel(body) }, "Aktualisieren")));

  // Wiederherstellung ist bewusst KEINE GUI-Funktion: ein Restore verwirft die
  // laufende Datenbank und laeuft mit erhoehten Rechten am Dump-Volume, auf das
  // die API nach der Sicherheitsregel "kein Web-/API-Zugriff auf das
  // Backup-Verzeichnis" bewusst keinen Zugriff hat. Er erfolgt daher als
  // gefuehrter Ops-Ablauf (restore.sh). Hier nur ein Hinweis mit Verweis.
  const restoreNote = el("p", { class: "muted", style: "margin-top:14px;" },
    "ℹ️ Eine Wiederherstellung erfolgt aus Sicherheitsgründen nicht über die " +
    "Oberfläche, sondern als geführter Betriebsablauf (restore.sh). Anleitung: ");
  restoreNote.appendChild(el("a",
    { href: docUrl("Betriebs-Backup-Leitfaden.md"), target: "_blank", rel: "noopener" },
    "Betriebs-Backup-Leitfaden"));
  restoreNote.appendChild(document.createTextNode("."));
  frag.appendChild(restoreNote);
  return frag;
}

// Fordert eine Sofort-Sicherung an (Marker); der Dienst fuehrt sie kurz darauf aus.
async function triggerBackupNow(body) {
  try {
    await api.post("/admin/backups/run-now");
    toast("ok", "Sicherung angefordert",
      ["Der Sicherungsdienst f\u00FChrt sie in K\u00FCrze aus. \u201EAktualisieren\u201C zeigt sie danach an."]);
    setTimeout(() => loadBackupsPanel(body), 5000);
  } catch (err) {
    toastError(err);
  }
}

// Epoch-Sekunden (Float) menschenlesbar; die Mail-Outbox stempelt so (analog outbox.py).
function fmtEpoch(sec) {
  if (!sec) return "–";
  try { return new Date(sec * 1000).toLocaleString("de-DE"); } catch (_e) { return "–"; }
}

const MAIL_STATE_LABELS = {
  PENDING: "ausstehend", FAILED: "in Wiederholung", SENT: "zugestellt", DEAD: "gescheitert",
  DROPPED: "verworfen \u2013 kein Mailserver",
};

// Fuellt den E-Mail-Ausgang-Bereich asynchron (GET /admin/mail-outbox).
async function loadMailOutboxPanel(body) {
  clear(body);
  body.appendChild(el("p", { class: "muted" }, "Wird geladen …"));
  try {
    const status = await api.get("/admin/mail-outbox");
    clear(body);
    body.appendChild(renderMailOutboxBody(status, body));
  } catch (err) {
    clear(body);
    const d = describeError(err);
    body.appendChild(el("p", { class: "muted" }, d.title));
  }
}

// Baut den Inhalt des E-Mail-Ausgang-Panels aus dem Statusobjekt. Zeigt bewusst
// nur Metadaten (Empfaengeranzahl, Zustand, Betreff) -- keine Adressliste, keinen
// Text (Datensparsamkeit).
function renderMailOutboxBody(status, body) {
  const frag = document.createDocumentFragment();

  const info = el("div", { class: "muted", style: "margin-bottom:10px;" });
  if (!status.configured) {
    info.appendChild(el("div", null,
      "Kein SMTP-Server konfiguriert – Benachrichtigungen werden modelliert und " +
      "protokolliert, aber nicht versendet. Den Mailserver in deploy/.env eintragen (PROCWORKS_SMTP_HOST, PROCWORKS_MAIL_FROM u. a.; siehe README, Abschnitt \u201EEinstellungen\u201C)."));
  }
  info.appendChild(el("div", null,
    `Gesamt ${status.total} · ausstehend ${status.pending} · in Wiederholung ` +
    `${status.failed} · zugestellt ${status.sent} · gescheitert ${status.dead}`
    + ` · verworfen (kein Mailserver) ${status.dropped || 0}`));
  frag.appendChild(info);

  const entries = status.entries || [];
  if (entries.length) {
    const rows = entries.map((e) => [
      MAIL_STATE_LABELS[e.state] || e.state,
      e.node_label || e.node_id,
      e.subject || "–",
      String(e.recipient_count),
      `${e.attempts}/${e.max_attempts}`,
      fmtEpoch(e.created_at),
      e.last_error ? el("span", { class: "pill pill-red", title: e.last_error }, "Fehler") : "–",
    ]);
    frag.appendChild(table(
      ["Zustand", "Schritt", "Betreff", "Empf.", "Versuche", "Angelegt", "Letzter Fehler"], rows));
  } else {
    frag.appendChild(el("p", { class: "muted" }, "Noch keine Benachrichtigung in der Warteschlange."));
  }

  frag.appendChild(el("div", { style: "display:flex; gap:10px; margin-top:12px; flex-wrap:wrap;" },
    el("button", { class: "btn", onClick: () => dispatchMailOutbox(body) }, "Fällige jetzt senden"),
    el("button", { class: "btn small", onClick: () => loadMailOutboxPanel(body) }, "Aktualisieren")));
  return frag;
}

// Stoesst einen manuellen Versand faelliger Eintraege an (POST …/dispatch) und
// zeigt das Ergebnis. Nuetzlich nach einer SMTP-Stoerung.
async function dispatchMailOutbox(body) {
  try {
    const status = await api.post("/admin/mail-outbox/dispatch");
    clear(body);
    body.appendChild(renderMailOutboxBody(status, body));
    toast("ok", "Versand angestoßen", [`Zugestellt: ${status.sent} · gescheitert: ${status.dead}`
      + ` · verworfen (kein Mailserver): ${status.dropped || 0}`]);
  } catch (err) { toastError(err); }
}

async function openInstanceFromMonitor(id) {
  try {
    await loadInstance(id);
    if (state.instance.schema_id !== state.schemaId) {
      state.schemaId = state.instance.schema_id;
      await refreshSchema();
      renderSchemaPicker();
    }
    render();
  } catch (err) { toastError(err); }
}

// Administrator-Wartung: System zur\u00FCcksetzen bzw. einen Beispieldatensatz
// laden. ``kind`` ist "demo" (schlanker Kosmos), "o2c" (der grosse
// Order-to-Cash-Datensatz) oder "wipe" (leeres System). Alle drei laufen ueber
// denselben Endpunkt POST /admin/reset, der immer zuerst loescht.
const RESET_KINDS = {
  demo: {
    title: "Beispieldaten laden",
    confirm: "Beispieldaten laden",
    done: "Beispieldaten geladen",
    msg: "Alle vorhandenen Daten werden gel\u00F6scht und durch die Beispieldaten ersetzt. Im Login-Betrieb entstehen dabei Beispiel-Anmeldungen (u. a. eine Modelliererin) mit einem zuf\u00E4lligen Passwort, das danach einmal angezeigt wird. M\u00F6chten Sie fortfahren?",
    body: { load_demo: true },
  },
  o2c: {
    title: "Order-to-Cash-Beispiel laden",
    confirm: "Order-to-Cash laden",
    done: "Order-to-Cash-Beispiel geladen",
    msg: "Alle vorhandenen Daten werden gel\u00F6scht und durch den Order-to-Cash-Datensatz ersetzt: sechs Prozesse, eine eigene Organisation und neun Vorg\u00E4nge an unterschiedlichen Stellen. Im Login-Betrieb entstehen dabei Beispiel-Anmeldungen mit einem zuf\u00E4lligen Passwort, das danach einmal angezeigt wird. M\u00F6chten Sie fortfahren?",
    body: { load_o2c: true },
  },
  wipe: {
    title: "Auf Null zur\u00FCcksetzen",
    confirm: "Endg\u00FCltig l\u00F6schen",
    done: "System auf Null zur\u00FCckgesetzt",
    msg: "Alle Schemata, Instanzen und Organisationsmodelle werden gel\u00F6scht. Im Login-Betrieb werden zus\u00E4tzlich alle Nutzer au\u00DFer Ihnen und dem Administrator-Konto entfernt. Dieser Schritt kann nicht r\u00FCckg\u00E4ngig gemacht werden.",
    body: {},
  },
};

function confirmReset(kind) {
  const spec = RESET_KINDS[kind] || RESET_KINDS.wipe;
  openModal(
    spec.title,
    el("p", { class: "muted" }, spec.msg),
    async () => {
      const password = await runReset(kind);
      // Der Passwort-Dialog ersetzt diesen hier; ``true`` wuerde ihn beim
      // Schliessen gleich wieder entfernen (openModal teilt sich einen Container).
      if (password) { showExamplePassword(password); return false; }
      return true;
    },
    spec.confirm, { danger: true });
}

/**
 * Fuehrt die Wartung aus (POST /admin/reset) und laedt die Oberflaeche neu.
 * @param {string} kind "demo", "o2c" oder "wipe" (siehe RESET_KINDS)
 * @returns {Promise<string|null>} das Passwort neu angelegter Beispiel-
 *   Anmeldungen -- der Aufrufer zeigt es an -- sonst ``null``
 */
async function runReset(kind) {
  const spec = RESET_KINDS[kind] || RESET_KINDS.wipe;
  try {
    const res = await api.post("/admin/reset", spec.body);
    const lines = [
      `Schemata: ${res.schemas}`,
      `Instanzen: ${res.instances}`,
      `Organisationsmodelle: ${res.org_models}`,
    ];
    if (state.passwordLogin) lines.push(`Nutzerkonten: ${res.users}`);
    toast("ok", spec.done, lines);
    // Auswahl zur\u00FCcksetzen, da bisherige Schemata/Instanzen evtl. weg sind.
    state.instance = null;
    state.schema = null;
    state.schemaId = null;
    await boot();
    return res.example_password || null;
  } catch (err) { toastError(err); return null; }
}

/**
 * Zeichnet die Benutzerverwaltung (Administration → Benutzer) in ``body``.
 *
 * Eine Zeile je Login: Login, Name, Rollen, zugeordnete Person (aus dem
 * Personenverzeichnis, sonst die id) und Zustand („muss Passwort aendern“).
 * Aktionen: „Passwort zuruecksetzen“ (neues Initialpasswort einmal angezeigt,
 * Sitzungen enden) und „Loeschen“ (mit Bestaetigung). Der eigene Login ist
 * nicht loeschbar – der Knopf ist gesperrt und nennt den Grund; der Kern
 * lehnt es zusaetzlich ab, ebenso das Loeschen des letzten Administrators.
 *
 * @param {HTMLElement} body Panel-Koerper; wird bei jedem Aufruf neu gefuellt
 * @returns {Promise<void>}
 */
async function loadUsersPanel(body) {
  let users;
  try { users = await api.get("/users"); }
  catch (err) {
    clear(body);
    body.appendChild(emptyState(describeError(err).title));
    return;
  }
  clear(body);
  const me = state.principal && state.principal.subject;
  const reload = () => loadUsersPanel(body);
  const rows = (users || []).slice().sort((a, b) => a.login.localeCompare(b.login)).map((u) => {
    const roles = (u.roles || []).map((r) => ROLE_LABELS[r] || r).join(", ") || "\u2013";
    const person = u.agent_id
      ? ((state.agentDirectory[u.agent_id] && state.agentDirectory[u.agent_id].name) || u.agent_id)
      : el("span", { class: "muted" }, "\u2013");
    const status = u.must_change
      ? el("span", { class: "pill pill-amber", title: "Initialpasswort noch nicht ge\u00E4ndert" }, "muss Passwort \u00E4ndern")
      : el("span", { class: "pill pill-green" }, "aktiv");
    const own = u.login === me;
    const actions = el("div", { style: "display:flex; gap:6px; justify-content:flex-end; flex-wrap:wrap" },
      el("button", { class: "btn small", onClick: () => confirmUserPasswordReset(u, reload) },
        "Passwort zur\u00FCcksetzen"),
      el("button", { class: "btn small danger", disabled: own,
        title: own ? "Den eigenen Login kann man nicht l\u00F6schen \u2013 sonst w\u00E4re man ausgesperrt." : null,
        onClick: () => confirmUserDelete(u, reload) }, "L\u00F6schen"));
    return [u.login, u.display_name || "\u2013", roles, person, status, actions];
  });
  body.appendChild(rows.length
    ? table(["Login", "Name", "Rollen", "Person", "Zustand", ""], rows)
    : emptyState("Keine Logins."));
}

/**
 * Rueckfrage und Zuruecksetzen des Passworts eines Logins.
 *
 * Nach Erfolg ersetzt der Zugangsdaten-Dialog diesen (Rueckgabe ``false``);
 * das neue Initialpasswort steht nur dort. Bestehende Sitzungen des Logins
 * beendet der Kern.
 *
 * @param {{login: string}} user der Login
 * @param {() => void} onDone laedt die Liste neu
 */
function confirmUserPasswordReset(user, onDone) {
  openModal(`Passwort zur\u00FCcksetzen: ${user.login}`,
    el("p", { class: "muted" },
      "Erzeugt ein neues Initialpasswort, das nur einmal angezeigt wird. Bestehende Anmeldungen dieses Logins enden; beim n\u00E4chsten Anmelden vergibt die Person ein eigenes Passwort."),
    async () => {
      try {
        const res = await api.post(`/users/${encodeURIComponent(user.login)}/reset-password`);
        showLoginCredentials(res, "Neues Initialpasswort");
        onDone();
        return false;
      } catch (err) { toastError(err); return false; }
    }, "Zur\u00FCcksetzen", { danger: true });
}

/**
 * Bestaetigung und Loeschen eines Logins.
 *
 * Die Person im Organisationsmodell bleibt erhalten; nur die Anmeldung
 * verschwindet, und ihre Sitzungen enden. Lehnt der Kern ab (eigener Login,
 * letzter Administrator), steht sein Grund in der Fehlermeldung.
 *
 * @param {{login: string}} user der Login
 * @param {() => void} onDone laedt die Liste neu
 */
function confirmUserDelete(user, onDone) {
  openModal(`Login l\u00F6schen: ${user.login}`,
    el("p", { class: "muted" },
      "Der Login wird entfernt, laufende Anmeldungen enden sofort. Die Person im Organisationsmodell, ihre Aufgaben und der Verlauf bleiben erhalten."),
    async () => {
      try {
        await api.del(`/users/${encodeURIComponent(user.login)}`);
        toast("ok", "Login gel\u00F6scht", [user.login]);
        onDone();
      } catch (err) { toastError(err); return false; }
    }, "L\u00F6schen", { danger: true });
}

/**
 * Zeigt das Passwort der gerade angelegten Beispiel-Anmeldungen -- genau
 * einmal, in einem Dialog statt einer verschwindenden Meldung.
 *
 * Bis 1.27.2 trugen diese Anmeldungen das auf der Website veroeffentlichte
 * Demo-Passwort; auf einer Kundeninstallation war damit u. a. eine
 * Modellierer-Anmeldung fuer jeden offen. Der Server vergibt jetzt ein
 * zufaelliges Passwort und nennt es nur in der Antwort auf das Laden.
 *
 * Ergebnisdialog (``cancel: false``): nur „Verstanden“ und ein Kopierknopf;
 * der Text verweist auf Administration → Benutzer zum Aufraeumen.
 * @param {string} password das Passwort aller Beispiel-Anmeldungen
 */
function showExamplePassword(password) {
  openModal("Beispiel-Anmeldungen angelegt",
    el("div", null,
      el("p", null, "Die Beispieldaten bringen Anmeldungen mit, z. B. ",
        el("code", null, "mara.modell"), " oder ", el("code", null, "erika.sander"),
        ". Ihr gemeinsames Passwort wird nur jetzt angezeigt:"),
      copyField("Passwort der Beispiel-Anmeldungen", password),
      el("p", { class: "muted", style: "font-size:13px" },
        "Vor dem echten Einsatz die Beispiel-Anmeldungen unter Administration \u2192 Benutzer l\u00F6schen oder ihnen neue Passw\u00F6rter geben \u2013 oder das System auf Null zur\u00FCcksetzen.")),
    async () => true, "Verstanden", { cancel: false });
}

/** Deutsche Namen der Vorgangszustaende (statt RUNNING/COMPLETED roh). */
const INSTANCE_STATE_LABELS = { RUNNING: "l\u00E4uft", COMPLETED: "abgeschlossen" };

function statePillFor(s) {
  return el("span", { class: "pill " + (s === "COMPLETED" ? "pill-green" : "pill-blue"), title: s },
    INSTANCE_STATE_LABELS[s] || s);
}

// Inzident eines externen Tasks aufloesen (Aufgabe wird erneut eingereiht).
async function resolveIncident(inc) {
  try {
    await api.post(`/v1/incidents/${inc.id}/resolve`);
    render();
    toast("ok", "Inzident aufgel\u00F6st", ["Aufgabe erneut eingereiht."]);
  } catch (err) { toastError(err); }
}

// Audit-/Monitoring-Hilfen (Schritt 15)
const EVENT_LABELS = {
  INSTANCE_CREATED: "Instanz erstellt",
  ACTIVITY_STARTED: "Aktivit\u00E4t gestartet",
  ACTIVITY_COMPLETED: "Aktivit\u00E4t abgeschlossen",
  ACTIVITY_SUPERVISED: "Aufsichtseingriff",
  BRANCH_DECIDED: "Zweig entschieden",
  ADHOC_INSERTED: "Ad-hoc eingef\u00FCgt",
  ADHOC_DELETED: "Ad-hoc gel\u00F6scht",
  ADHOC_RENAMED: "Ad-hoc umbenannt",
  INSTANCE_MIGRATED: "Instanz migriert",
  INSTANCE_DATA_SET: "Daten ge\u00E4ndert",
  INSTANCE_COMPLETED: "Instanz abgeschlossen",
  ACTIVITY_CLAIMED: "Aufgabe \u00FCbernommen",
  ACTIVITY_RETURNED: "Aufgabe zur\u00FCckgelegt",
  ACTIVITY_SUSPENDED: "Aufgabe angehalten",
  ACTIVITY_RESUMED: "Weiterarbeit aufgenommen",
  ACTIVITY_FAILED: "Problem gemeldet",
  ACTIVITY_RESET: "Wiederanlauf",
  TASK_ESCALATED: "Eskaliert",
  MAIL_SENT: "E-Mail versendet",
  MAIL_FAILED: "E-Mail gescheitert",
  TIME_ANCHOR: "Zeitbezug gesetzt",
};

function eventLabel(t) { return EVENT_LABELS[t] || t; }

/**
 * Wer hat das Ereignis ausgeloest? Reihenfolge: gebundener Bearbeiter, dann der
 * Login (`detail.actor`, gesetzt wenn kein Agent dahintersteht), sonst
 * "System" -- und das nur fuer Ereignisse, die tatsaechlich die Maschine
 * ausloest. Fruehere Fassung zeigte "System" auch fuer manuelle Abschluesse.
 * @param {{agent_id?: string|null, detail?: Record<string,string>}} ev Audit-Ereignis
 * @returns {string}
 */
function auditActorLabel(ev) {
  if (ev.agent_id) return agentNameOf(ev.agent_id);
  const actor = ev.detail && ev.detail.actor;
  if (actor) return actor === "anonymous" ? "unbekannt (offener Zugang)" : actor;
  return "System";
}

/**
 * Detailspalte eines Audit-Ereignisses: Schrittname; bei einem Aufsichtseingriff
 * zusaetzlich die Begruendung.
 * @param {{label?: string|null, node_id?: string|null, detail?: Record<string,string>}} ev Audit-Ereignis
 * @returns {string}
 */
function auditDetailText(ev) {
  let base = ev.label || ev.node_id || "\u2013";
  const detail = ev.detail || {};
  // Datenaenderung: Element mit altem und neuem Wert. Beide reisen als
  // JSON im Detail; ``null`` heisst „war noch nicht gesetzt".
  if (ev.event_type === "INSTANCE_DATA_SET" && "new" in detail) {
    const show = (raw) => {
      let v;
      try { v = JSON.parse(raw); } catch (e) { v = raw; }
      if (v === null || v === undefined) return "leer";
      if (v === true) return "Ja";
      if (v === false) return "Nein";
      return String(v);
    };
    base = `${base}: ${show(detail.old)} \u2192 ${show(detail.new)}`;
  }
  // Ad-hoc: ``label`` ist der Schritt davor bzw. der alte Name, das
  // Detail traegt den neuen Schritt bzw. den neuen Namen.
  if (ev.event_type === "ADHOC_INSERTED" && detail.label) {
    base = `\u201E${detail.label}\u201C nach ${base}`;
  } else if (ev.event_type === "ADHOC_RENAMED" && detail.label) {
    base = `${base} \u2192 \u201E${detail.label}\u201C`;
  }
  const reason = detail.reason;
  return reason ? `${base} \u2013 Begr\u00FCndung: ${reason}` : base;
}

function fmtTimestamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("de-DE");
}

function fmtDuration(sec) {
  if (sec == null) return "\u2013";
  // „0.0 s" sah aus wie eine kaputte Kachel, war aber die ehrliche Auskunft
  // ueber Vorgaenge, die in derselben Sekunde entstanden und endeten.
  // Die Demo-Daten haben einen Zeitverlauf; bleibt der Wert dennoch unter einer Sekunde, sagt die Kachel
  // das jetzt, statt eine Null zu zeigen.
  if (sec < 1) return "< 1 s";
  if (sec < 60) return fmtNumber(sec, 1) + " s";
  if (sec < 3600) return fmtNumber(sec / 60, 1) + " min";
  return fmtNumber(sec / 3600, 1) + " h";
}

