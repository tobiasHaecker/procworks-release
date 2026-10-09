// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks - schlanker Web-Client (Roadmap-Schritt 13, Abschnitt 8).
 *
 * Die GUI ist ein reiner Client der headless FastAPI: sie sammelt Intentionen
 * und rendert Zustand. Jede Korrektheitsentscheidung (K/D/Z/A/C/H/F/R/M)
 * trifft ausschliesslich der Kern - hier liegt keine Validierungslogik.
 */

// --------------------------------------------------------------------------
// Navigation + Render-Dispatch
// --------------------------------------------------------------------------

const VIEW_META = {
  model: { title: "Modellieren", sub: "Gef\u00FChrte +-Operationen, live-validiert", fn: viewModel },
  data: { title: "Datensicht", sub: "Datenelemente + Lese/Schreib-Bindung (D/C)", fn: viewData },
  org: { title: "Ressourcensicht", sub: "Organisationsmodell + Bearbeiterregeln (Z/A)", fn: viewOrg },
  run: { title: "Ausf\u00FChrung", sub: "Instanzen starten und Arbeitsliste abarbeiten", fn: viewRun },
  tasks: { title: "Meine Aufgaben", sub: "Deine offenen Aufgaben \u00FCber alle Prozesse, inklusive Vertretung", fn: viewTasks },
  testrun: { title: "Pr\u00FCfinstanz", sub: "Test-Instanz eines Entwurfs im 4-Quadranten-Cockpit durchspielen", fn: viewTestRun },
  monitor: { title: "Monitoring", sub: "Live-Status aktiver Instanzen", fn: viewMonitor },
  integration: { title: "Integration", sub: "Connectoren, Datenanbindung, Automatik & Webhooks", fn: viewIntegration },
  admin: { title: "Administration", sub: "Betrieb: Datensicherung, Wartung & Beispieldaten", fn: viewAdmin },
  help: { title: "Hilfe", sub: "Sichten, Schnellstart je Rolle & Glossar der Regel-Codes", fn: viewHelp },
};

function setActiveNav() {
  [...byId("nav").children].forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
}

// --- Auth / Login ----------------------------------------------------------

// German labels for the coarse RBAC roles (technical ids stay English).
const ROLE_LABELS = { admin: "Administrator", modeler: "Modellierer", operator: "Bearbeiter", viewer: "Leser" };

// Which roles may see each navigation view. In open dev mode the principal
// holds every role, so the full UI stays visible exactly as before.
const VIEW_ROLES = {
  model: ["modeler", "admin"],
  data: ["modeler", "admin"],
  org: ["modeler", "admin"],
  run: ["operator", "modeler", "admin"],
  tasks: ["operator", "modeler", "admin"],
  testrun: ["modeler", "admin"],
  monitor: ["viewer", "operator", "modeler", "admin"],
  integration: ["modeler", "admin"],
  admin: ["admin"],
  help: ["viewer", "operator", "modeler", "admin"],
};

function currentRoles() {
  return (state.principal && state.principal.roles) || [];
}

function hasRole(...allowed) {
  const roles = currentRoles();
  return allowed.some((r) => roles.includes(r));
}

// Fetch the verified identity from the API (/auth/me). On 401 the token is
// invalid; we drop it and fall back to anonymous so the UI stays usable.
// Gehoeren die gemerkten Sitzungsdaten einem anderen Login (SESSION_OWNER_KEY)
// – Wechsel nach abgelaufener Sitzung, Neuladen nach fremder Anmeldung,
// Rollenwechsel der Demo –, werden sie verworfen (resetSessionState). Derselbe
// Login behaelt sie, damit ein Neuladen die Arbeit nicht verliert.
async function loadPrincipal() {
  const before = state.principal && state.principal.subject;
  try {
    state.principal = await api.get("/auth/me");
    // Anderer Login: Meldungen der vorigen Person gehoeren nicht hierher.
    if (before !== undefined && before !== (state.principal && state.principal.subject)) clearToasts();
    const subject = (state.principal && state.principal.subject) || "";
    const owner = storageGet(localStorage, SESSION_OWNER_KEY);
    if (owner !== subject) {
      // Kein Eintrag (null) ist ebenfalls fremd: Er fehlt nach dem Abmelden
      // (dann ist ohnehin alles leer) und bei Altbestand aus der Zeit vor der
      // Bindung, dessen Herkunft unbekannt ist.
      resetSessionState();
      storageSet(localStorage, SESSION_OWNER_KEY, subject);
    }
  } catch (err) {
    state.principal = null;
    // 401: Diese Anmeldung gilt nicht (Token geleert, ungueltig, abgelaufen).
    // Dann gehoert der gemerkte Stand niemandem mehr, der hier sitzt – sonst
    // zeichnete etwa die Ausfuehrung den Vorgang der vorigen Person weiter.
    // Ein Netzfehler (kein status) laesst ihn stehen: Die Person ist dieselbe.
    if (err && err.status === 401) {
      resetSessionState();
      try { localStorage.removeItem(SESSION_OWNER_KEY); } catch (_e) { /* nur Komfort */ }
    }
    if (err && err.status === 401 && state.token && !demoRecovering) {
      toast("err", "Anmeldung fehlgeschlagen", ["Token ung\u00FCltig \u2013 bitte erneut anmelden."]);
    }
  }
  renderUser();
  applyRoleNav();
}

// Ask the API which login UI to present (open/token/password). In password mode
// the SPA gates the whole app behind a login screen; the manual token field is
// hidden because the server issues session tokens via /auth/login.
async function loadAuthConfig() {
  try {
    const cfg = await api.get("/auth/config");
    state.authMode = cfg.mode || "open";
    state.passwordLogin = !!cfg.password_login;
    // Demo-only conveniences (absent/false outside PROCWORKS_DEMO_MODE).
    state.demo = !!cfg.demo;
    state.demoPassword = cfg.demo_password || "";
    state.demoAutologin = cfg.demo_autologin || "";
    state.demoLogins = Array.isArray(cfg.demo_logins) ? cfg.demo_logins : [];
    state.demoFeedbackUrl = cfg.demo_feedback_url || "";
    // OIDC-Redirect-Login (JWT-Modus, Opt-in per Server-Konfiguration):
    // nur wenn alle drei Angaben da sind, bietet die SPA den Firmen-Login an.
    state.oidc = (cfg.mode === "jwt" && cfg.oidc_authorize_url && cfg.oidc_token_url && cfg.oidc_client_id)
      ? { authorizeUrl: cfg.oidc_authorize_url, tokenUrl: cfg.oidc_token_url,
          clientId: cfg.oidc_client_id, scopes: cfg.oidc_scopes || "openid profile email" }
      : null;
  } catch (_e) {
    state.authMode = "open";
    state.passwordLogin = false;
    state.demo = false;
    state.demoPassword = "";
    state.demoAutologin = "";
    state.demoLogins = [];
    state.demoFeedbackUrl = "";
    state.oidc = null;
  }
  const tokenField = byId("token-field");
  if (tokenField) tokenField.style.display = state.passwordLogin ? "none" : "";
}

// --- OIDC-Redirect-Login (Opt-in) ------------------------------------------
// Authorization Code + PKCE, komplett im Client: der Kern rendert weiterhin
// keinen IdP-spezifischen Fluss, er reicht nur die konfigurierten Endpunkte
// über /auth/config durch. Ohne Konfiguration bleibt das Token-Feld der
// dokumentierte Weg. PKCE (S256) statt Client-Secret: die SPA ist ein
// öffentlicher Client; Verifier und State leben nur im sessionStorage des
// laufenden Anmeldevorgangs.

