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
 * Ein **Serverausfall** (kein fetch moeglich, oder 502/503/504 ohne
 * JSON-Rumpf des Kerns -- so antwortet der vorgeschaltete Proxy, wenn die
 * API steht) setzt die Statusanzeige auf „getrennt“ und wirft
 * ``{status, unreachable: true, detail: {code: "NET.unreachable"}}``
 * (``unreachableError``). Jede Antwort des Kerns -- auch ein 502 mit
 * JSON-Rumpf, wenn ein angebundenes Fremdsystem scheitert, oder eine
 * 4xx-Ablehnung -- gilt als „verbunden“.
 *
 * @param {string} method HTTP-Methode.
 * @param {string} path Pfad ab der API-Basis.
 * @param {object} [body] Anfragekoerper (JSON).
 * @returns {Promise<*>} Antwortkoerper; wirft bei Fehlern {status, detail}
 *   (bei einem Ausfall zusaetzlich ``unreachable: true``).
 */
async function request(method, path, body) {
  if (typeof Tour !== "undefined") {
    const simulated = Tour.intercept(method, path, body);
    if (simulated) return simulated();
  }
  // Das Token dieser Anfrage: Kommen zwei Ablehnungen gleichzeitig zurueck,
  // hat die erste die Sitzung schon beendet -- die zweite gehoert trotzdem dazu.
  const sentToken = state.token;
  let resp;
  try {
    resp = await fetch(state.apiBase + path, {
      method,
      headers: authHeaders(body !== undefined),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    setConnected(false);
    throw unreachableError(0);
  }
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_e) {
    // Ein Gateway-Fehler des Proxys kommt als HTML oder Klartext: Ausfall.
    if (GATEWAY_STATUSES.has(resp.status)) {
      setConnected(false);
      throw unreachableError(resp.status);
    }
    setConnected(true);
    // Keine JSON-Antwort: Hinter der Adresse steht kein ProcWorks-Kern, meist
    // die Weboberflaeche selbst (sonst stuende hier „Unexpected token '<' …“).
    throw { detail: `Unter ${state.apiBase} antwortet kein ProcWorks-Server. Die API-Adresse unten in der Seitenleiste pr\u00FCfen (\u00FCblich: ${defaultApiBase()}).` };
  }
  // 502/503/504 ohne ``detail`` des Kerns: der Proxy meldet die stehende API.
  if (GATEWAY_STATUSES.has(resp.status) && !(data && data.detail)) {
    setConnected(false);
    throw unreachableError(resp.status);
  }
  setConnected(true);
  if (!resp.ok) {
    if (isSessionEnd(resp.status, path, sentToken, data && data.detail)) {
      // Demo: Sitzung verloren -- einmal still neu anmelden, statt den
      // Besucher auf einer nackten Anmeldung abzusetzen.
      if (state.demo) {
        recoverDemoSession();
      } else {
        // Idempotent: nur die erste Ablehnung beendet die Sitzung.
        expireSession();
        // Der englische Kernsatz („invalid session“) erreicht nie den
        // Schirm: Jeder Aufrufer, der den Fehler anzeigt, zeigt den
        // Katalogtext. ``noticeShown``: Die Anmeldemaske nennt den Grund
        // schon -- ``toastError`` legt keine zweite Meldung daneben.
        throw { status: 401, sessionExpired: true, noticeShown: !!loginNotice,
          detail: { code: "AUTH.session-expired", params: {}, message: data && data.detail } };
      }
    }
    throw { status: resp.status, detail: data && data.detail };
  }
  return data;
}

/**
 * Pfade, deren 401 nie das Ende der Sitzung meldet: die Anmeldung selbst
 * (falsches Passwort; vorher gab es keine Sitzung) und das Abmelden (raeumt
 * ohnehin ab).
 *
 * Der Passwortwechsel steht bewusst nicht hier: Seine 401 heisst entweder
 * „aktuelles Passwort falsch“ (Kern-Code :data:`INPUT_ERROR_CODES`) oder --
 * ohne diesen Code -- „Sitzung beendet“, wie bei jedem anderen Pfad.
 */
const CREDENTIAL_PATHS = new Set(["/auth/login", "/auth/logout"]);

/**
 * Codes, mit denen der Kern eine 401 als falsche Eingabe einer gueltigen
 * Sitzung kennzeichnet (nicht als Sitzungsende). Unterschieden wird am Code,
 * nie am englischen Satz: ``AUTH.wrong-current-password`` beim
 * Passwortwechsel. Eine 401 ohne solchen Code beendet die Sitzung.
 */
const INPUT_ERROR_CODES = new Set(["AUTH.wrong-current-password"]);

/**
 * Hat der Kern mit dieser Antwort die Sitzung des Clients beendet?
 *
 * Eine 401 heisst: Das mitgeschickte Token gilt nicht (mehr) -- abgelaufen,
 * Passwort zurueckgesetzt, Login geloescht, in einem anderen Tab
 * abgemeldet. Nur mit Token gibt es eine Sitzung, die enden kann (offener
 * Modus: keine). 403 (Recht fehlt) und 409 (Stand ueberholt) sagen nichts
 * ueber die Sitzung.
 *
 * @param {number} status HTTP-Status der Antwort.
 * @param {string} path Pfad der Anfrage ab der API-Basis.
 * @param {string} sentToken das Token, mit dem die Anfrage abging (``""``:
 *   ohne Anmeldung). Massgeblich ist dieses, nicht das aktuelle: Eine
 *   gleichzeitige Ablehnung kann es inzwischen geleert haben.
 * @param {*} [detail] ``detail`` der Antwort (Text oder ``{code, …}``). Traegt
 *   es einen Code aus :data:`INPUT_ERROR_CODES`, meldet die 401 eine falsche
 *   Eingabe und kein Sitzungsende. Fehlt es oder ist es ein blosser Text, gilt
 *   die 401 als Sitzungsende (sicherer Weg: lieber neu anmelden als eine
 *   beendete Sitzung als Tippfehler ausgeben).
 * @returns {boolean} true fuer eine 401 einer Anfrage mit Token ausserhalb
 *   von :data:`CREDENTIAL_PATHS`, die keinen Eingabefehler-Code traegt.
 */
function isSessionEnd(status, path, sentToken, detail) {
  if (status !== 401 || !sentToken || CREDENTIAL_PATHS.has(path)) return false;
  const code = detail && typeof detail === "object" ? detail.code : undefined;
  return !INPUT_ERROR_CODES.has(code);
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

/** Statuscodes, mit denen ein Proxy eine nicht erreichbare API meldet. */
const GATEWAY_STATUSES = new Set([502, 503, 504]);

/**
 * Der Fehler, den ``request`` bei einem Serverausfall wirft.
 *
 * Formuliert wird er im Meldungskatalog (``NET.unreachable``), damit Toast,
 * Fehlerzustand der Sichten und Banner denselben Satz zeigen.
 *
 * @param {number} status HTTP-Status des Proxys (502/503/504) oder 0, wenn
 *   gar keine Antwort kam (Netzfehler, Adresse falsch).
 * @returns {{status: number, unreachable: true, detail: object}} der Fehler.
 */
function unreachableError(status) {
  return {
    status,
    unreachable: true,
    detail: { code: "NET.unreachable", params: { status, base: state.apiBase },
      message: `Keine Verbindung zur API (${state.apiBase})` },
  };
}

/**
 * Hat eine fruehere Anfrage einen Ausfall gemeldet, den die offene Sicht
 * noch nicht durch Neuladen ueberwunden hat? ``pollLiveUpdates`` zeichnet
 * dann nach der Wiederkehr einmal neu, auch ohne neue Revision -- sonst
 * bliebe die waehrend des Ausfalls gezeichnete Sicht bis zum Sichtwechsel
 * stehen.
 */
let outageRedrawPending = false;

/**
 * Setzt die Verbindungsanzeige und merkt einen Ausfall vor.
 *
 * @param {boolean} ok true: der Kern hat geantwortet; false: Ausfall
 *   (Netzfehler oder Gateway-Fehler des Proxys, siehe ``request``).
 */
function setConnected(ok) {
  state.connected = ok;
  if (!ok) outageRedrawPending = true;
  const pill = byId("conn-pill");
  pill.textContent = ok ? "verbunden" : "getrennt";
  pill.className = "pill " + (ok ? "pill-green" : "pill-gray");
}

/**
 * Sagt ein Fehler, dass es das Angefragte (fuer diese Anmeldung) nicht gibt?
 *
 * Nur 404 (geloescht, nie da) und 403 (nicht lesbar) rechtfertigen, einen
 * gemerkten Bezug (Vorgang, Pruefinstanz) zu verwerfen. Jeder andere Fehler --
 * vor allem ein Ausfall (``unreachableError``, 502/503/504) -- sagt nichts ueber
 * das Objekt: Es laeuft auf dem Server vermutlich weiter, die Sicht zeigt dann
 * einen Fehlerzustand und behaelt den Bezug, bis der Server wieder antwortet.
 *
 * @param {*} err Fehler von ``request`` (``{status, detail}``) oder etwas anderes.
 * @returns {boolean} true bei 403/404.
 */
function isGoneError(err) {
  return !!err && (err.status === 403 || err.status === 404);
}

/**
 * Fehlerzustand eines Bereichs, dessen Daten nicht geladen werden konnten.
 *
 * Statt des Leerzustands („Keine offenen Aufgaben“, „✓ Keine offenen
 * Inzidente“), der einen leeren Bestand behaupten wuerde. Text aus dem
 * Meldungskatalog (``describeError``).
 *
 * @param {*} err der gefangene Fehler von ``request``.
 * @param {string} [what] was nicht geladen werden konnte, als Satzanfang
 *   (Standard „Die Liste konnte nicht geladen werden“).
 * @returns {HTMLElement} der Hinweis (Rolle ``alert``).
 */
function loadErrorState(err, what = "Die Liste konnte nicht geladen werden") {
  const d = describeError(err);
  return el("div", { class: "warn-banner load-error", role: "alert" }, `${what}: ${d.title}`);
}

/**
 * Laedt einen Teil einer Sicht und haelt einen Fehler fest, statt ihn zu
 * verschlucken.
 *
 * Die gemeinsame Grundlage fuer „nicht geladen ist nicht leer“: Eine Sicht,
 * die mehrere Abrufe zusammensetzt (Kacheln, Zaehler, Panels), rechnet mit
 * ``value`` weiter, zeigt aber fuer jeden Teil mit ``ok === false`` den
 * Fehlerzustand (``loadErrorState``) bzw. „–“ (``loadedValue``) statt „0“,
 * „keine …“ oder „✓“. Frueher fing jeder Abruf seinen Fehler mit einem leeren
 * Ersatzwert ab, und bei einem Serverausfall behaupteten die Kacheln einen
 * leeren Bestand.
 *
 * @param {function(): Promise<*>} load der Abruf (etwa ``() => api.get(p)``).
 * @param {*} fallback Ersatzwert, mit dem die Sicht nach einem Fehler weiter
 *   rechnet (leere Liste, ``null``); er darf nie als Bestand erscheinen.
 * @returns {Promise<{ok: boolean, value: *, error: *}>} ``ok`` und der Wert
 *   oder ``ok: false``, ``value: fallback`` und der Fehler. Wirft nie.
 */
async function loadPart(load, fallback) {
  try { return { ok: true, value: await load(), error: null }; }
  catch (error) { return { ok: false, value: fallback, error }; }
}

/**
 * Anzeigewert einer Kachel oder eines Zaehlers: „–“, solange die Grundlage
 * nicht geladen ist -- eine „0“ waere eine Behauptung ueber den Bestand.
 *
 * @param {boolean} loaded ob alle Daten, aus denen ``value`` stammt, geladen sind.
 * @param {*} value der gerechnete Wert.
 * @returns {*} ``value`` oder „–“.
 */
function loadedValue(loaded, value) {
  return loaded ? value : "\u2013";
}

/**
 * Banner ueber jeder Sicht, solange der Server nicht erreichbar ist: Die
 * gezeigten Daten koennen veraltet sein (auch zwischengespeicherte, etwa der
 * geladene Vorgang in der Ausfuehrung).
 *
 * @returns {HTMLElement} das Banner.
 */
function outageBanner() {
  return el("div", { class: "warn-banner outage-banner", role: "alert" },
    findingText({ code: "NET.unreachable", params: {} }, { withHint: true })
      + " Die angezeigten Daten können veraltet sein.");
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
 * Das erste Feld in ``scope``, das den Fokus annimmt und eine Eingabe erlaubt.
 *
 * Uebersprungen werden gesperrte (``disabled``), schreibgeschuetzte
 * (``readonly``) und versteckte (``type="hidden"``) Felder. Ein gesperrtes
 * Feld nimmt ``focus()`` stillschweigend nicht an -- der Fokus blieb dann auf
 * ``<body>``, und Escape erreichte den Dialog nicht. So beginnt etwa eine
 * Aufgabenmaske, deren erstes Feld nur gelesen wird (die
 * Entscheidungsgrundlage), im ersten Feld, das die Person ausfuellen kann.
 *
 * @param {Element} scope der Dialog (oder ein anderer Bereich).
 * @returns {Element|null} das Feld, oder ``null``, wenn es keines gibt (dann
 *   entscheidet der Aufrufer, etwa „Bestaetigen“).
 */
function firstEditableField(scope) {
  return [...scope.querySelectorAll("input, select, textarea")].find((x) =>
    !x.disabled && !x.hasAttribute("disabled")
    && !x.readOnly && !x.hasAttribute("readonly")
    && String(x.getAttribute("type") || "").toLowerCase() !== "hidden") || null;
}

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
  // dessen Ausloeser, nicht der verschwindende Knopf darin. Hat die
  // Doppelklick-Sperre dem Ausloeser den Fokus entzogen, waehrend er vor dem
  // Dialog noch lud („Erledigen“), zaehlt er trotzdem (``focusOrigin``) --
  // sonst bekam ``<body>`` den Fokus zurueck.
  // Ersetzt wird, wenn schon ein Dialog offen ist -- nicht erst, wenn der
  // Fokus noch darin liegt: Die Doppelklick-Sperre entzieht dem Bestaetigen-
  // Knopf des ersten Dialogs den Fokus, bevor dessen Aktion (etwa nach einer
  // Ablehnung des Kerns) den naechsten Dialog oeffnet. Dann zaehlte der
  // verschwindende Knopf als Ausloeser, und Schliessen gab ``<body>`` den Fokus.
  const replacing = root.children.length > 0 || root.contains(document.activeElement);
  const opener = replacing ? openModal._opener : focusOrigin();
  openModal._opener = opener;
  // Kennung des Ausloesers im Inhalt: Zeichnet eine Laufzeit-Sicht hinter dem
  // Dialog neu, ist der Ausloeser beim Schliessen weg; dann bekommt sein
  // Gegenstueck im neuen Inhalt den Fokus (statt <body>).
  const openerKey = replacing ? dialogOpenerKey : focusKeyOf(opener, byId("content"));
  dialogOpenerKey = openerKey;
  clear(root);
  const close = () => {
    clear(root);
    dialogOpenerKey = null;
    // Sperre sofort aufheben: Der Beobachter meldet sich erst asynchron, und in
    // eine noch ``inert`` gesetzte App laesst sich kein Fokus zurueckgeben.
    syncInert();
    // Ein noch gesperrter Ausloeser nimmt keinen Fokus an; die Sperre gibt
    // ihn nach ihrer Aktion selbst zurueck (``guardAsyncClick``).
    if (opener && opener.isConnected && typeof opener.focus === "function") {
      try { opener.focus(); } catch (_e) { /* nur Komfort */ }
    } else if (openerKey && keepsViewState()) {
      restoreFocusKey(openerKey, byId("content"));
    } else if (openerKey) {
      // Modellier-/Verwaltungssicht: Der Ausloeser im Inhalt ist weg (etwa
      // „Freigeben“, ersetzt durch „Neue Revision“) -- Inhalt statt <body>.
      focusContentFallback();
    } else if (opener && !opener.isConnected) {
      // Ausloeser ausserhalb des Inhalts, der beim Neuzeichnen verschwand --
      // etwa „+ Neu“, „Aus Vorlage“, „BPMN-Import“ in der Kopfleiste nach der
      // Anlage: Das neue Modell steht im Inhalt, also dorthin statt <body>.
      focusContentFallback();
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
  // Fuer Hilfsmittel ein modaler Dialog mit seiner Ueberschrift als Namen.
  const titleId = nextDomId("modal-title");
  const modal = el("div", { class: "modal-backdrop",
      onClick: (e) => {
        if (!withCancel || e.target !== e.currentTarget) return;
        if (edited) {
          keepHint.textContent = "Ungespeicherte Eingaben \u2013 zum Verwerfen \u201EAbbrechen\u201C oder Esc.";
          // Der Klick hat den Fokus aus dem Dialog genommen; ohne ihn erreichte
          // Esc den Dialog nicht mehr (sein Tastenweg haengt am Dialog).
          const back = lastFocus && modal.contains(lastFocus) ? lastFocus
            : firstEditableField(modal) || confirmBtn;
          if (back && typeof back.focus === "function") back.focus();
          return;
        }
        close();
      } },
    el("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
      el("div", { class: "modal-h" }, el("h3", { id: titleId }, title)),
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
  // Nur ein Feld, das den Fokus annimmt: Ein gesperrtes erstes Feld (Lesefeld
  // einer Maske) liess ihn sonst auf <body> (firstEditableField).
  const firstInput = firstEditableField(modal);
  // Mit Eingabefeldern (etwa Pflicht-Anlass) beginnt man dort; ohne Felder
  // landet der Fokus auf „Abbrechen“.
  if (o.danger && cancelBtn && !firstInput) cancelBtn.focus();
  else (firstInput || confirmBtn).focus();
  // Rueckgabe fuer Dialoge, die ihren Bestaetigen-Knopf je nach Eingabe
  // sperren (z. B. der Einfuege-Dialog ohne Diskriminator). Bisherige Aufrufer
  // ignorieren sie.
  return { modal, confirmBtn, close };
}

