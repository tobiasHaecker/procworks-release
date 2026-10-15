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

/**
 * Hebt den Navigationseintrag der aktuellen Sicht hervor -- sichtbar
 * (Klasse ``active``) und fuer Screenreader (``aria-current="page"``); alle
 * anderen Eintraege verlieren beides.
 */
function setActiveNav() {
  [...byId("nav").children].forEach((b) => {
    const on = b.dataset.view === state.view;
    b.classList.toggle("active", on);
    if (on) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
}

/**
 * „Zum Inhalt“: setzt den Fokus in den Inhaltsbereich (``#content`` traegt
 * ``tabindex="-1"``). Bewusst per Programm statt ueber den Anker -- die
 * Adresse bleibt unveraendert, ein ``#content`` in der URL stuende sonst bei
 * jedem Neuladen und Teilen mit.
 * @param {Event} e der Klick (Enter auf dem Link)
 */
function skipToContent(e) {
  if (e && typeof e.preventDefault === "function") e.preventDefault();
  const content = byId("content");
  if (content) content.focus();
}

// --- Sicht-Routen (Browser-Verlauf) ----------------------------------------
//
// Jede Hauptsicht steht als Route in der Adresse (``#/tasks``). So wechseln
// Zurueck und Vor des Browsers die Sicht, statt die Anwendung zu verlassen,
// und Neuladen bzw. ein Lesezeichen oeffnen dieselbe Sicht. Die Route traegt
// **nur den Namen der Sicht** -- keine Kennungen von Vorgaengen oder Schemata
// und nie ein Token: Adressen landen in Verlauf, Lesezeichen und Protokollen.
// Was eine Person sehen darf, entscheidet weiter die Rolle (``VIEW_ROLES``,
// ``applyRoleNav``); eine Route auf eine fremde Sicht fuehrt zur Startsicht.

/** Praefix einer Sicht-Route im Fragment der Adresse. */
const ROUTE_PREFIX = "#/";

/**
 * Soll das naechste Abgleichen der Adresse (``syncRoute``) den Eintrag
 * ersetzen statt einen neuen anzulegen? Gesetzt, wenn die Sicht berichtigt
 * wurde (unbekannte Route, fehlendes Recht) -- eine Berichtigung ist kein
 * Schritt, zu dem Zurueck fuehren soll.
 */
let routeReplaceNext = false;

/**
 * Der Sichtname aus einem Adressfragment.
 *
 * @param {string} hash ``location.hash`` (etwa ``"#/tasks"``).
 * @returns {string|null} der Name hinter ``#/`` (auch ein unbekannter --
 *   gepruefte erst ``routeTarget``); ``null``, wenn das Fragment keine Route
 *   ist (leer, ``#content`` des Sprunglinks, ``#`` eines Links).
 */
function routeFromHash(hash) {
  if (typeof hash !== "string" || !hash.startsWith(ROUTE_PREFIX)) return null;
  return hash.slice(ROUTE_PREFIX.length);
}

/**
 * Startsicht der angemeldeten Rolle.
 *
 * Wer Aufgaben bearbeitet, aber nicht modelliert (Bearbeiter), beginnt in
 * „Meine Aufgaben“ -- dort liegt seine Arbeit ueber alle Prozesse; die
 * Ausfuehrung zeigt nur einen Prozess (Entscheidung des Nutzers, QA-Lauf
 * 2026-10). Alle anderen beginnen wie bisher in der ersten Sicht der
 * Menuereihenfolge, die sie sehen duerfen (Modellierer/Admin: Modellieren,
 * Leser: Monitoring). Nur Anzeige -- was erlaubt ist, entscheidet der Kern.
 * @returns {string|null} der Sichtname; ``null`` ohne Anmeldung.
 */
function startViewOfRole() {
  if (hasRole(...VIEW_ROLES.tasks) && !hasRole(...VIEW_ROLES.model)) return "tasks";
  return Object.keys(VIEW_META).find((v) => hasRole(...(VIEW_ROLES[v] || []))) || null;
}

/**
 * Die Sicht, die eine Route tatsaechlich oeffnen darf.
 *
 * Bekannt und erlaubt: diese Sicht. Unbekannt: die gezeigte Sicht, sofern
 * erlaubt, sonst die Startsicht der Rolle. Ohne Recht: die Startsicht der
 * Rolle. Ohne Anmeldung (Start, Anmeldemaske) zaehlt nur „bekannt“ -- das
 * Recht prueft ``applyRoleNav``, sobald die Anmeldung feststeht.
 *
 * @param {string} name Sichtname aus der Route.
 * @returns {string} eine Sicht aus ``VIEW_META``.
 */
function routeTarget(name) {
  const known = (v) => !!v && Object.prototype.hasOwnProperty.call(VIEW_META, v);
  if (!state.principal) return known(name) ? name : (known(state.view) ? state.view : "model");
  const allowed = (v) => known(v) && hasRole(...(VIEW_ROLES[v] || []));
  if (allowed(name)) return name;
  if (!known(name) && allowed(state.view)) return state.view;
  return startViewOfRole() || state.view;
}

/**
 * Uebernimmt die Sicht aus der Adresse (``boot``, vor dem Zeichnen): Neuladen
 * und Lesezeichen oeffnen die Sicht der Route statt der zuletzt gemerkten.
 * Eine unbekannte Route laesst die gemerkte Sicht stehen und wird beim
 * Zeichnen ersetzt. Ohne Route geschieht nichts.
 */
function adoptRouteFromUrl() {
  const name = routeFromHash(location.hash);
  if (name === null) return;
  const view = routeTarget(name);
  if (view !== name) routeReplaceNext = true;
  state.view = view;
}

/**
 * Traegt die gezeigte Sicht in die Adresse ein (``render``).
 *
 * Neuer Verlaufseintrag nur, wenn die Adresse schon eine gueltige Route einer
 * anderen Sicht traegt -- also bei einem echten Sichtwechsel. Ersetzt wird
 * beim ersten Zeichnen (noch keine Route), nach einer Berichtigung
 * (``routeReplaceNext``) und waehrend der gefuehrten Tour (ihre Schritte
 * sollen Zurueck nicht fuellen). Ohne Verlaufs-API (sehr alter Browser) ein
 * No-op.
 */
function syncRoute() {
  const replace = routeReplaceNext;
  routeReplaceNext = false;
  if (typeof history === "undefined" || typeof history.replaceState !== "function") return;
  const target = ROUTE_PREFIX + state.view;
  if (location.hash === target) return;
  const current = routeFromHash(location.hash);
  const tour = typeof Tour !== "undefined" && Tour.running;
  const push = !replace && !tour && current !== null
    && Object.prototype.hasOwnProperty.call(VIEW_META, current);
  try {
    if (push) history.pushState(null, "", target);
    else history.replaceState(null, "", target);
  } catch (_e) { /* Adresse ist Komfort; die Sicht steht trotzdem */ }
}

/**
 * Folgt einer geaenderten Adresse (``popstate``/``hashchange``: Zurueck, Vor,
 * von Hand geaendert): oeffnet die Sicht der Route.
 *
 * Kein Routen-Fragment: nichts tun. Dieselbe Sicht: nur die Adresse
 * berichtigen. Steht ein Dialog offen, bleibt die Sicht (der Dialog arbeitet
 * noch auf ihr) und die Adresse zeigt wieder auf sie. Vor der Anmeldung bzw.
 * waehrend des Starts im Ausfall wird die Sicht nur vorgemerkt. Wie ein Klick
 * ins Menue verwirft der Wechsel Hervorhebungen und den Ruecksprungpunkt.
 * Doppelte Ereignisse (popstate und hashchange) sind harmlos: Beim zweiten
 * stimmt die Sicht schon.
 */
function onRouteChange() {
  const name = routeFromHash(location.hash);
  if (name === null) return;
  const modal = byId("modal-root");
  if (modal && modal.children.length) {
    routeReplaceNext = true;
    syncRoute();
    return;
  }
  const view = routeTarget(name);
  if (view !== name) routeReplaceNext = true;
  if (view === state.view) { syncRoute(); return; }
  state.view = view;
  state.dataFocusNode = null;
  state.staffFocusNode = null;
  state.orgFocusUnit = null;
  state.orgFocusAgents = [];
  state.returnTo = null;
  if ((state.passwordLogin && !state.principal) || state.startPending) return;
  closeMobileNav();
  render();
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
    loginNotice ? el("div", { class: "auth-notice", role: "status" }, loginNotice) : null,
    el("button", { class: "btn primary", onClick: () => startOidcLogin() },
      "Über Firmenkonto anmelden"),
    el("p", { class: "auth-hint", style: "margin-top:10px" },
      "Alternativ können Sie ein Zugangs-Token direkt in das Feld „API-Token“ der Seitenleiste eintragen. ",
      el("a", { href: "#", onClick: (e) => { e.preventDefault(); hideOverlay(); } }, "Ohne Anmeldung fortfahren")));
  showOverlay(card);
}

/**
 * Zeigt eine Karte (Anmeldung, Passwortwechsel, Firmenkonto) bildschirmfuellend
 * ueber der App.
 *
 * Die Karte ist ein modaler Dialog fuer Hilfsmittel: ``role=dialog``,
 * ``aria-modal`` und als Name die Ueberschrift aus ``authBrand``
 * (``#auth-title``); die App dahinter ist ohnehin ``inert`` (``watchInert``).
 *
 * @param {HTMLElement} card die Karte; ihre Rollen-Attribute werden gesetzt.
 */
function showOverlay(card) {
  const root = byId("auth-overlay");
  clear(root);
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");
  if (card.querySelector("h2")) card.setAttribute("aria-labelledby", "auth-title");
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
      el("h2", { id: "auth-title" }, "ProcWorks"),
      el("div", { class: "auth-hint" }, subtitle)));
}