function _oidcRandom() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return _b64url(bytes);
}

function _b64url(bytes) {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Die eigene Rücksprung-Adresse: Origin + Pfad ohne Query/Fragment. Muss beim
// IdP als Redirect-URI des Clients registriert sein.
function _oidcRedirectUri() {
  return location.origin + location.pathname;
}

async function startOidcLogin() {
  if (!state.oidc) return;
  const verifier = _oidcRandom();
  const stateVal = _oidcRandom();
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = _b64url(new Uint8Array(digest));
  sessionStorage.setItem("oidcVerifier", verifier);
  sessionStorage.setItem("oidcState", stateVal);
  const q = new URLSearchParams({
    response_type: "code",
    client_id: state.oidc.clientId,
    redirect_uri: _oidcRedirectUri(),
    scope: state.oidc.scopes,
    state: stateVal,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  location.assign(state.oidc.authorizeUrl + (state.oidc.authorizeUrl.includes("?") ? "&" : "?") + q.toString());
}

// Rücksprung vom IdP: ?code=…&state=… gegen den Verifier einlösen. Liefert
// true, wenn ein Token übernommen wurde; räumt Query-Parameter und Merker in
// jedem Fall (auch ein abgebrochener Versuch hinterlässt keinen Zustand).
async function completeOidcLogin() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  const returnedState = params.get("state");
  if (!code || !state.oidc) return false;
  const verifier = sessionStorage.getItem("oidcVerifier");
  const expected = sessionStorage.getItem("oidcState");
  sessionStorage.removeItem("oidcVerifier");
  sessionStorage.removeItem("oidcState");
  history.replaceState(null, "", _oidcRedirectUri());
  if (!verifier || !expected || returnedState !== expected) {
    toast("err", "Anmeldung abgebrochen", ["Der Rücksprung passt nicht zum gestarteten Anmeldevorgang (State-Prüfung)."]);
    return false;
  }
  try {
    const res = await fetch(state.oidc.tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: _oidcRedirectUri(),
        client_id: state.oidc.clientId,
        code_verifier: verifier,
      }).toString(),
    });
    if (!res.ok) throw new Error("token endpoint " + res.status);
    const body = await res.json();
    if (!body.access_token) throw new Error("kein access_token in der Antwort");
    state.token = String(body.access_token);
    localStorage.setItem("authToken", state.token);
    return true;
  } catch (err) {
    toast("err", "Anmeldung fehlgeschlagen", ["Der Identitätsanbieter hat den Code nicht eingelöst.", String(err && err.message || err)]);
    return false;
  }
}

// Vollbild-Anmeldekarte des JWT-Modus mit konfiguriertem Firmen-Login. Der
// manuelle Weg (Token ins Seitenleisten-Feld) bleibt als Ausweich erhalten.
function showOidcLoginOverlay() {
  const card = el("div", { class: "auth-card" },
    authBrand("Anmeldung über Ihren Firmen-Identitätsanbieter."),
    el("button", { class: "btn primary", onClick: () => startOidcLogin() },
      "Über Firmenkonto anmelden"),
    el("p", { class: "auth-hint", style: "margin-top:10px" },
      "Alternativ können Sie ein Zugangs-Token direkt in das Feld „API-Token“ der Seitenleiste eintragen. ",
      el("a", { href: "#", onClick: (e) => { e.preventDefault(); hideOverlay(); } }, "Ohne Anmeldung fortfahren")));
  showOverlay(card);
}

function showOverlay(card) {
  const root = byId("auth-overlay");
  clear(root);
  root.appendChild(card);
  root.style.display = "grid";
}

function hideOverlay() {
  const root = byId("auth-overlay");
  clear(root);
  root.style.display = "none";
}

function authBrand(subtitle) {
  return el("div", { class: "auth-brand" },
    el("div", { class: "logo" }, "CbC"),
    el("div", {},
      el("h2", {}, "ProcWorks"),
      el("div", { class: "auth-hint" }, subtitle)));
}

// Full-screen login: exchange username + password for a session token, store it
// and continue booting. A forced password change is handled right after.
function showLoginOverlay() {
  const errBox = el("div", { class: "auth-err" });
  const loginInput = el("input", { type: "text", id: "login-name", autocomplete: "username", placeholder: "vorname.nachname" });
  const pwInput = el("input", { type: "password", id: "login-pw", autocomplete: "current-password", placeholder: "Passwort" });
  const submit = async (e) => {
    if (e) e.preventDefault();
    errBox.textContent = "";
    try {
      const res = await api.post("/auth/login", {
        login: loginInput.value.trim(),
        password: pwInput.value,
      });
      state.token = res.token;
      localStorage.setItem("authToken", state.token);
      if (res.must_change) {
        showChangePasswordOverlay(true);
      } else {
        hideOverlay();
        await boot();
      }
    } catch (err) {
      // 429: zu viele Fehlversuche (Drosselung im Kern) -- der Kern
      // nennt die Wartezeit, der Text wird unveraendert gezeigt.
      errBox.textContent = err && err.status === 401
        ? "Login oder Passwort ist falsch."
        : err && err.status === 429 && typeof err.detail === "string"
          ? err.detail
          : "Anmeldung fehlgeschlagen.";
    }
  };
  const form = el("form", { onSubmit: submit },
    el("label", { class: "field" }, "Login", loginInput),
    el("label", { class: "field" }, "Passwort", pwInput),
    errBox,
    el("button", { class: "btn primary", type: "submit" }, "Anmelden"));
  const card = el("div", { class: "auth-card" },
    authBrand("Bitte melden Sie sich an."), form,
    el("p", { class: "auth-disclaimer" },
      "Nutzung auf eigenes Risiko. ProcWorks wird ohne jede Gewährleistung und ",
      "ohne jede Haftung bereitgestellt – für keinerlei Schäden an Systemen, ",
      "Daten oder Prozessen. ",
      el("a", {
        href: DISCLAIMER_URL,
        target: "_blank", rel: "noopener",
      }, "Haftungsausschluss")));
  showOverlay(card);
  setTimeout(() => loginInput.focus(), 0);
}

// Forced (first login) or self-service password change. On success we have a
// usable session and boot the app.
function showChangePasswordOverlay(forced) {
  const errBox = el("div", { class: "auth-err" });
  const curInput = el("input", { type: "password", autocomplete: "current-password", placeholder: "Aktuelles Passwort" });
  const newInput = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort (min. 8 Zeichen)" });
  const repInput = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort wiederholen" });
  const submit = async (e) => {
    if (e) e.preventDefault();
    errBox.textContent = "";
    if (newInput.value !== repInput.value) {
      errBox.textContent = "Die Passw\u00F6rter stimmen nicht \u00FCberein.";
      return;
    }
    try {
      await api.post("/auth/change-password", {
        current_password: curInput.value,
        new_password: newInput.value,
      });
      hideOverlay();
      toast("ok", "Passwort ge\u00E4ndert", ["Sie sind jetzt angemeldet."]);
      await boot();
    } catch (err) {
      // Der Kern nennt den konkreten Grund (``PW.too-short``/``PW.unchanged``);
      // formuliert wird im Meldungskatalog wie jede andere Absage.
      errBox.textContent = err && err.status === 400
        ? describeError(err).title
        : (err && err.status === 401
          ? "Aktuelles Passwort ist falsch."
          : "\u00C4nderung fehlgeschlagen.");
    }
  };
  const subtitle = forced
    ? "Bitte vergeben Sie ein eigenes Passwort."
    : "Passwort \u00E4ndern.";
  const form = el("form", { onSubmit: submit },
    el("label", { class: "field" }, "Aktuelles Passwort", curInput),
    el("label", { class: "field" }, "Neues Passwort", newInput),
    el("label", { class: "field" }, "Wiederholen", repInput),
    errBox,
    el("button", { class: "btn primary", type: "submit" }, "Speichern"));
  const card = el("div", { class: "auth-card" }, authBrand(subtitle), form);
  showOverlay(card);
  setTimeout(() => curInput.focus(), 0);
}

