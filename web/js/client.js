// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - API-Zugriff und Modal.
 *
 * Der einzige Weg zur API (``request``) und der gemeinsame Dialog
 * (``openModal``).
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// Web-Client
// --------------------------------------------------------------------------

/**
 * Der einzige Weg, auf dem dieser Client die API erreicht.
 *
 * Genau deshalb sitzt hier der Sperrpunkt der gefuehrten Tour: Solange eine
 * Sandkasten-Tour laeuft, beantwortet ``Tour.intercept`` jeden schreibenden
 * Aufruf aus einer Aufzeichnung, und es findet KEIN fetch statt. Dadurch
 * entstehen durch Tutorial-Eingaben keine dauerhaften Daten.
 * Ausserhalb der Tour ist der Aufruf ein No-op.
 *
 * @param {string} method HTTP-Methode.
 * @param {string} path Pfad ab der API-Basis.
 * @param {object} [body] Anfragekoerper (JSON).
 * @returns {Promise<*>} Antwortkoerper; wirft bei Fehlern {status, detail}.
 */
async function request(method, path, body) {
  if (typeof Tour !== "undefined") {
    const simulated = Tour.intercept(method, path, body);
    if (simulated) return simulated();
  }
  let resp;
  try {
    resp = await fetch(state.apiBase + path, {
      method,
      headers: authHeaders(body !== undefined),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    setConnected(false);
    throw { detail: `Keine Verbindung zur API (${state.apiBase}). L\u00E4uft der Server?` };
  }
  setConnected(true);
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_e) {
    // Keine JSON-Antwort: Hinter der Adresse steht kein ProcWorks-Kern, meist
    // die Weboberflaeche selbst (sonst stuende hier „Unexpected token '<' …“).
    throw { detail: `Unter ${state.apiBase} antwortet kein ProcWorks-Server. Die API-Adresse unten in der Seitenleiste pr\u00FCfen (\u00FCblich: ${defaultApiBase()}).` };
  }
  if (!resp.ok) {
    // Demo: Sitzung verloren -- einmal still neu anmelden, statt den
    // Besucher auf einer nackten Anmeldung abzusetzen. Nicht fuer den Login
    // selbst, sonst ruft er sich endlos auf.
    if (resp.status === 401 && state.demo && state.token && path !== "/auth/login") {
      recoverDemoSession();
    }
    throw { status: resp.status, detail: data && data.detail };
  }
  return data;
}

/**
 * Antwortet unter ``base`` ein ProcWorks-Kern?
 * @param {string} base API-Basisadresse, z. B. "http://localhost/api"
 * @returns {Promise<boolean>} true bei ``GET /health`` mit JSON ``{status: "ok"}``
 */
async function isProcWorksApi(base) {
  try {
    const resp = await fetch(`${base}/health`, { cache: "no-store" });
    const data = await resp.json();
    return !!(data && data.status === "ok");
  } catch (_e) { return false; }
}

/** Laeuft gerade eine Wiederanmeldung der Demo? (nur eine zugleich) */
let demoRecovering = false;

/**
 * Stellt eine verlorene Demo-Sitzung wieder her.
 *
 * Die Demo-Instanz haelt Sitzungen und Daten im Speicher. Startet die Maschine
 * nach einer Leerlaufpause neu, ist beides weg -- der Besucher landete nach
 * ~45 min ohne Hinweis und ohne Demo-Leiste auf der Anmeldung. Hier meldet sich
 * der Client als dieselbe Demo-Person wieder an (Passwort kennt er aus
 * ``/auth/config``) und sagt ehrlich, was passiert ist. Scheitert das, bleibt
 * die normale Anmeldung.
 * @returns {Promise<boolean>} true, wenn wieder angemeldet
 */
async function recoverDemoSession() {
  if (demoRecovering || !state.demo || !state.demoPassword) return false;
  demoRecovering = true;
  try {
    const login = storageGet(sessionStorage, DEMO_LAST_LOGIN_KEY) || state.demoAutologin;
    if (!login || !(await demoLoginAs(login))) return false;
    toast("info", "Die Demo wurde nach einer Pause neu gestartet",
      ["Du bist wieder angemeldet. Was du vorher angelegt hattest, ist zur\u00FCckgesetzt."]);
    await boot();
    return true;
  } finally {
    demoRecovering = false;
  }
}

// Build request headers, attaching the bearer token when the user is logged in.
function authHeaders(hasBody) {
  const headers = {};
  if (hasBody) headers["Content-Type"] = "application/json";
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  return headers;
}

const api = {
  get: (p) => request("GET", p),
  post: (p, b) => request("POST", p, b === undefined ? {} : b),
  put: (p, b) => request("PUT", p, b === undefined ? {} : b),
  patch: (p, b) => request("PATCH", p, b === undefined ? {} : b),
  del: (p) => request("DELETE", p),
  raw: async (p) => {
    const resp = await fetch(state.apiBase + p, { headers: authHeaders(false) });
    if (!resp.ok) throw { status: resp.status, detail: "Export fehlgeschlagen" };
    return resp.text();
  },
};

function setConnected(ok) {
  const pill = byId("conn-pill");
  pill.textContent = ok ? "verbunden" : "getrennt";
  pill.className = "pill " + (ok ? "pill-green" : "pill-gray");
}

// Show the running software version (reported by the API's /health endpoint,
// which reads it from the installed package -- the single source of truth).
function showVersion(version) {
  const el = byId("app-version");
  if (!el) return;
  el.textContent = version ? `ProcWorks v${version}` : "ProcWorks";
}

// --------------------------------------------------------------------------
// Modal
// --------------------------------------------------------------------------

/**
 * Oeffnet den einen Dialog der App (``#modal-root``) und ersetzt dabei einen
 * offenen.
 *
 * @param {string} title Ueberschrift
 * @param {Node} bodyNode Inhalt
 * @param {() => (boolean|undefined|Promise<boolean|undefined>)} onConfirm
 *   Rueckruf des Bestaetigen-Knopfs. Alles ausser ``false`` schliesst danach
 *   den Dialog. **Oeffnet der Rueckruf selbst einen Folgedialog, muss er
 *   ``false`` zurueckgeben** – sonst schliesst dieser Aufruf den Container und
 *   nimmt den Folgedialog mit (so verschwanden die Zugangsdaten eines neuen
 *   Logins ungesehen). Ein Waechter prueft das fuer alle Aufrufer.
 * @param {string} [confirmLabel] Beschriftung des Bestaetigen-Knopfs
 * @param {{cancel?: boolean, danger?: boolean}} [opts]
 *   ``cancel: false`` – Ergebnisdialog: kein „Abbrechen“ (es gibt nichts
 *   abzubrechen) und kein Schliessen per Klick neben den Dialog, damit ein
 *   einmalig gezeigtes Ergebnis (Passwort) nicht versehentlich verschwindet;
 *   Escape und der eine Knopf schliessen weiterhin.
 *   Unabhaengig davon schliesst ein Klick daneben nie einen Dialog, in dem
 *   schon etwas eingegeben wurde (input/change); er zeigt dann einen Hinweis.
 *   ``danger: true`` – zerstoerende Aktion: Knopf als Gefahr gestaltet; ohne
 *   Eingabefelder startet der Fokus auf „Abbrechen“, damit Enter nichts
 *   unwiderruflich ausloest (mit Feldern im ersten Feld).
 * @returns {{modal: HTMLElement, confirmBtn: HTMLElement, close: () => void}}
 */
function openModal(title, bodyNode, onConfirm, confirmLabel, opts) {
  const o = opts || {};
  const withCancel = o.cancel !== false;
  const root = byId("modal-root");
  // Wer den Dialog oeffnete, bekommt den Fokus danach zurueck. Kommt
  // der Aufruf aus einem schon offenen Dialog (Dialog ersetzt Dialog), zaehlt
  // dessen Ausloeser, nicht der verschwindende Knopf darin.
  const opener = root.contains(document.activeElement)
    ? openModal._opener : document.activeElement;
  openModal._opener = opener;
  clear(root);
  const close = () => {
    clear(root);
    // Sperre sofort aufheben: Der Beobachter meldet sich erst asynchron, und in
    // eine noch ``inert`` gesetzte App laesst sich kein Fokus zurueckgeben.
    syncInert();
    if (opener && opener.isConnected && typeof opener.focus === "function") {
      try { opener.focus(); } catch (_e) { /* nur Komfort */ }
    }
  };
  const confirmBtn = el("button", {
    class: o.danger ? "btn danger" : "btn primary",
    onClick: async () => {
      const ok = await onConfirm();
      if (ok !== false) close();
    },
  }, confirmLabel || "Anwenden");
  const cancelBtn = withCancel ? el("button", { class: "btn ghost", onClick: close }, "Abbrechen") : null;
  // Ein Klick neben den Dialog schliesst ihn nur, solange nichts eingegeben
  // wurde. Vorher verwarf er still einen halb gestalteten Maskenentwurf; jetzt
  // bleibt der Dialog stehen und sagt, wie man bewusst verwirft (Abbrechen/Esc).
  let edited = false;
  const keepHint = el("span", { class: "modal-keep-hint", role: "status" });
  const modal = el("div", { class: "modal-backdrop",
      onClick: (e) => {
        if (!withCancel || e.target !== e.currentTarget) return;
        if (edited) {
          keepHint.textContent = "Ungespeicherte Eingaben \u2013 zum Verwerfen \u201EAbbrechen\u201C oder Esc.";
          // Der Klick hat den Fokus aus dem Dialog genommen; ohne ihn erreichte
          // Esc den Dialog nicht mehr (sein Tastenweg haengt am Dialog).
          const back = lastFocus && modal.contains(lastFocus) ? lastFocus
            : modal.querySelector("input, select, textarea, button");
          if (back && typeof back.focus === "function") back.focus();
          return;
        }
        close();
      } },
    el("div", { class: "modal" },
      el("div", { class: "modal-h" }, el("h3", null, title)),
      el("div", { class: "modal-b" }, bodyNode),
      el("div", { class: "modal-f" }, keepHint, cancelBtn, confirmBtn)));
  const markEdited = () => { edited = true; };
  modal.addEventListener("input", markEdited);
  modal.addEventListener("change", markEdited);
  let lastFocus = null;
  modal.addEventListener("focusin", (e) => { lastFocus = e.target; });
  // Enter bestaetigt den Dialog -- einmal hier fuer alle Aufrufer statt je
  // Dialog.
  // Ausnahmen: mehrzeilige Felder (Enter = Zeilenumbruch), fokussierte Knoepfe
  // und Links (Enter loest deren eigene Aktion aus), Auswahllisten, jede
  // Zusatztaste und laufende IME-Eingaben.
  modal.addEventListener("keydown", (e) => {
    // Escape schliesst wie „Abbrechen“. Der globale Escape-Handler
    // ueberlaesst die Taste bewusst dem offenen Dialog.
    if (e.key === "Escape" && !e.isComposing) { e.preventDefault(); close(); return; }
    // Tab bleibt im Dialog und laeuft im Kreis. ``inert`` sperrt nur die
    // App; Tour-Angebot, Meldungen und Demo-Leiste liegen daneben und haetten
    // den Fokus sonst aus dem Dialog gezogen.
    if (e.key === "Tab") {
      const focusables = [...modal.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
        .filter((x) => !x.disabled && x.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0], last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      return;
    }
    if (e.key !== "Enter" || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.isComposing) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (tag === "TEXTAREA" || tag === "BUTTON" || tag === "A" || tag === "SELECT") return;
    e.preventDefault();
    confirmBtn.click();
  });
  root.appendChild(modal);
  // Fokus in den Dialog: ins erste Feld, sonst auf „Bestaetigen“. Ohne
  // Feld blieb er auf dem Knopf hinter dem Dialog; Tab lief dann nur durch die
  // Seite und erreichte „Abschliessen“ nie. Die Seite dahinter ist waehrend
  // des Dialogs ``inert`` (siehe watchInert), Tab bleibt also im Dialog.
  // Zerstoerende Aktion: Fokus auf „Abbrechen“ – Enter auf einem Knopf loest
  // dessen eigene Aktion aus, also bricht Enter hier ab statt zu loeschen.
  const firstInput = modal.querySelector("input:not([readonly]), select, textarea");
  // Mit Eingabefeldern (etwa Pflicht-Anlass) beginnt man dort; ohne Felder
  // landet der Fokus auf „Abbrechen“.
  if (o.danger && cancelBtn && !firstInput) cancelBtn.focus();
  else (firstInput || confirmBtn).focus();
  // Rueckgabe fuer Dialoge, die ihren Bestaetigen-Knopf je nach Eingabe
  // sperren (z. B. der Einfuege-Dialog ohne Diskriminator). Bisherige Aufrufer
  // ignorieren sie.
  return { modal, confirmBtn, close };
}