// Full-screen login: exchange username + password for a session token, store it
// and continue booting. A forced password change is handled right after.
function showLoginOverlay() {
  // Live-Region: Ein Screenreader sagt die Meldung an, sobald sie erscheint;
  // das Passwortfeld verweist auf sie (``aria-describedby``).
  const errBox = el("div", { class: "auth-err", id: "login-err", role: "alert" });
  const loginInput = el("input", { type: "text", id: "login-name", autocomplete: "username", placeholder: "vorname.nachname" });
  const pwInput = el("input", { type: "password", id: "login-pw", autocomplete: "current-password",
    placeholder: "Passwort", "aria-describedby": "login-err" });
  const submit = async (e) => {
    if (e) e.preventDefault();
    errBox.textContent = "";
    markInvalid(pwInput, false);
    try {
      const res = await api.post("/auth/login", {
        login: loginInput.value.trim(),
        password: pwInput.value,
      });
      state.token = res.token;
      localStorage.setItem("authToken", state.token);
      loginNotice = "";
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
      // Nur eine echte Absage (401) sagt etwas ueber die Eingabe; Drosselung
      // und Ausfall markieren das Feld nicht.
      markInvalid(pwInput, !!(err && err.status === 401));
    }
  };
  const form = el("form", { onSubmit: submit },
    el("label", { class: "field" }, "Login", loginInput),
    el("label", { class: "field" }, "Passwort", pwInput),
    errBox,
    el("button", { class: "btn primary", type: "submit" }, "Anmelden"));
  // Nach dem Ende einer Sitzung: warum die Maske kommt (``expireSession``).
  const notice = loginNotice
    ? el("div", { class: "auth-notice", role: "status" }, loginNotice) : null;
  const card = el("div", { class: "auth-card" },
    authBrand("Bitte melden Sie sich an."), notice, form,
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
  // Wie bei der Anmeldung: Meldung als Live-Region, Felder verweisen auf sie.
  const errBox = el("div", { class: "auth-err", id: "pw-change-err", role: "alert" });
  const described = { "aria-describedby": "pw-change-err" };
  const curInput = el("input", { type: "password", autocomplete: "current-password", placeholder: "Aktuelles Passwort", ...described });
  const newInput = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort (min. 8 Zeichen)", ...described });
  const repInput = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort wiederholen", ...described });
  const submit = async (e) => {
    if (e) e.preventDefault();
    errBox.textContent = "";
    [curInput, newInput, repInput].forEach((i) => markInvalid(i, false));
    if (newInput.value !== repInput.value) {
      errBox.textContent = "Die Passw\u00F6rter stimmen nicht \u00FCberein.";
      markInvalid(repInput, true);
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
      // Sitzung beendet (401 ohne Eingabefehler-Code): ``request`` hat die
      // Anmeldemaske schon an die Stelle dieses Dialogs gesetzt -- hier
      // nichts mehr melden oder markieren.
      if (err && err.sessionExpired) return;
      // Der Kern nennt den konkreten Grund (``PW.too-short``/``PW.unchanged``,
      // 401 ``AUTH.wrong-current-password``); formuliert wird im
      // Meldungskatalog wie jede andere Absage.
      const wrongCurrent = !!(err && err.status === 401 && err.detail
        && err.detail.code === "AUTH.wrong-current-password");
      errBox.textContent = err && (err.status === 400 || wrongCurrent)
        ? describeError(err).title
        : "\u00C4nderung fehlgeschlagen.";
      // 400: das neue Passwort genuegt der Richtlinie nicht; 401 (mit Code):
      // das aktuelle ist falsch. Andere Fehler sagen nichts ueber die Eingaben.
      if (err && err.status === 400) markInvalid(newInput, true);
      if (wrongCurrent) markInvalid(curInput, true);
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
 * die Basis fuer „neue Aufgabe eingetroffen“, offene Dialoge, Tour-Angebot
 * und laufende Tour (``Tour.dismiss``) und der gezeichnete Seiteninhalt.
 * Ein laufendes Zeichnen einer Laufzeit-Sicht (auch das erste nach einem
 * Sichtwechsel) wird verworfen, ein vorgemerkter weiterer Lauf entfaellt.
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
  // Tour-Angebot und laufende Tour gehoeren zur Sitzung (sie lagen sonst ueber
  // der Anmeldemaske); zuerst, damit kein Tour-Code den Zustand danach anfasst.
  if (typeof Tour !== "undefined") Tour.dismiss();
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
  agentDirectoryLoad = { at: 0, pending: null };
  state.schemaIds = [];
  state.schemaNames = {};
  state.schemaVersions = {};
  state.schemaLifecycle = {};
  state.schemaRevisionOf = {};
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
  // Ein laufendes Neuzeichnen laedt noch mit der alten Sitzung: verwerfen,
  // und die naechste Sicht beginnt frisch (kein Austausch auf altem Inhalt).
  discardViewStage();
  drawnView = null;
  // Ein waehrend des Ladens vorgemerkter Lauf (Zeit-Tick, Poll) gehoert zur
  // alten Sitzung: nicht nachholen. Wer danach zeichnen soll (``boot`` nach
  // der Anmeldung), ruft ``render`` selbst.
  renderQueued = false;
  renderQueuedKeepScroll = true;
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
  if (!isGoneError(err)) return false;
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

/** Hinweis der Anmeldemaske, nachdem der Kern die Sitzung beendet hat. */
const SESSION_EXPIRED_NOTICE = "Ihre Sitzung ist abgelaufen \u2013 bitte erneut anmelden. "
  + "Was nach dem Ablauf abgeschickt wurde, ist nicht gespeichert; Eingaben in einem "
  + "offenen Dialog sind verworfen.";

/**
 * Hinweis, den die naechste Anmeldemaske ueber dem Formular zeigt (oder ``""``).
 * Gesetzt von ``expireSession``, geleert nach einer erfolgreichen Anmeldung --
 * so traegt auch die Maske eines folgenden ``boot`` (Neuladen) den Grund.
 */
let loginNotice = "";

/**
 * Beendet die Sitzung im Client, nachdem der Kern sie beendet hat (401).
 *
 * Aufgerufen von ``request`` bei jeder 401 einer angemeldeten Sitzung
 * (``isSessionEnd``), nie fuer die Anmeldung selbst. Wirkt wie ein Abmelden
 * ohne Serveraufruf: Token und Login-Bindung verwerfen, alles
 * Sitzungsbezogene leeren (``resetSessionState``: angezeigte Daten, offene
 * Dialoge, Tour), Meldungen der Sitzung entfernen, dann die Anmeldemaske mit
 * :data:`SESSION_EXPIRED_NOTICE` zeigen (bzw. im Firmen-Login-Modus dessen
 * Karte). Der Hinweis verspricht nichts, was der Client nicht halten kann:
 * Er sagt, dass Abgeschicktes nicht gespeichert und Dialog-Eingaben
 * verworfen sind -- eine 401 lehnt der Kern vor jeder Aenderung ab.
 *
 * Nur wo es eine Anmeldung gibt, zu der man zurueckkehren kann (Passwort-
 * Login, Firmen-Login). Im Token-Modus (Token von Hand in der Seitenleiste)
 * bleibt das Token stehen; der Fehler zeigt dort nur den deutschen Text.
 *
 * @returns {boolean} true, wenn die Sitzung beendet wurde; false, wenn es
 *   keine gab (kein Token -- auch der zweite von zwei gleichzeitigen 401)
 *   oder der Modus keine Anmeldemaske kennt.
 */
function expireSession() {
  const oidc = state.authMode === "jwt" && !!state.oidc;
  if (!state.token || !(state.passwordLogin || oidc)) return false;
  state.token = "";
  state.principal = null;
  try { localStorage.removeItem("authToken"); } catch (_e) { /* nur Komfort */ }
  try { localStorage.removeItem(SESSION_OWNER_KEY); } catch (_e) { /* nur Komfort */ }
  clearToasts();
  resetSessionState();
  renderUser();
  loginNotice = SESSION_EXPIRED_NOTICE;
  if (state.passwordLogin) showLoginOverlay();
  else showOidcLoginOverlay();
  return true;
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

/**
 * Hinweis an Besucher der oeffentlichen Demo: dort nur mit fiktiven Daten arbeiten.
 *
 * Die Demo ist eine oeffentlich erreichbare Wegwerf-Instanz. Wer dort seine
 * tatsaechlichen Unternehmensprozesse nachbaut oder echte Personen eintraegt,
 * legt Betriebsinterna und personenbezogene Daten auf einem fremden Server ab.
 * Der Satz steht deshalb dort, wo eingegeben wird: in der ausgeklappten Leiste
 * und im Tooltip der eingeklappten. Eine Quelle fuer beide Stellen.
 */
const DEMO_PRIVACY_NOTE =
  "Nur fiktive Beispieldaten: Bitte keine personenbezogenen Daten und keine echten Unternehmensprozesse eingeben.";

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

/**
 * Baut den Demo-Hinweis unten am Fenster neu auf (nur im oeffentlichen Demo-Modus).
 *
 * Drei Zustaende:
 * - fuer diese Sitzung geschlossen (sessionStorage `demoBannerDismissed`): nichts,
 * - eingeklappt (localStorage `demoBannerCollapsed`): ein kleiner Knopf; sein
 *   Tooltip und sein `aria-label` tragen `DEMO_PRIVACY_NOTE`,
 * - sonst die volle Leiste: angemeldete Person, Rollenwechsel, Passwort,
 *   `DEMO_PRIVACY_NOTE` als eigene Zeile (`.demo-privacy`) und die Knoepfe.
 * Ohne gespeicherten Zustand steht die Leiste ausgeklappt, der Hinweis ist
 * also beim ersten Besuch sichtbar.
 * Ein vorhandener Banner wird ersetzt; der Platz unten wird ueber
 * `reserveDemoBannerSpace` reserviert. Kein Rueckgabewert.
 */
function mountDemoBanner() {
  const old = byId("demo-banner");
  if (old) old.remove();
  if (sessionStorage.getItem("demoBannerDismissed") === "1") { reserveDemoBannerSpace(null); return; }

  // Eingeklappt: nur ein kleiner Knopf, der den Banner wieder oeffnet.
  if (storageGet(localStorage, DEMO_BANNER_COLLAPSED_KEY) === "1") {
    const pill = el("button", {
      id: "demo-banner", class: "demo-banner demo-banner-collapsed",
      title: `Demo-Hinweis mit Rollenwechsel und Passwort öffnen. ${DEMO_PRIVACY_NOTE}`,
      // Auch im Namen fuer Screenreader: ein Tooltip erreicht weder sie noch Touch-Geraete.
      "aria-label": `Demo-Hinweis öffnen. ${DEMO_PRIVACY_NOTE}`,
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
        : null,
      el("span", { class: "demo-privacy" }, DEMO_PRIVACY_NOTE)),
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
  if (!p && state.startPending) {
    // Start im Ausfall: Die Anmeldung ist noch nicht geprueft -- weder
    // „offen“ noch „nicht angemeldet“ behaupten.
    pill.textContent = "nicht gepr\u00FCft";
    pill.className = "pill pill-gray";
    foot.textContent = "Server nicht erreichbar \u2013 die Anmeldung wird gepr\u00FCft, sobald er antwortet.";
    return;
  }
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
    // Die Startsicht der Rolle (Bearbeiter: „Meine Aufgaben“); ohne Rolle mit
    // einer Sicht der erste sichtbare Menueeintrag wie bisher.
    const start = startViewOfRole();
    const first = buttons.find((b) => b.style.display !== "none");
    const next = start || (first && first.dataset.view);
    if (next) {
      state.view = next;
      // Berichtigung, kein Schritt der Person: die Adresse ersetzen.
      routeReplaceNext = true;
    }
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
/**
 * Die Sicht, die ``render`` zuletzt fertig gezeichnet hat (``null`` nach dem
 * Abmelden). Nur wenn sie mit der zu zeichnenden uebereinstimmt, bleibt der
 * alte Inhalt bis zum Austausch stehen (``openViewStage`` in core.js).
 * @type {string|null}
 */
let drawnView = null;
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
  if (!VIEW_META[state.view]) { state.view = "model"; routeReplaceNext = true; }
  // Der Kontrollfluss-Vollbildmodus lebt nur in der Modellieren-Sicht; beim
  // Verlassen zuruecksetzen, damit er nicht als Overlay in anderen Sichten haengt.
  if (state.view !== "model") state.graphMaximized = false;
  // Remember the active view so a page reload restores it instead of always
  // falling back to "Modellieren".
  localStorage.setItem("view", state.view);
  // Die Sicht steht als Route in der Adresse (Zurueck/Vor, Lesezeichen).
  syncRoute();
  const meta = VIEW_META[state.view];
  byId("view-title").textContent = meta.title;
  byId("view-sub").textContent = meta.sub;
  byId("view-sub").title = meta.sub;   // gekuerzt angezeigt, voll im Tooltip
  renderSchemaPicker();
  setActiveNav();
  renderTourBadge();
  // Laufzeit-Sichten: Fokus und aufgeklappte Bereiche ueberstehen das
  // Neuzeichnen (captureViewState/restoreViewState in core.js). Nur, wenn die
  // Sicht dieselbe bleibt -- ein Sichtwechsel beginnt frisch.
  const content = byId("content");
  const viewBefore = state.view;
  // Steht dieselbe Laufzeit-Sicht schon da, zeichnet sie in einen
  // Zwischenbehaelter, und der alte Inhalt bleibt bedienbar, bis der neue
  // fertig geladen ist (beginViewContent/commitViewStage in core.js). Den
  // Ansichtszustand dann erst unmittelbar vor dem Austausch merken -- die
  // Person kann sich waehrend des Ladens weiterbewegt haben.
  const staged = keepsViewState() && drawnView === state.view && !!content.firstChild;
  let snapshot = keepsViewState() ? captureViewState(content) : null;
  // Auch die erste Zeichnung einer Laufzeit-Sicht (Sichtwechsel) laeuft ueber
  // den Zwischenbehaelter, nur auf leerem Inhalt -- nie der alte Inhalt unter
  // neuem Titel. So kann ``resetSessionState`` (Abmelden waehrend des Ladens)
  // auch sie verwerfen; vorher schrieb die Sicht direkt in ``#content``, und
  // die Daten der beendeten Sitzung erschienen nach dem Abmelden.
  const firstDraw = keepsViewState() && !staged;
  if (firstDraw) clear(content);
  if (staged || firstDraw) openViewStage(content);
  Promise.resolve(meta.fn())
    .catch((err) => { toastError(err); })
    .then(() => {
      if (firstDraw) { commitViewStage(); return; }
      if (!staged) return;
      if (state.view === viewBefore && viewStagePending()) snapshot = captureViewState(content);
      commitViewStage();
    })
    .then(() => { drawnView = viewBefore; })
    // Waehrend eines Serverausfalls sagt jede Sicht oben, dass sie nicht
    // nachladen konnte -- auch eine mit zwischengespeicherten Daten.
    .then(() => { if (state.connected === false) showOutageBanner(); })
    .then(() => { if (scroller) scroller.scrollTop = scrollTop; })
    .then(() => { if (snapshot && state.view === viewBefore) restoreViewState(snapshot, content); })
    // Die Sichten bauen ihr DOM bei jedem Rendern komplett neu auf -- der Anker
    // der laufenden Tour existiert danach nicht mehr und muss neu gesucht
    // werden. Gekapselt, damit ein Fehler in der Tour nie die Sicht mitreisst.
    .then(() => { if (typeof Tour !== "undefined") Tour.afterRender(); })
    // Erst hier ist die Sicht fertig aufgebaut. Ein waehrenddessen vorgemerkter
    // Lauf wird jetzt nachgeholt -- ``finally``, damit ein Fehler das Rendern
    // nicht dauerhaft blockiert (renderBusy bliebe sonst true).
    .catch(() => { /* Tour-Fehler: die Sicht steht, weiter geht es trotzdem */ })
    .finally(() => {
      // Nie einen Zwischenbehaelter offen lassen (er finge sonst das naechste
      // Zeichnen einer anderen Sicht ab); ohne offenen ein No-op.
      if (staged || firstDraw) commitViewStage();
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
 * Setzt das Ausfall-Banner (``outageBanner``) an den Anfang des Inhalts.
 *
 * Einmal je Zeichnen: Die Sichten leeren ``#content`` selbst, ein Banner aus
 * einem frueheren Lauf steht also nie doppelt. Ohne Inhaltsbereich (frueher
 * Start) geschieht nichts.
 */
function showOutageBanner() {
  const content = byId("content");
  if (!content || content.querySelector(".outage-banner")) return;
  content.insertBefore(outageBanner(), content.firstChild);
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
    // Nach einem Ausfall einmal neu zeichnen, auch ohne neue Revision: Die
    // Sicht wurde waehrend des Ausfalls mit Fehlerzustand gezeichnet. Die
    // Marke bleibt stehen, bis wirklich neu gezeichnet wurde (eine gerade
    // beschaeftigte Person verschiebt das nur).
    const recovered = outageRedrawPending;
    if (rev === state.revision && !recovered) return;
    if (!LIVE_VIEWS.has(state.view)) {
      state.revision = rev;
      // Modellier- und Verwaltungssichten zeichnen nie von selbst neu; nur
      // der Hinweis auf den Ausfall verschwindet.
      outageRedrawPending = false;
      const content = byId("content");
      const banner = content && content.querySelector(".outage-banner");
      if (banner) banner.remove();
      return;
    }
    if (userIsBusy()) {
      // Ungespeicherte Eingaben (nicht Dialog/Anmeldung): Daten nicht still
      // verschlucken, sondern Neuladen anbieten.
      if (contentDirty && !byId("modal-root").children.length) showLiveRefreshHint();
      // Die Revision bewusst NICHT uebernehmen: Sie gilt erst als gesehen,
      // wenn neu gezeichnet wurde. Sonst verfiel eine waehrend eines Dialogs
      // eingetroffene Aenderung bis zum naechsten Zeit-Tick (bis zu 30 s).
      return;
    }
    state.revision = rev;
    // Vor dem Nachladen zuruecksetzen: scheitert es erneut am Ausfall, merkt
    // ``setConnected(false)`` ihn wieder vor.
    outageRedrawPending = false;
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

/**
 * Laedt die Laufzeit-Sicht sofort nach, wenn der Kern eine Aktion mit 409
 * abgelehnt hat.
 *
 * Ein 409 heisst: Der Stand, auf dem die Person handelte, ist ueberholt (meist
 * hat jemand anderes den Schritt schon erledigt oder uebernommen). Die Meldung
 * raet zum Aktualisieren -- die Liste zeigte den erledigten Schritt aber bis
 * zum naechsten Poll bzw. Zeit-Tick weiter. Jetzt zeichnet sie sofort neu,
 * auch hinter einem offenen Dialog (der bleibt stehen; beim Schliessen ist
 * die Liste schon aktuell). Nur Anzeige: Was erlaubt ist, entscheidet der Kern.
 *
 * @param {*} err der gefangene Fehler von ``request``.
 * @returns {Promise<void>|undefined} das Nachladen, oder ``undefined``, wenn
 *   nichts zu tun ist: kein 409, keine Laufzeit-Sicht. Stehen im Inhalt nicht
 *   abgeschickte Eingaben (``contentDirty``), wird nicht ueber sie hinweg
 *   gezeichnet, sondern „Aktualisieren“ angeboten (``showLiveRefreshHint``).
 */
function refreshAfterConflict(err) {
  if (!err || err.status !== 409 || !LIVE_VIEWS.has(state.view)) return undefined;
  if (contentDirty) { showLiveRefreshHint(); return undefined; }
  const reload = state.view === "run" && state.instanceId
    ? loadInstance(state.instanceId).catch((e) => dropUnreadableInstance(e))
    : Promise.resolve();
  return reload.then(() => render({ keepScroll: true }));
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
  // Letztes Fokusziel im Inhalt merken: Faellt der Fokus beim Neuzeichnen auf
  // <body>, findet render sein Gegenstueck wieder (captureViewState).
  document.addEventListener("focusin", trackContentFocus, true);
  const skip = byId("skip-link");
  if (skip) skip.addEventListener("click", skipToContent);
  // Zurueck/Vor und eine von Hand geaenderte Adresse wechseln die Sicht.
  window.addEventListener("popstate", onRouteChange);
  window.addEventListener("hashchange", onRouteChange);
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

// --------------------------------------------------------------------------
// Start im Ausfall: Wiederholung mit wachsendem Abstand
// --------------------------------------------------------------------------

/** Abstand vor dem ersten erneuten Startversuch nach einem Ausfall (ms). */
const BOOT_RETRY_FIRST_MS = 3000;
/** Obergrenze des Abstands zwischen zwei Startversuchen (ms). */
const BOOT_RETRY_MAX_MS = 30000;

/**
 * Geplante Startwiederholung: ``timer`` (oder ``null``) und der Abstand fuer
 * die naechste Planung. Ein erfolgreicher Start setzt ``delay`` zurueck.
 * @type {{timer: (number|null), delay: number}}
 */
let bootRetry = { timer: null, delay: BOOT_RETRY_FIRST_MS };

/**
 * Plant nach einem Ausfall beim Start einen neuen Versuch (``retryBoot``).
 *
 * Ohne erfolgreichen Start laufen weder Live-Abfrage noch Zeit-Tick -- eine im
 * Ausfall geladene Seite haette sich sonst nie verbunden. Der Abstand
 * verdoppelt sich je Fehlschlag bis ``BOOT_RETRY_MAX_MS``, damit eine lange
 * stehende API nicht im Sekundentakt gefragt wird. Eine schon geplante
 * Wiederholung wird ersetzt (nie zwei nebeneinander).
 *
 * @returns {number} der geplante Abstand in ms.
 */
function scheduleBootRetry() {
  if (bootRetry.timer) clearTimeout(bootRetry.timer);
  const delay = bootRetry.delay;
  bootRetry.timer = setTimeout(retryBoot, delay);
  bootRetry.delay = Math.min(delay * 2, BOOT_RETRY_MAX_MS);
  return delay;
}

/**
 * Fuehrt die geplante Startwiederholung aus (Ziel des Zeitgebers).
 * @returns {Promise<void>} der Startversuch.
 */
function retryBoot() {
  bootRetry.timer = null;
  return boot();
}

/**
 * Zeigt statt einer Sicht den Hinweis, dass der Start auf den Server wartet.
 *
 * Solange die Anmeldung nicht geprueft ist, zeichnet keine Sicht: Sie haette
 * nur Fehlerzustaende fuer Bereiche gezeigt, deren Rechte niemand kennt. Das
 * Menue ist leer (``applyRoleNav`` ohne Anmeldung), das Token-Feld
 * verborgen -- ob es gebraucht wird, sagt erst die Serverkonfiguration.
 */
function showStartPending() {
  const tokenField = byId("token-field");
  if (tokenField) tokenField.style.display = "none";
  renderUser();
  applyRoleNav();
  byId("view-title").textContent = "ProcWorks";
  byId("view-sub").textContent = "";
  drawnView = null;
  const content = byId("content");
  clear(content);
  content.appendChild(outageBanner());
  content.appendChild(emptyState(
    "Die Anwendung startet, sobald der Server antwortet \u2013 neue Versuche laufen automatisch."));
}

async function boot() {
  // Ein ausdruecklicher Start (Anmeldung, neue API-Adresse) ersetzt eine
  // geplante Wiederholung.
  if (bootRetry.timer) { clearTimeout(bootRetry.timer); bootRetry.timer = null; }
  // Neuladen/Lesezeichen: die Sicht der Adresse oeffnen (das Recht prueft
  // applyRoleNav, sobald die Anmeldung feststeht).
  adoptRouteFromUrl();
  try {
    const health = await api.get("/health");
    setConnected(true);
    bootRetry.delay = BOOT_RETRY_FIRST_MS;
    state.startPending = false;
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
    // Erstpasswort noch nicht ersetzt (etwa Neuladen direkt nach der ersten
    // Anmeldung): Der Kern laesst bis zum Wechsel nur /auth/me, den Wechsel
    // und das Abmelden zu. Statt die Anwendung zu laden und lauter Absagen
    // einzusammeln, gleich den Pflichtdialog zeigen; nach dem Wechsel ruft er
    // boot() erneut.
    if (state.principal && state.principal.must_change) {
      showChangePasswordOverlay(true);
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
    // Eine Ablehnung des Kerns (4xx, etwa eine abgelaufene Sitzung) ist kein
    // Ausfall -- der Server hat geantwortet, ``request`` hat das schon
    // vermerkt. Alles andere (Ausfall, kein ProcWorks-Server unter der
    // Adresse) zeigt „getrennt“.
    if (!(err && err.status >= 400 && err.status < 500)) setConnected(false);
    toastError(err);
    // Ausfall (keine Antwort, Gateway-Fehler): neu versuchen. Ohne gepruefte
    // Anmeldung ist das nicht der offene Modus -- statt einer Sicht steht der
    // Hinweis da.
    if (err && err.unreachable) {
      scheduleBootRetry();
      if (!state.principal) {
        state.startPending = true;
        showStartPending();
        return;
      }
    }
  }
  render();
}

wireNav();
watchInert();
boot();