/**
 * localStorage-Schluessel: Login (``principal.subject``), dem die gemerkten
 * Sitzungsdaten gehoeren (Pruefinstanz-Auswahl u. ae.). Meldet sich jemand
 * anderes an, verwirft ``loadPrincipal`` diese Daten, statt sie zu uebernehmen.
 */
const SESSION_OWNER_KEY = "sessionOwner";

/**
 * localStorage-Schluessel, die an der angemeldeten Person haengen und beim
 * Sitzungswechsel verworfen werden (``resetSessionState``): ``agentId`` ist
 * die in „Meine Aufgaben“ gewaehlte Person – sonst oeffnete die naechste
 * Anmeldung ohne eigene Bearbeiter-Zuordnung direkt deren Liste. Wer einen
 * neuen personenbezogenen Schluessel einfuehrt, traegt ihn hier ein.
 */
const SESSION_STORAGE_KEYS = ["agentId"];

/**
 * Verwirft alles, was an der Anmeldung der vorigen Person haengt.
 *
 * Zweck: Nach einem Abmelden oder Login-Wechsel im selben Browser darf die
 * naechste Person in keiner Sicht Daten der vorigen sehen. Der zuletzt
 * geladene Vorgang stand sonst samt Instanzdaten weiter in der Ausfuehrung,
 * obwohl der Kern ihn der neuen Person verweigert (er liest einzelne Vorgaenge
 * nur fuer Beteiligte).
 *
 * Geleert werden: der geladene Vorgang und seine Arbeitsliste (auch der fuer
 * ein Neuladen gemerkte Vorgang, ``forgetInstance``), die
 * Pruefinstanz-Auswahl (auch ihr localStorage-Spiegel), das
 * Personenverzeichnis, Schema-Liste/-Namen und das geladene Schema samt
 * Validierung und Hinweisen (``boot`` laedt sie mit den Rechten der neuen
 * Person neu), Verbindungsstatus der Konnektoren, Fokus-/Ruecksprung-Marken,
 * die Basis fuer „neue Aufgabe eingetroffen“, offene Dialoge und der
 * gezeichnete Seiteninhalt.
 *
 * Bewusst erhalten: gewaehlte Sicht und gewaehlter Prozess (``view``,
 * ``schemaId``) – sie tragen selbst keine Daten; der Prozess wird beim
 * naechsten ``boot`` mit den Rechten der neuen Person nachgeladen, und eine
 * fuer sie gesperrte Sicht faengt ``applyRoleNav`` ab. Ebenso reine
 * Darstellungswahl (Farbschema, Menue, Modellier-Oberflaeche).
 *
 * Keine Parameter, kein Rueckgabewert; ohne DOM (Tests) laeuft es ebenfalls.
 */
function resetSessionState() {
  state.instanceIds = [];
  state.instanceId = null;
  state.instance = null;
  state.worklist = null;
  state.testInstanceId = null;
  state.testInstance = null;
  state.testStarter = null;
  state.testAgentA = null;
  state.testAgentB = null;
  persistTestState();
  state.simulation = null;
  state.agentDirectory = {};
  state.schemaIds = [];
  state.schemaNames = {};
  state.schemaVersions = {};
  state.schema = null;
  state.validation = null;
  state.hints = [];
  state.migrationReport = null;
  state.connectorStatus = {};
  state.revision = 0;
  state.selectedNode = null;
  state.dataFocusNode = null;
  state.staffFocusNode = null;
  state.returnTo = null;
  state.dataElemFocus = null;
  state.orgFocusUnit = null;
  state.orgFocusAgents = [];
  taskAlert = { agentId: null, keys: new Set(), primed: false };
  forgetInstance();
  // Gemerkte Werte, die an der Person haengen (die Pruefinstanz-Schluessel
  // raeumt persistTestState oben ab). Reine Darstellungswahl bleibt.
  SESSION_STORAGE_KEYS.forEach((k) => {
    try { localStorage.removeItem(k); } catch (_e) { /* nur Komfort */ }
  });
  const content = byId("content");
  if (content) clear(content);
  const modals = byId("modal-root");
  if (modals) clear(modals);
}

/**
 * Nimmt den angezeigten Vorgang aus der Ansicht, wenn der Kern ihn nicht
 * (mehr) herausgibt.
 *
 * Ein 404 bzw. 403 beim Nachladen heisst: Der Vorgang ist geloescht, oder die
 * aktuelle Anmeldung darf ihn nicht lesen. Dann darf der zuvor geladene Stand
 * nicht weiter angezeigt werden – sonst stuende ein fremder Vorgang samt Daten
 * auf dem Schirm, obwohl der Kern ihn verweigert.
 *
 * @param {*} err Fehlerobjekt aus ``request`` (``{status, detail}``) oder etwas anderes.
 * @returns {boolean} true, wenn der Vorgang verworfen und ein Hinweis gezeigt
 *   wurde; false bei jedem anderen Fehler (z. B. Verbindungsabbruch) – dann
 *   bleibt der Stand stehen, weil der Vorgang vermutlich noch existiert.
 */
function dropUnreadableInstance(err) {
  if (!err || (err.status !== 403 && err.status !== 404)) return false;
  forgetInstance();
  state.instanceId = null;
  state.instance = null;
  state.worklist = null;
  toast("info", "Vorgang nicht mehr verfügbar",
    ["Er wurde gelöscht, oder diese Anmeldung darf ihn nicht einsehen."]);
  return true;
}

// End the session server-side, drop the local token and return to the login.
// Alles Sitzungsbezogene wird verworfen (resetSessionState) – die naechste
// Person am selben Browser sieht nichts von dieser Sitzung.
async function logout() {
  try {
    await api.post("/auth/logout");
  } catch (_e) {
    // ignore: the token is dropped locally regardless.
  }
  state.token = "";
  state.principal = null;
  clearToasts();  // nichts von dieser Sitzung bleibt fuer die naechste stehen
  resetSessionState();
  localStorage.removeItem("authToken");
  try { localStorage.removeItem(SESSION_OWNER_KEY); } catch (_e) { /* nur Komfort */ }
  if (state.passwordLogin) showLoginOverlay();
  else await boot();
}

// --- Public-demo login conveniences (demo mode only) ----------------------

// Log in as a seeded demo user by exchanging the advertised demo credentials
// for a session token (via the normal /auth/login path -- no auth bypass).
// Returns true on success. Used for the silent auto-login and role switching.
// Demo users skip the forced first-change, so no password-change step follows.
/** sessionStorage: zuletzt benutzte Demo-Person (fuer die Wiederanmeldung). */
const DEMO_LAST_LOGIN_KEY = "demoLastLogin";

async function demoLoginAs(login) {
  try {
    const res = await api.post("/auth/login", { login, password: state.demoPassword });
    storageSet(sessionStorage, DEMO_LAST_LOGIN_KEY, login);
    state.token = res.token;
    localStorage.setItem("authToken", state.token);
    return true;
  } catch (_e) {
    return false;
  }
}

// One-click role switch from the demo banner: re-authenticate as another seeded
// demo user and reboot the app so nav/permissions reflect the new role.
async function switchDemoRole(login) {
  if (login === (state.principal && state.principal.subject)) return;
  const ok = await demoLoginAs(login);
  if (ok) {
    // Wer die Leiste geschlossen hatte, sieht sie nach dem Wechsel nur
    // eingeklappt wieder -- als Hinweis auf die neue Rolle, nicht als volle
    // Leiste, die sich jedes Mal neu aufdraengt.
    if (sessionStorage.getItem("demoBannerDismissed") === "1") {
      storageSet(localStorage, DEMO_BANNER_COLLAPSED_KEY, "1");
    }
    sessionStorage.removeItem("demoBannerDismissed");
    hideOverlay();
    await boot();
  } else {
    toast("err", "Rollenwechsel fehlgeschlagen", ["Bitte erneut versuchen."]);
  }
}

// Mount (or refresh) the dismissible demo banner: shows the current demo
// identity and offers one-click switches to the other seeded roles plus the
// shared password. Idempotent -- re-created on each boot so it reflects the
// active role; stays hidden once dismissed for the session.
// Derive the demo's own trial id from its hostname (trial-<id>.fly.dev). Sent
// with the survey so the operator can match feedback to the earlier lead mail
// from the same session (both carry the id). Empty when not on a trial host.
function currentTrialId() {
  const m = /^trial-([0-9a-f]+)\./.exec(window.location.hostname);
  return m ? m[1] : "";
}

// A 1..5 radio scale as one row of options; read the pick with
// ``node.querySelector('input:checked')`` after submit.
function scaleField(name) {
  return el("div", { class: "survey-scale" },
    ...[1, 2, 3, 4, 5].map((i) =>
      el("label", { class: "survey-scale-opt" },
        el("input", { type: "radio", name, value: String(i) }),
        el("span", {}, String(i)))));
}

// The post-demo, 2-minute survey ("Demo beenden"). Purely a demo feature: it is
// only reachable when the server advertised a broker feedback URL. The answers
// are POSTed cross-origin to the broker (which relays them by e-mail and stores
// nothing); delivery is best-effort, so the visitor is always thanked, even if
// the POST fails. Nothing here touches the correctness core.
// sessionStorage key: set as soon as the survey has been shown once (by the
// "Demo beenden" button or by the exit-intent hook below), so a visitor is
// asked at most once per demo session and never nagged.
const SURVEY_OFFERED_KEY = "demoSurveyOffered";

function endDemoSurvey() {
  if (!state.demoFeedbackUrl) return;
  sessionStorage.setItem(SURVEY_OFFERED_KEY, "1");
  const roleSel = el("select", { class: "input" },
    el("option", { value: "" }, "– bitte wählen –"),
    ...["Fachbereich / Prozessverantwortung", "IT / Entwicklung", "Beratung / Consulting",
      "Management / Geschäftsführung", "Lehre / Studium", "Sonstiges"]
      .map((r) => el("option", { value: r }, r)));
  const intentSel = el("select", { class: "input" },
    el("option", { value: "" }, "– bitte wählen –"),
    ...["Ja, konkret", "Ja, perspektivisch", "Eher nicht", "Nein"]
      .map((v) => el("option", { value: v }, v)));
  const comment = el("textarea", { class: "input", rows: "3", maxlength: "2000",
    placeholder: "Was hat gefehlt, was war unklar, was würdest du verbessern?" });

  const field = (label, node) => el("label", { class: "survey-field" },
    el("span", { class: "survey-q" }, label), node);
  const body = el("div", { class: "survey" },
    field("Was beschreibt dich am besten?", roleSel),
    field("Wie zufrieden bist du mit ProcWorks insgesamt? (1 = gar nicht, 5 = sehr)",
      scaleField("satisfaction")),
    field("ProcWorks lässt per Konstruktion keine fehlerhaften Prozessmodelle zu. "
      + "Wie wichtig ist dir das? (1–5)", scaleField("cbc")),
    field("Wie leicht fiel dir das Modellieren? (1–5)", scaleField("ease")),
    field("Könntest du dir vorstellen, ProcWorks einzusetzen?", intentSel),
    field("Was würdest du verbessern? (optional)", comment));

  const scale = (name) => {
    const hit = body.querySelector(`input[name="${name}"]:checked`);
    return hit ? Number(hit.value) : null;
  };
  openModal("Demo beenden – 2-Minuten-Feedback", body, async () => {
    const payload = {
      trial_id: currentTrialId(),
      role: roleSel.value,
      satisfaction: scale("satisfaction"),
      cbc_importance: scale("cbc"),
      ease: scale("ease"),
      intent: intentSel.value,
      comment: comment.value,
    };
    try {
      await fetch(state.demoFeedbackUrl, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (_e) { /* best-effort: thank the visitor regardless */ }
    toast("success", "Danke für dein Feedback!",
      ["Es hilft uns, ProcWorks zu verbessern. Du kannst den Tab jetzt schließen."]);
    return true;
  }, "Absenden & beenden");
}

// Minimum time on the page before the exit-intent survey may appear. A visitor
// who tabs away in the first minute has not seen enough to answer, and being
// asked immediately reads as nagging.
const SURVEY_MIN_DWELL_MS = 60000;

// Offer the survey when the visitor *leaves* the tab, not only when they use
// the "Demo beenden" button -- closing the window is otherwise the one exit
// that yields no feedback at all.
//
// Deliberately hooked to ``visibilitychange`` rather than ``beforeunload``:
// browsers forbid arbitrary UI (and any reliable async work) during unload, so
// a survey shown there would either be suppressed or lose the answers. Here the
// modal is opened while the tab is hidden and is simply *waiting* when the
// visitor comes back. Someone who never returns is unreachable either way --
// that case is a mail, which we deliberately do not send (contact data lives
// only for the duration of the lead request, never stored).
//
// Guarded so it fires at most once, only in the public demo (a feedback URL is
// configured), only after a real dwell time, and never on top of an open modal
// or a half-filled form (``userIsBusy``).
function installExitIntentSurvey() {
  if (!state.demoFeedbackUrl) return;
  if (sessionStorage.getItem(SURVEY_OFFERED_KEY) === "1") return;
  const armedAt = Date.now();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") return;
    if (sessionStorage.getItem(SURVEY_OFFERED_KEY) === "1") return;
    if (Date.now() - armedAt < SURVEY_MIN_DWELL_MS) return;
    if (userIsBusy()) return;
    if (typeof Tour !== "undefined" && Tour.running) return;   // Tour laeuft
    endDemoSurvey();   // marks the session as offered
  });
}

/** localStorage-Schluessel: Demo-Banner eingeklappt (ueberlebt Neuladen). */
const DEMO_BANNER_COLLAPSED_KEY = "demoBannerCollapsed";

/** Liest einen Speicherwert fehlertolerant (privater Modus -> null). */
function storageGet(store, key) {
  try { return store.getItem(key); } catch (e) { return null; }
}
/** Schreibt einen Speicherwert fehlertolerant (privater Modus -> ignoriert). */
function storageSet(store, key, value) {
  try { store.setItem(key, value); } catch (e) { /* nur Komfort */ }
}

/**
 * Reserviert unten Platz fuer den Demo-Banner, statt ihn ueber die App zu legen.
 *
 * Der Banner ist fest positioniert. Frueher lag er ueber allem -- auch ueber dem
 * unteren Teil der Schritt-Karte, ueber Dialogen und Toasts. Jetzt meldet er seine Hoehe als CSS-Variable `--demo-banner-h`;
 * `.app` wird um genau diese Hoehe kuerzer und die Toasts sitzen darueber.
 * Ein ResizeObserver haelt den Wert beim Umbrechen (schmales Fenster) aktuell.
 * @param {HTMLElement|null} banner der Banner oder null (entfernt)
 */
function reserveDemoBannerSpace(banner) {
  const root = document.documentElement;
  if (reserveDemoBannerSpace._obs) { reserveDemoBannerSpace._obs.disconnect(); reserveDemoBannerSpace._obs = null; }
  const apply = () => {
    const h = banner && banner.isConnected ? Math.ceil(banner.getBoundingClientRect().height) + 20 : 0;
    root.style.setProperty("--demo-banner-h", h + "px");
  };
  apply();
  if (banner && typeof ResizeObserver !== "undefined") {
    reserveDemoBannerSpace._obs = new ResizeObserver(apply);
    reserveDemoBannerSpace._obs.observe(banner);
  }
}

function mountDemoBanner() {
  const old = byId("demo-banner");
  if (old) old.remove();
  if (sessionStorage.getItem("demoBannerDismissed") === "1") { reserveDemoBannerSpace(null); return; }

  // Eingeklappt: nur ein kleiner Knopf, der den Banner wieder oeffnet.
  if (storageGet(localStorage, DEMO_BANNER_COLLAPSED_KEY) === "1") {
    const pill = el("button", {
      id: "demo-banner", class: "demo-banner demo-banner-collapsed",
      title: "Demo-Hinweis mit Rollenwechsel und Passwort öffnen",
      "aria-label": "Demo-Hinweis öffnen",
      onClick: () => { storageSet(localStorage, DEMO_BANNER_COLLAPSED_KEY, "0"); mountDemoBanner(); },
    }, el("span", { class: "demo-badge" }, "DEMO"), " Rollen & Passwort \u25B4");
    document.body.appendChild(pill);
    reserveDemoBannerSpace(pill);
    return;
  }

  const meLogin = state.principal && state.principal.subject;
  const meName = (state.principal && state.principal.display_name) || meLogin || "–";
  const meRole = ((state.principal && state.principal.roles) || [])
    .map((r) => ROLE_LABELS[r] || r).join(", ") || "ohne Rolle";

  const others = state.demoLogins.filter((u) => u.login !== meLogin);
  const caption = (u) => `${ROLE_LABELS[u.role] || u.role}: ${u.name}`;
  // Auf dem Handy eine Auswahlliste statt eines Knopfs je Rolle: Die
  // 13 Knoepfe brachen auf 390 px in viele Zeilen um, die Leiste wurde so hoch,
  // dass die App (sie macht der Leiste Platz, --demo-banner-h) kaum noch Raum
  // hatte -- und die Tour ihre Blase oben ueber den Menueknopf legen musste.
  const narrow = typeof window !== "undefined" && window.innerWidth <= 720;
  const switches = narrow && others.length
    ? [el("select", {
        class: "demo-switch-select", "aria-label": "Andere Rolle testen",
        onChange: (e) => { if (e.target.value) switchDemoRole(e.target.value); },
      },
        el("option", { value: "" }, "Andere Rolle testen \u2026"),
        ...others.map((u) => el("option", { value: u.login }, caption(u))))]
    : others.map((u) => el("button", {
        class: "demo-switch",
        title: `Als ${u.name} anmelden`,
        onClick: () => switchDemoRole(u.login),
      }, caption(u)));

  const banner = el("div", { id: "demo-banner", class: "demo-banner", role: "region", "aria-label": "Demo-Hinweis" },
    el("div", { class: "demo-banner-main" },
      el("span", { class: "demo-badge" }, "DEMO"),
      el("span", {}, `Angemeldet als ${meName} (${meRole}).`),
      switches.length
        ? el("span", { class: "demo-switch-wrap" },
            narrow ? null : el("span", { class: "demo-switch-label" }, "Andere Rolle testen:"),
            ...switches)
        : null,
      state.demoPassword
        ? el("span", { class: "demo-pw" }, `Passwort: ${state.demoPassword}`)
        : null),
    el("div", { class: "demo-banner-actions" },
      state.demoFeedbackUrl
        ? el("button", {
            class: "demo-end", title: "Demo beenden und kurz Feedback geben",
            onClick: endDemoSurvey,
          }, "Demo beenden")
        : null,
      el("button", {
        class: "demo-banner-close", "aria-label": "Hinweis einklappen", title: "Einklappen",
        onClick: () => { storageSet(localStorage, DEMO_BANNER_COLLAPSED_KEY, "1"); mountDemoBanner(); },
      }, "\u25BE"),
      el("button", {
        class: "demo-banner-close", "aria-label": "Hinweis schließen", title: "Für diese Sitzung schließen",
        onClick: () => { sessionStorage.setItem("demoBannerDismissed", "1"); banner.remove(); reserveDemoBannerSpace(null); },
      }, "×")));

  document.body.appendChild(banner);
  reserveDemoBannerSpace(banner);
}


function renderUser() {
  const pill = byId("user-pill");
  const foot = byId("auth-user");
  const logoutBtn = byId("logout-btn");
  const p = state.principal;
  const bound = p && p.agent_id;
  const roles = (p && p.roles) || [];
  const roleText = roles.map((r) => ROLE_LABELS[r] || r).join(", ");
  const showLogout = state.passwordLogin && !!p;
  if (logoutBtn) logoutBtn.style.display = showLogout ? "" : "none";
  if (!p) {
    pill.textContent = "nicht angemeldet";
    pill.className = "pill pill-gray";
    foot.textContent = "Nicht angemeldet";
    return;
  }
  // Open dev mode: anonymous principal with all roles -> show "offen".
  const open = !bound && roles.length >= 4;
  pill.textContent = open ? "offen" : (p.display_name || p.subject);
  pill.className = "pill " + (open ? "pill-gray" : "pill-green");
  if (showLogout) {
    clear(foot);
    foot.appendChild(el("span", {}, `${p.display_name || p.subject} \u00B7 ${roleText || "ohne Rolle"}`));
    foot.appendChild(document.createTextNode(" \u00B7 "));
    foot.appendChild(el("a", {
      href: "#", onClick: (e) => { e.preventDefault(); showChangePasswordOverlay(false); },
    }, "Passwort \u00E4ndern"));
    return;
  }
  foot.textContent = open
    ? "Modus: offen (kein Login)"
    : `${p.display_name || p.subject} \u00B7 ${roleText || "ohne Rolle"}`;
}

// Hide nav entries the current role may not use and keep the active view valid.
function applyRoleNav() {
  const buttons = [...byId("nav").children];
  buttons.forEach((b) => {
    const allowed = VIEW_ROLES[b.dataset.view] || [];
    const visible = hasRole(...allowed);
    b.style.display = visible ? "" : "none";
  });
  const allowedNow = hasRole(...(VIEW_ROLES[state.view] || []));
  if (!allowedNow) {
    const first = buttons.find((b) => b.style.display !== "none");
    if (first) state.view = first.dataset.view;
  }
}

async function setToken(token) {
  state.token = token.trim();
  if (state.token) localStorage.setItem("authToken", state.token);
  else localStorage.removeItem("authToken");
  await boot();
}

// Laeuft gerade eine Sichtfunktion? Und wurde waehrenddessen erneut gerendert?
// Siehe render() -- diese beiden Merker verhindern das Ueberlappen zweier
// Renderlaeufe.
let renderBusy = false;
let renderQueued = false;
// Nur wenn ALLE vorgemerkten Laeufe die Rollposition behalten wollen
// (Live-Aktualisierung), behaelt sie auch der nachgeholte Lauf.
let renderQueuedKeepScroll = true;

/**
 * Hat die Person im Inhaltsbereich etwas eingegeben, das noch nicht
 * abgeschickt ist? Gesetzt von ``markContentDirty`` (input/change an einem
 * Formularfeld in ``#content``), geloescht bei jedem Neuzeichnen der Sicht –
 * danach steht ohnehin ein frisches Formular da. Solange es gesetzt ist,
 * zeichnet die automatische Aktualisierung nicht neu (``userIsBusy``); sie
 * bietet stattdessen „Aktualisieren“ an (``showLiveRefreshHint``).
 */
let contentDirty = false;

/**
 * Zeichnet die aktuelle Sicht neu.
 *
 * @param {{keepScroll?: boolean}} [opts] ``keepScroll: true`` (automatische
 *   Aktualisierung): Rollposition des Inhalts danach wiederherstellen.
 *   Ohne Option (Aktion der Person, Sichtwechsel) beginnt die Sicht oben.
 *
 * **Renderlaeufe ueberlappen sich nie.** Die Sichtfunktionen sind asynchron und
 * folgen alle demselben Muster: erst ``clear(content)``, dann ``await api.get``,
 * dann anhaengen. Starten zwei Laeufe kurz nacheinander, leert der zweite den
 * Inhalt, waehrend der erste noch auf die API wartet -- und danach haengen
 * *beide* ihre Panels an. Sichtbar wurde das als **doppelte Bereiche** in „Meine
 * Aufgaben" (zweimal „Offene Aufgaben", zweimal „Abwesenheit").
 *
 * Ausloeser gibt es auf den Laufzeit-Sichten reichlich, und sie treffen sich
 * genau beim Erledigen einer Aufgabe: der Klick-Callback, der Revisions-Poll
 * (das Erledigen erhoeht die Revision), der Zeit-Tick und -- waehrend des
 * Tutorials -- der Tour-Tick, der den Schritt weiterschaltet. Deshalb fiel es im
 * Tutorial auf; der Fehler ist aber nicht tutorial-spezifisch.
 *
 * Statt jede Sicht einzeln abzusichern, werden die Laeufe hier **zusammen-
 * gefasst**: waehrend einer laeuft, wird ein weiterer nur vorgemerkt und danach
 * genau einmal nachgeholt. Der zuletzt gewuenschte Zustand wird also immer
 * gezeichnet, nur eben nacheinander statt verschraenkt.
 */
function render(opts) {
  const keepScroll = !!(opts && opts.keepScroll);
  if (renderBusy) {
    renderQueued = true;
    renderQueuedKeepScroll = renderQueuedKeepScroll && keepScroll;
    return;
  }
  renderBusy = true;
  // Neu gezeichnet = frisches Formular: nichts Ungespeichertes mehr auf dem Schirm.
  contentDirty = false;
  // Live-Aktualisierung: an derselben Stelle bleiben, statt nach oben zu springen.
  const scroller = keepScroll ? document.querySelector(".main") : null;
  const scrollTop = scroller ? scroller.scrollTop : 0;
  // Guard against a stale/unknown persisted view (e.g. after a rename) so the
  // dispatch below never dereferences an undefined entry.
  if (!VIEW_META[state.view]) state.view = "model";
  // Der Kontrollfluss-Vollbildmodus lebt nur in der Modellieren-Sicht; beim
  // Verlassen zuruecksetzen, damit er nicht als Overlay in anderen Sichten haengt.
  if (state.view !== "model") state.graphMaximized = false;
  // Remember the active view so a page reload restores it instead of always
  // falling back to "Modellieren".
  localStorage.setItem("view", state.view);
  const meta = VIEW_META[state.view];
  byId("view-title").textContent = meta.title;
  byId("view-sub").textContent = meta.sub;
  byId("view-sub").title = meta.sub;   // gekuerzt angezeigt, voll im Tooltip
  renderSchemaPicker();
  setActiveNav();
  renderTourBadge();
  Promise.resolve(meta.fn())
    .catch((err) => { toastError(err); })
    .then(() => { if (scroller) scroller.scrollTop = scrollTop; })
    // Die Sichten bauen ihr DOM bei jedem Rendern komplett neu auf -- der Anker
    // der laufenden Tour existiert danach nicht mehr und muss neu gesucht
    // werden. Gekapselt, damit ein Fehler in der Tour nie die Sicht mitreisst.
    .then(() => { if (typeof Tour !== "undefined") Tour.afterRender(); })
    // Erst hier ist die Sicht fertig aufgebaut. Ein waehrenddessen vorgemerkter
    // Lauf wird jetzt nachgeholt -- ``finally``, damit ein Fehler das Rendern
    // nicht dauerhaft blockiert (renderBusy bliebe sonst true).
    .catch(() => { /* Tour-Fehler: die Sicht steht, weiter geht es trotzdem */ })
    .finally(() => {
      renderBusy = false;
      if (renderQueued) {
        const keep = renderQueuedKeepScroll;
        renderQueued = false;
        renderQueuedKeepScroll = true;
        render({ keepScroll: keep });
      }
    });
}

/**
 * Zeigt in der Topbar das Abzeichen „Tutorial – es wird nichts gespeichert",
 * solange die Tour im schreibfreien Sandkasten laeuft.
 *
 * Ehrlichkeit gegenueber dem Nutzer: Er arbeitet dort auf einem Beispielprozess
 * und mit abgespielten Antworten -- das muss sichtbar sein, nicht versteckt.
 */
function renderTourBadge() {
  const slot = byId("tour-badge-slot");
  if (!slot) return;
  clear(slot);
  if (typeof Tour === "undefined" || !Tour.sandboxed) return;
  slot.appendChild(el("span", {
    class: "tour-badge",
    title: "Im Tutorial verl\u00E4sst kein schreibender Aufruf den Browser.",
  }, "Tutorial – es wird nichts gespeichert"));
}

// --------------------------------------------------------------------------
// Live-Aktualisierung (Auto-Refresh der Laufzeit-Sichten)
// --------------------------------------------------------------------------

// Views that mirror runtime progress and should refresh automatically when an
// activity/instance advances anywhere (e.g. another user completes a task).
// Modelling views are intentionally excluded so editing is never interrupted.
const LIVE_VIEWS = new Set(["run", "tasks", "testrun", "monitor"]);
const LIVE_POLL_MS = 4000;
let livePollBusy = false;

// True while the user is actively interacting (a modal/login overlay is open,
// the focus sits in a form field of the content area, or something was typed
// there and not yet sent -- contentDirty, regardless of focus). Auto-refresh is
// skipped then so it never wipes an open dropdown or a half-filled form.
function userIsBusy() {
  if (byId("modal-root").children.length) return true;
  const overlay = byId("auth-overlay");
  if (overlay && overlay.style.display !== "none") return true;
  if (contentDirty) return true;
  const active = document.activeElement;
  if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) {
    const content = byId("content");
    if (content && content.contains(active)) return true;
  }
  return false;
}

/**
 * Merkt eine Eingabe im Inhaltsbereich als „noch nicht abgeschickt“.
 *
 * Haengt (in ``wireNav``) als input-/change-Lauscher am Dokument. Zaehlt nur
 * Formularfelder in ``#content``; Felder mit ``data-live-safe`` sind
 * ausgenommen – ihr Wert liegt in ``state`` und uebersteht ein Neuzeichnen
 * (Simulation). Ohne diese Marke verwarf die automatische Aktualisierung
 * halb ausgefuellte Formulare, sobald der Fokus das Feld verliess.
 *
 * @param {Event} e das Ereignis
 */
function markContentDirty(e) {
  const t = e && e.target;
  if (!t || !/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;
  if (t.hasAttribute && t.hasAttribute("data-live-safe")) return;
  const content = byId("content");
  if (content && content.contains(t)) contentDirty = true;
}

/**
 * Zeigt oben in der Sicht „Neue Daten – Aktualisieren“, statt die Sicht mit
 * ungespeicherten Eingaben neu zu zeichnen. Einmal je Sicht (kein Stapeln).
 * Der Knopf zeichnet neu – bewusst durch die Person, denn dabei gehen die
 * Eingaben verloren (Tooltip sagt das).
 */
function showLiveRefreshHint() {
  const content = byId("content");
  if (!content || content.querySelector(".live-refresh-hint")) return;
  const hint = el("div", { class: "live-refresh-hint", role: "status" },
    el("span", null, "Neue Daten sind da."),
    el("button", { class: "btn small", type: "button",
      title: "Zeichnet die Ansicht neu \u2013 nicht abgeschickte Eingaben gehen dabei verloren.",
      onClick: async () => {
        if (state.view === "run" && state.instanceId) {
          try { await loadInstance(state.instanceId); } catch (e) { dropUnreadableInstance(e); }
        }
        render({ keepScroll: true });
      } }, "Aktualisieren"));
  content.insertBefore(hint, content.firstChild);
}

// Poll the cheap runtime-event revision; when it changed, refresh the current
// live view so task lists and monitoring follow progress without a manual
// reload. The "run" view caches the loaded instance, so reload it first.
async function pollLiveUpdates() {
  if (livePollBusy) return;
  if (state.passwordLogin && !state.principal) return;
  livePollBusy = true;
  try {
    const res = await api.get("/monitoring/revision");
    const rev = res && typeof res.revision === "number" ? res.revision : 0;
    if (rev === state.revision) return;
    state.revision = rev;
    if (!LIVE_VIEWS.has(state.view)) return;
    if (userIsBusy()) {
      // Ungespeicherte Eingaben (nicht Dialog/Anmeldung): Daten nicht still
      // verschlucken, sondern Neuladen anbieten.
      if (contentDirty && !byId("modal-root").children.length) showLiveRefreshHint();
      return;
    }
    if (state.view === "run" && state.instanceId) {
      // 404/403: geloescht oder nicht (mehr) lesbar -> aus der Ansicht nehmen,
      // nie den alten Stand stehen lassen. Andere Fehler (Netz) behalten ihn.
      try { await loadInstance(state.instanceId); } catch (e) { dropUnreadableInstance(e); }
    }
    render({ keepScroll: true });
  } catch (_e) {
    // Silent: a transient API hiccup must not spam toasts on a background poll.
  } finally {
    livePollBusy = false;
  }
}

// Views whose time-based worklist criticality "walks on" as wall-clock time
// passes, even without a new runtime event (Zeitbasierte-Priorisierung 7).
const TIME_TICK_VIEWS = new Set(["tasks", "testrun", "monitor", "run"]);
const TIME_TICK_MS = 30000;

// Re-render the current time-sensitive view on a slow cadence so the "Fällig"
// countdown and the criticality bands advance without a triggering event. Kept
// separate from the (event-driven) revision poll and skipped while the user is
// busy, so it never disturbs an open form or modal.
function tickTimeViews() {
  if (!TIME_TICK_VIEWS.has(state.view) || userIsBusy()) return;
  if (state.passwordLogin && !state.principal) return;
  render({ keepScroll: true });
}

// Start the background poll exactly once (boot may run repeatedly on re-login).
function startLiveUpdates() {
  if (startLiveUpdates._started) return;
  startLiveUpdates._started = true;
  setInterval(pollLiveUpdates, LIVE_POLL_MS);
  setInterval(tickTimeViews, TIME_TICK_MS);
}

/**
 * Haelt die App hinter einem Dialog oder der Anmeldemaske ``inert``.
 *
 * ``inert`` nimmt die Seite aus der Tab-Reihenfolge und fuer Klicks aus dem
 * Spiel. Ohne das sprang Tab aus der Anmeldemaske in die verdeckte App und
 * lief bei offenem Dialog durch die Liste dahinter. Ein Beobachter statt
 * Aufrufen in openModal/showOverlay: Dialoge werden an mehreren Stellen
 * geschlossen (Abbrechen, Hintergrund, Bestaetigen, Escape), und die Sperre
 * darf nie haengen bleiben.
 */
function watchInert() {
  const modalRoot = byId("modal-root");
  const overlay = byId("auth-overlay");
  if (!modalRoot || !overlay || typeof MutationObserver === "undefined") return;
  const obs = new MutationObserver(syncInert);
  obs.observe(modalRoot, { childList: true });
  obs.observe(overlay, { childList: true, attributes: true, attributeFilter: ["style"] });
  syncInert();
}

/** Setzt ``inert`` auf der App genau dann, wenn Dialog oder Anmeldemaske offen sind. */
function syncInert() {
  const app = document.querySelector(".app");
  const modalRoot = byId("modal-root");
  const overlay = byId("auth-overlay");
  if (!app || !modalRoot || !overlay) return;
  const blocked = modalRoot.childElementCount > 0 || overlay.style.display !== "none";
  if (blocked) app.setAttribute("inert", ""); else app.removeAttribute("inert");
}

function wireNav() {
  // Ungespeicherte Eingaben im Inhalt erkennen (schuetzt sie vor der
  // automatischen Aktualisierung, siehe markContentDirty).
  document.addEventListener("input", markContentDirty, true);
  document.addEventListener("change", markContentDirty, true);
  byId("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (!btn) return;
    state.view = btn.dataset.view;
    // A direct nav click is a fresh intent -- drop any badge-driven highlight
    // and the control-flow return point (it would otherwise be stale).
    state.dataFocusNode = null;
    state.staffFocusNode = null;
    state.orgFocusUnit = null;
    state.orgFocusAgents = [];
    state.returnTo = null;
    // Auf dem Smartphone faehrt die Menue-Schublade nach der Wahl wieder ein,
    // damit sofort der gewaehlte Inhalt (z. B. „Meine Aufgaben") sichtbar ist.
    closeMobileNav();
    render();
  });
  const sideToggle = byId("sidebar-toggle");
  if (sideToggle) sideToggle.addEventListener("click", toggleSidebar);
  // Mobile Menue-Schublade: Hamburger oeffnet/schliesst, Tipp auf den
  // abdunkelnden Hintergrund schliesst.
  const burger = byId("nav-burger");
  if (burger) burger.addEventListener("click", toggleMobileNav);
  const scrim = byId("nav-scrim");
  if (scrim) scrim.addEventListener("click", closeMobileNav);
  // Escape verlaesst das Kontrollfluss-Vollbild, hebt die Auswahl im
  // Kontrollfluss auf (beide Oberflaechen) bzw. schliesst die mobile Menue-Schublade (in dieser
  // Reihenfolge, jeweils nur wenn aktiv). Eingaben in einem Feld bleiben
  // unberuehrt -- sonst raeumte Escape mitten im Tippen die Karte weg.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (isTypingTarget()) return;
    // Steht ein Dialog offen, gehoert die Taste ihm -- die Sicht dahinter
    // duerfte sonst umbauen, waehrend der Dialog noch auf ihr arbeitet.
    const modal = byId("modal-root");
    if (modal && modal.childElementCount) return;
    if (state.graphMaximized) {
      state.graphMaximized = false;
      render();
    } else if (state.view === "model" && state.selectedNode) {
      state.selectedNode = null;
      render();
    } else if (document.documentElement.getAttribute("data-mobile-nav") === "open") {
      closeMobileNav();
    }
  });
  // Tastaturnavigation im Kontrollfluss (Stufe U4): Pfeiltasten
  // bewegen die Auswahl entlang des Spine bzw. zwischen den Zweigen, Enter holt
  // den Fokus in die Schritt-Karte. Gilt in beiden Modellier-Oberflaechen --
  // beide fuehren dieselbe Auswahl (state.selectedNode) und ruecken den
  // gewaehlten Knoten ins Bild.
  document.addEventListener("keydown", (e) => {
    const dir = ARROW_DIRS[e.key];
    if (!dir && e.key !== "Enter") return;
    // Mit Zusatztaste gehoert die Kombination dem Browser (Verlauf, Zeilenende,
    // Textauswahl) -- die wird nicht uebernommen.
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (state.view !== "model") return;
    if (isTypingTarget()) return;
    const modal = byId("modal-root");
    if (modal && modal.childElementCount) return;   // Dialog hat Vorrang
    if (!dir) {
      // Enter nur aus dem Kontrollfluss heraus: liegt der Fokus schon auf einem
      // Bedienelement, ist Enter dessen Ausloeser und darf nicht abgefangen
      // werden.
      const tag = document.activeElement && document.activeElement.tagName;
      if (tag === "BUTTON" || tag === "A") return;
      if (focusStepCard()) e.preventDefault();
      return;
    }
    // Pfeiltasten scrollen sonst die Seite, waehrend die Auswahl wandert.
    if (moveSelection(dir)) e.preventDefault();
  });
  const apiInput = byId("api-base");
  apiInput.value = state.apiBase;
  apiInput.addEventListener("change", async () => {
    // Nur eine Adresse uebernehmen, hinter der wirklich ein ProcWorks-Kern
    // antwortet: Eine verirrte Eingabe („4“) legte sonst still jeden
    // Aufruf lahm und blieb im Browser gespeichert.
    const candidate = apiInput.value.trim() || defaultApiBase();
    if (!(await isProcWorksApi(candidate))) {
      toast("err", "Keine ProcWorks-API unter dieser Adresse",
        [`${candidate} antwortet nicht wie ein ProcWorks-Server. Die bisherige Adresse bleibt: ${state.apiBase}`]);
      apiInput.value = state.apiBase;
      return;
    }
    state.apiBase = candidate;
    localStorage.setItem("apiBase", state.apiBase);
    await boot();
  });
  const tokenInput = byId("auth-token");
  tokenInput.value = state.token;
  tokenInput.addEventListener("change", () => { setToken(tokenInput.value); });
  const logoutBtn = byId("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", () => { logout(); });
  wireTheme();
}

/**
 * Verdrahtet die Farbschema-Knoepfe (Dunkel/Hell) der Sidebar und markiert den
 * aktuell aktiven. Rein clientseitig -- kein API-Aufruf, kein boot().
 */
function wireTheme() {
  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.addEventListener("click", () => setTheme(btn.dataset.themeChoice));
  });
  applyTheme(state.theme); // aktiven Knopf markieren, nun da er im DOM ist
}

async function boot() {
  try {
    const health = await api.get("/health");
    setConnected(true);
    showVersion(health && health.version);
    await loadAuthConfig();
    // OIDC-Rücksprung (JWT-Modus): einen mitgebrachten ?code= sofort gegen
    // ein Token einlösen, bevor irgendeine Gate-Entscheidung fällt.
    if (state.oidc) await completeOidcLogin();
    // JWT-Modus mit konfiguriertem Firmen-Login: ohne Token die Anmeldekarte
    // zeigen (der manuelle Token-Weg bleibt über die Seitenleiste offen).
    if (state.authMode === "jwt" && state.oidc && !state.token) {
      showOidcLoginOverlay();
      return;
    }
    // In password mode an unauthenticated visitor must log in first; the rest
    // of the app stays hidden behind the overlay until /auth/me succeeds.
    if (state.passwordLogin && !state.token) {
      // Public demo: silently auto-login as the modeler so the visitor lands
      // straight in the editor. Attempted once; on failure fall back to the
      // normal login overlay so the demo is never a dead end.
      let autologged = false;
      if (state.demo && state.demoAutologin && !state.demoAutologinTried) {
        state.demoAutologinTried = true;
        autologged = await demoLoginAs(state.demoAutologin);
      }
      if (!autologged) {
        showLoginOverlay();
        return;
      }
    }
    await loadPrincipal();
    if (state.passwordLogin && !state.principal) {
      // Demo mit abgelaufener Sitzung: Der 401 von /auth/me hat die
      // Wiederanmeldung schon angestossen (request); sie ruft boot() erneut.
      // Keine Anmeldemaske dazwischen.
      if (demoRecovering) return;
      showLoginOverlay();
      return;
    }
    hideOverlay();
    if (state.demo) mountDemoBanner();
    await loadSchemas();
    await refreshSchema();
    // Nach einem Neuladen beim zuletzt gewaehlten Vorgang bleiben (nur
    // derselbe Login, nur dieser Tab -- siehe rememberInstance).
    await restoreInstance();
    // Personenverzeichnis ueber alle Organisationen: Namen in Aufgaben-,
    // Audit- und Monitoring-Sichten sollen nicht davon abhaengen, welcher
    // Prozess oben gewaehlt ist ("Meine Aufgaben" zeigte sonst interne IDs).
    await loadAgentDirectory();
    // Baseline the live-update revision to "now" so the first poll only fires on
    // genuinely new progress, then start the background auto-refresh.
    try {
      const res = await api.get("/monitoring/revision");
      state.revision = res && typeof res.revision === "number" ? res.revision : 0;
    } catch (_e) { /* keep current baseline */ }
    startLiveUpdates();
    // Erstkontakt: die zur Rolle passende gefuehrte Tour anbieten, sofern sie
    // noch nicht erledigt oder dreimal verschoben wurde. In der oeffentlichen
    // Demo greift derselbe Pfad, weil sich der Besucher dort automatisch
    // anmeldet.
    if (typeof Tour !== "undefined") Tour.maybeOffer();
    // Nur in der oeffentlichen Demo wirksam: Umfrage auch beim Verlassen des
    // Tabs anbieten, nicht erst am "Demo beenden"-Knopf.
    installExitIntentSurvey();
  } catch (err) {
    setConnected(false);
    toastError(err);
  }
  render();
}

wireNav();
watchInert();
boot();
