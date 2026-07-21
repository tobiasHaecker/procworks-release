// SPDX-License-Identifier: BUSL-1.1
// ---------------------------------------------------------------------------
// Engine der geführten Tour (docs/Tutorial-Konzept.md).
//
// Kennt KEINE Inhalte -- die stehen in tours.js, die Aufzeichnung des
// Beispielprozesses in fixtures.js. Diese Datei kann drei Dinge:
//
//   1. Ein Popup an ein Element im DOM heften und den Fortschritt verwalten.
//   2. Den schreibfreien SANDKASTEN durchsetzen: Solange eine Sandkasten-Tour
//      läuft, verlässt kein POST/PUT/PATCH/DELETE den Browser. Deshalb
//      entstehen durch Tutorial-Eingaben keine dauerhaften Daten.
//   3. Sich merken, wer welche Tour schon gesehen oder verschoben hat.
//
// Stabilität geht vor Führung: Jeder Einstiegspunkt ist gekapselt; ein Fehler
// in der Tour beendet die Tour, nie die Anwendung. Findet ein Schritt seinen
// Anker nicht, rutscht das Popup in die Mitte, statt zu scheitern.
// ---------------------------------------------------------------------------

const Tour = (() => {
  "use strict";

  // --- Konstanten ---------------------------------------------------------

  /** Schlüssel-Präfix im localStorage. Gespeichert wird NUR, was der Nutzer
   *  über die Tour entschieden hat -- niemals eine seiner Eingaben. */
  const LS = "tour.";
  /** Wie oft "Später erinnern" höchstens erneut anbietet, bevor Ruhe ist. */
  const MAX_POSTPONE = 3;
  /** Wartezeit, bis ein fehlender Anker als "nicht da" gilt (ms). */
  const ANCHOR_TIMEOUT_MS = 1500;
  /** Takt, in dem Fortschrittsbedingung und Ankerposition geprüft werden (ms). */
  const TICK_MS = 300;
  /** Verzögerung des Erstangebots nach dem Booten (ms), damit die Anwendung
   *  nicht unter dem Modal aufblitzt. */
  const OFFER_DELAY_MS = 400;
  /** Ab dieser Breite gilt die Ansicht als "Desktop" (vgl. styles.css). */
  const DESKTOP_MIN_PX = 721;

  // --- Zustand (bewusst NICHT persistiert) --------------------------------

  const t = {
    tour: null,        // laufende Tour
    index: 0,          // aktueller Schritt
    sandbox: false,    // schreibfreier Modus aktiv?
    stage: 0,          // Stufe der Aufzeichnung (nur im Sandkasten)
    rejected: false,   // wurde die vorgeführte Ablehnung bereits ausgelöst?
    saved: null,       // gesicherter App-Zustand, für das Ende der Tour
    timer: null,       // Intervall für Fortschritt/Neupositionierung
    anchorSince: 0,    // seit wann wird der Anker des Schritts vermisst?
    painted: null,     // zuletzt gezeichnetes Overlay (siehe paint())
  };

  // --- Merker (localStorage) ----------------------------------------------

  /**
   * Präfix der Merker des **angemeldeten** Nutzers.
   *
   * Die Merker hängen am Browser, das Konto wechselt aber darin: Ohne diesen
   * Zusatz erbte der nächste Nutzer den Stand seines Vorgängers. Zwei Personen
   * derselben Rolle (in der Demo etwa Erika und Tom, beide Bearbeiter) teilen
   * sich denselben Tour-Schlüssel -- hatte die eine die Tour gesehen oder
   * abgelehnt, bekam die andere nie ein Angebot. Auf gemeinsam genutzten
   * Rechnern gilt dasselbe.
   *
   * Ohne Anmeldung (offener Modus) bleibt es beim bisherigen, browserweiten
   * Schlüssel -- dort gibt es kein Konto, an dem sich etwas festmachen liesse.
   *
   * @returns {string} Präfix inklusive Trennzeichen (oder "").
   */
  function who() {
    try {
      const subject = state.principal && state.principal.subject;
      return subject ? `${subject}.` : "";
    } catch (_e) { return ""; }
  }

  /**
   * Liest einen Merker der Tour aus dem localStorage.
   *
   * @param {string} key Schlüssel ohne Präfix.
   * @returns {string|null} Wert oder null.
   */
  function mark(key) {
    try { return localStorage.getItem(LS + who() + key); } catch (_e) { return null; }
  }

  /**
   * Schreibt einen Merker der Tour. Fehler (privater Modus, volle Quote)
   * werden geschluckt -- ein nicht merkbarer Fortschritt ist ein Schönheits-
   * fehler, kein Grund, die Tour zu verweigern.
   *
   * @param {string} key Schlüssel ohne Präfix.
   * @param {string} value Zu speichernder Wert.
   */
  function setMark(key, value) {
    try { localStorage.setItem(LS + who() + key, value); } catch (_e) { /* egal */ }
  }

  /** @returns {boolean} true, wenn die Tour in dieser Fassung erledigt ist. */
  function isDone(tour) {
    return mark(`done.${tour.id}.${tour.version}`) === "1";
  }

  /** @returns {number} Wie oft die Tour bereits verschoben wurde. */
  function postponeCount(tour) {
    return Number(mark(`postponed.${tour.id}`) || 0);
  }

  /** @returns {number} Gemerkter Schritt-Index eines Abbruchs (0, wenn keiner). */
  function savedProgress(tour) {
    const raw = mark(`progress.${tour.id}.${tour.version}`);
    const i = Number(raw);
    return Number.isFinite(i) && i > 0 && i < tour.steps.length ? i : 0;
  }

  // --- Auswahl der passenden Tour -----------------------------------------

  /** @returns {boolean} true auf schmalen (mobilen) Ansichten. */
  function isMobile() {
    return window.innerWidth < DESKTOP_MIN_PX;
  }

  /**
   * Alle Touren, die zur Rolle des angemeldeten Nutzers passen -- in der
   * Rangfolge aus TOUR_ROLE_ORDER und ohne solche, die in der aktuellen
   * Ansicht (mobil) gar nicht sinnvoll sind.
   *
   * @returns {Array<object>} Passende Touren, ggf. leer.
   */
  function availableTours() {
    const mobile = isMobile();
    return TOUR_ROLE_ORDER
      .map((role) => TOURS.find((x) => x.role === role))
      .filter((tour) => tour && hasRole(tour.role) && (tour.mobile !== false || !mobile));
  }

  // --- Angebot beim ersten Anmelden ---------------------------------------

  /**
   * Bietet nach dem Booten die erste noch nicht erledigte Tour an.
   *
   * Wird von ``boot()`` aufgerufen. Zeigt höchstens ein Angebot und niemals
   * eines, das der Nutzer bereits abgelehnt oder dreimal verschoben hat.
   */
  function maybeOffer() {
    try {
      if (t.tour) return;
      const tour = availableTours().find(
        (x) => !isDone(x) && postponeCount(x) < MAX_POSTPONE);
      if (!tour) return;
      setTimeout(() => { try { offer(tour); } catch (_e) { /* still */ } }, OFFER_DELAY_MS);
    } catch (_e) { /* Angebot ist Kür -- nie die App gefährden */ }
  }

  /**
   * Zeigt das Willkommens-Modal mit den drei Entscheidungen des Nutzers:
   * starten, später erinnern oder endgültig ablehnen.
   *
   * @param {object} tour Die anzubietende Tour.
   */
  function offer(tour) {
    if (t.tour) return;
    const resumeAt = savedProgress(tour);
    const root = byId("tour-root");
    clear(root);
    const card = el("div", { class: "tour-offer" },
      el("h2", null, "Kurze Einführung?"),
      el("p", null,
        `„${tour.title}“ — ${tour.subtitle}. `,
        `${tour.steps.length} Schritte, keine zwei Minuten.`),
      tour.sandbox
        ? el("p", { class: "tour-offer-note" },
            "Die Tour arbeitet auf einem Beispielprozess in deinem Browser. Es wird nichts gespeichert.")
        : null,
      resumeAt
        ? el("p", { class: "tour-offer-note" },
            `Du warst zuletzt bei Schritt ${resumeAt + 1} stehengeblieben.`)
        : null,
      el("div", { class: "tour-offer-actions" },
        el("button", { class: "btn primary", onClick: () => { clear(root); start(tour.id, { resume: !!resumeAt }); } },
          resumeAt ? "Fortsetzen" : "Tour starten"),
        resumeAt
          ? el("button", { class: "btn ghost", onClick: () => { clear(root); start(tour.id, { resume: false }); } }, "Von vorn")
          : null,
        el("button", { class: "btn ghost", onClick: () => { postpone(tour); clear(root); } }, "Später erinnern"),
        el("button", { class: "btn ghost", onClick: () => { setMark(`done.${tour.id}.${tour.version}`, "1"); clear(root); } }, "Nein danke")));
    root.appendChild(el("div", { class: "tour-offer-backdrop" }, card));
  }

  /**
   * Merkt eine Verschiebung. Nach MAX_POSTPONE Verschiebungen wird die Tour
   * nicht mehr von selbst angeboten -- sie bleibt aber in der Hilfe erreichbar.
   *
   * @param {object} tour Die verschobene Tour.
   */
  function postpone(tour) {
    setMark(`postponed.${tour.id}`, String(postponeCount(tour) + 1));
    toast("info", "Später gern", ["Die Einführung findest du jederzeit unter „Hilfe“."]);
  }

  // --- Start / Ende --------------------------------------------------------

  /**
   * Startet eine Tour.
   *
   * Bei einer Sandkasten-Tour wird der bisherige App-Zustand gesichert und der
   * Tutorial-Beispielprozess eingesetzt; ab dann blockt intercept() jeden
   * schreibenden API-Aufruf.
   *
   * @param {string} tourId Kennung aus tours.js.
   * @param {{resume?: boolean}} [opts] resume = beim gemerkten Schritt einsteigen.
   */
  function start(tourId, opts) {
    const tour = TOURS.find((x) => x.id === tourId);
    if (!tour || t.tour) return;
    t.tour = tour;
    t.index = opts && opts.resume ? savedProgress(tour) : 0;
    t.stage = 0;
    t.rejected = false;
    t.anchorSince = 0;
    t.painted = null;

    if (tour.sandbox) enterSandbox();

    const step = tour.steps[t.index];
    if (step && step.view && state.view !== step.view) state.view = step.view;
    render();
    t.timer = setInterval(tick, TICK_MS);
    document.addEventListener("keydown", onKey, true);
  }

  /**
   * Beendet die Tour und stellt den Ausgangszustand wieder her.
   *
   * @param {{completed?: boolean}} [opts] completed = regulär durchlaufen
   *   (dann wird kein Fortschritt zum Fortsetzen gemerkt).
   */
  function stop(opts) {
    const tour = t.tour;
    if (!tour) return;
    const completed = !!(opts && opts.completed);
    try {
      if (completed) {
        setMark(`done.${tour.id}.${tour.version}`, "1");
        setMark(`progress.${tour.id}.${tour.version}`, "0");
      } else {
        setMark(`progress.${tour.id}.${tour.version}`, String(t.index));
      }
    } catch (_e) { /* egal */ }

    if (t.timer) clearInterval(t.timer);
    t.timer = null;
    document.removeEventListener("keydown", onKey, true);
    t.tour = null;
    t.index = 0;
    t.painted = null;
    clear(byId("tour-root"));
    document.documentElement.removeAttribute("data-tour-active");

    // Sandkasten IMMER verlassen -- auch wenn oben etwas schiefging.
    const wasSandbox = t.sandbox;
    t.sandbox = false;
    t.stage = 0;
    t.rejected = false;
    if (wasSandbox) leaveSandbox();
    else render();

    if (!completed) {
      toast("info", "Tour beendet", ["Jederzeit unter „Hilfe“ neu startbar."]);
    }
  }

  // --- Sandkasten ----------------------------------------------------------

  /**
   * Schaltet in den schreibfreien Modus: sichert den echten Zustand weg und
   * setzt den Tutorial-Beispielprozess (Stufe 0) ein.
   *
   * Ab hier fängt intercept() jeden Aufruf ab, der schreiben würde oder den
   * Beispielprozess betrifft.
   */
  function enterSandbox() {
    t.saved = {
      schemaId: state.schemaId,
      schema: state.schema,
      validation: state.validation,
      view: state.view,
      selectedNode: state.selectedNode,
      paletteTab: state.paletteTab,
      schemaIds: state.schemaIds.slice(),
    };
    t.sandbox = true;
    t.stage = 0;
    applyStage();
    state.selectedNode = null;
    state.paletteTab = "data";
  }

  /**
   * Verlässt den Sandkasten und holt den echten Zustand zurück. Der zuvor
   * angezeigte Prozess wird frisch vom Kern geladen, damit die Anwendung
   * garantiert wieder auf echten Daten steht.
   */
  function leaveSandbox() {
    const saved = t.saved;
    t.saved = null;
    if (!saved) { render(); return; }
    state.schemaId = saved.schemaId;
    state.schema = saved.schema;
    state.validation = saved.validation;
    state.view = saved.view;
    state.selectedNode = saved.selectedNode;
    state.paletteTab = saved.paletteTab;
    state.schemaIds = saved.schemaIds;
    // Frisch nachladen (und dabei den localStorage-Eintrag wieder geraderücken,
    // den refreshSchema() im Sandkasten auf die Tutorial-Id gesetzt hat).
    Promise.resolve()
      .then(() => (state.schemaId ? refreshSchema() : null))
      .catch(() => { /* Anzeige bleibt beim gesicherten Stand */ })
      .then(() => render());
  }

  /**
   * Setzt Schema und Befunde der aktuellen Aufzeichnungsstufe in den
   * Anwendungszustand.
   *
   * Es wird eine tiefe Kopie eingesetzt, damit die Aufzeichnung selbst nie von
   * der GUI verändert werden kann (sie wird pro Tour mehrfach gelesen).
   */
  function applyStage() {
    const rec = TOUR_FIXTURES.stages[Math.min(t.stage, TOUR_FIXTURES.stages.length - 1)];
    state.schema = JSON.parse(JSON.stringify(rec.schema));
    state.validation = JSON.parse(JSON.stringify(rec.validation));
    state.schemaId = state.schema.id;
    state.schemaIds = [state.schema.id];
    state.schemaNames[state.schema.id] = state.schema.name;
    state.schemaVersions[state.schema.id] = state.schema.version;
  }

  /**
   * DER Sperrpunkt für „keine dauerhaften Daten“.
   *
   * Wird von ``request()`` vor jedem Netzwerkaufruf befragt. Liefert sie eine
   * Funktion, findet KEIN fetch statt -- die Antwort kommt aus der Aufzeichnung.
   * Liefert sie null, läuft der Aufruf ganz normal.
   *
   * Regeln im Sandkasten:
   *   - alles unter /schemas wird bedient (GET aus der Aufzeichnung,
   *     schreibende Aufrufe über das sim-Feld des laufenden Schritts),
   *   - jeder andere schreibende Aufruf wird freundlich abgelehnt,
   *   - andere Lesezugriffe laufen echt durch (die Anwendung soll sich nicht
   *     „tot“ anfühlen).
   *
   * @param {string} method HTTP-Methode.
   * @param {string} path Pfad ab der API-Basis.
   * @param {object|undefined} body Anfragekörper.
   * @returns {null|function(): Promise<*>} Ersatzantwort oder null.
   */
  function intercept(method, path, body) {
    if (!t.sandbox) return null;
    const write = method !== "GET";
    const isSchemaPath = path === "/schemas" || path.startsWith("/schemas/");
    if (!write && !isSchemaPath) return null;

    if (!write) return () => Promise.resolve(readSchemaPath(path));
    if (isSchemaPath) return () => applySimulation(body);
    return () => Promise.reject({
      status: 400,
      detail: "Im Tutorial werden keine Daten gespeichert.",
    });
  }

  /**
   * Beantwortet einen lesenden /schemas-Aufruf aus der Aufzeichnung.
   *
   * @param {string} path Angefragter Pfad.
   * @returns {*} Das passende Stück der Aufzeichnung.
   */
  function readSchemaPath(path) {
    if (path === "/schemas") return [state.schema.id];
    if (path.endsWith("/validation")) return state.validation;
    const rest = path.slice("/schemas/".length);
    if (rest === state.schema.id) return state.schema;
    // Alles andere (Metriken, Instanzen eines Schemas …) gibt es im Tutorial
    // nicht -- eine leere, gültige Antwort ist harmloser als ein Fehler.
    return null;
  }

  /**
   * Führt den schreibenden Aufruf des aktuellen Schritts als Aufzeichnung aus.
   *
   * Drei Fälle, gesteuert über ``step.sim``:
   *   - ``{stage: n}``  -> die Aufzeichnung rückt auf Stufe n vor,
   *   - ``{reject: true}`` -> die vorgeführte Ablehnung des Kerns (HTTP 422),
   *   - kein sim       -> freundliche Ablehnung (der Schritt sieht das nicht vor).
   *
   * ``applyLabel`` übernimmt zusätzlich die vom Nutzer eingetippte Bezeichnung
   * in den aufgezeichneten Knoten. Das ist reine Kosmetik an einer Konserve --
   * es wird nichts berechnet und nichts validiert.
   *
   * @param {object|undefined} body Der abgefangene Anfragekörper.
   * @returns {Promise<*>} Ersatzantwort bzw. abgelehnte Zusage.
   */
  function applySimulation(body) {
    const step = current();
    const sim = step && step.sim;
    if (!sim) {
      return Promise.reject({
        status: 400,
        detail: "Im Tutorial werden keine Daten gespeichert.",
      });
    }
    if (sim.reject) {
      t.rejected = true;
      return Promise.reject({ status: 422, detail: TOUR_FIXTURES.rejection });
    }
    t.stage = sim.stage;
    applyStage();
    if (sim.applyLabel && body && typeof body.label === "string" && body.label.trim()) {
      const node = Object.values(state.schema.nodes)
        .find((n) => n.label === TOUR_NEW_STEP_LABEL);
      if (node) node.label = body.label.trim();
    }
    return Promise.resolve(state.schema);
  }

  // --- Ablauf --------------------------------------------------------------

  /** @returns {object|null} Der aktuelle Schritt. */
  function current() {
    return t.tour ? t.tour.steps[t.index] : null;
  }

  /**
   * Geht einen Schritt weiter -- oder beendet die Tour nach dem letzten.
   *
   * Wechselt bei Bedarf in die Sicht des nächsten Schritts, damit ein reiner
   * „Zeigen“-Schritt nicht ins Leere zeigt.
   */
  function next() {
    if (!t.tour) return;
    if (t.index >= t.tour.steps.length - 1) { stop({ completed: true }); return; }
    t.index += 1;
    t.anchorSince = 0;
    const step = current();
    if (step && step.view && state.view !== step.view) {
      state.view = step.view;
      render();
      return;
    }
    paint();
  }

  /** Geht einen Schritt zurück (ohne den Modellzustand zurückzudrehen). */
  function prev() {
    if (!t.tour || t.index === 0) return;
    t.index -= 1;
    t.anchorSince = 0;
    paint();
  }

  /**
   * Taktgeber: prüft die Fortschrittsbedingung des Schritts und hält das Popup
   * an seinem Anker, wenn sich das Layout bewegt hat (Scrollen, Zoom, Resize).
   */
  function tick() {
    try {
      const step = current();
      if (!step) return;
      if (typeof step.advance === "function" && step.advance(ctx())) { next(); return; }
      paint();
    } catch (_e) {
      stop();
    }
  }

  /**
   * Baut den Kontext, den eine Fortschrittsbedingung auswerten darf.
   *
   * Bewusst schmal: Anwendungszustand (lesend), Sandkasten-Stufe und die
   * Information, ob die vorgeführte Ablehnung schon eingetreten ist.
   *
   * @returns {{state: object, stage: number, rejected: boolean}} Kontext.
   */
  function ctx() {
    return { state, stage: t.stage, rejected: t.rejected };
  }

  /**
   * Prüft, ob der Fokus in einem Eingabeelement steht, das die Tasten selbst
   * braucht.
   *
   * Hintergrund: ``onKey`` läuft in der Capture-Phase und nimmt Esc und die
   * Pfeiltasten weg, *bevor* das fokussierte Element sie sieht. Genau diese
   * Tasten bedienen aber ein Datums-/Zeitfeld -- die Pfeile wechseln zwischen
   * Tag, Monat und Jahr und zählen den Wert hoch, Esc schließt den Kalender.
   * Ohne diese Ausnahme ließ sich das Abwesenheits-Datum bei laufender Tour
   * nicht auswählen: jeder Pfeiltastendruck blätterte die Tour weiter.
   *
   * Deshalb dieselbe Regel wie beim offenen Dialog: Wer tippt, besitzt die
   * Tastatur. Die Tour bleibt per Maus vollständig bedienbar (Zurück/Weiter
   * im Popup, „×“ beendet), es geht also kein Bedienweg verloren.
   *
   * @param {EventTarget|null} target Ziel des Tastenereignisses.
   * @returns {boolean} true, wenn die Tour die Taste durchlassen muss.
   */
  function typingInField(target) {
    if (!target || target.nodeType !== 1) return false;
    if (target.isContentEditable) return true;
    return ["INPUT", "SELECT", "TEXTAREA"].indexOf(target.tagName) !== -1;
  }

  /**
   * Reagiert auf Tastatur: Esc beendet, Pfeiltasten blättern.
   *
   * Läuft in der Capture-Phase, damit Esc die Tour beendet, bevor die
   * Anwendung es als „Vollbild verlassen“ deutet.
   *
   * @param {KeyboardEvent} e Tastenereignis.
   */
  function onKey(e) {
    if (!t.tour) return;
    // In einem geöffneten Dialog gehört die Tastatur dem Dialog.
    if (byId("modal-root").children.length) return;
    // Ebenso in einem Eingabefeld (Datum, Auswahl, Text) -- siehe typingInField.
    if (typingInField(e.target)) return;
    if (e.key === "Escape") { e.stopPropagation(); stop(); }
    else if (e.key === "ArrowRight") { e.stopPropagation(); next(); }
    else if (e.key === "ArrowLeft") { e.stopPropagation(); prev(); }
  }

  /**
   * Wird nach jedem ``render()`` der Anwendung aufgerufen und zeichnet das
   * Overlay neu -- die Sichten bauen ihr DOM jedes Mal komplett neu auf, der
   * Anker von eben existiert also nicht mehr.
   */
  function afterRender() {
    if (!t.tour) return;
    try { paint(); } catch (_e) { stop(); }
  }

  // --- Darstellung ---------------------------------------------------------

  /**
   * Zeichnet Spotlight und Popup für den aktuellen Schritt.
   *
   * Solange ein Dialog offen ist, tritt die Tour zurück (leeres Overlay) --
   * sonst läge der Scrim über dem Dialog, den der Nutzer gerade ausfüllen soll.
   * Fehlt der Anker länger als ANCHOR_TIMEOUT_MS, rutscht das Popup mittig und
   * bietet das Überspringen an, statt die Tour scheitern zu lassen.
   *
   * **Wichtig -- wird im Takt von TICK_MS aufgerufen.** Solange derselbe
   * Schritt gezeichnet bleibt, wird das Overlay deshalb NICHT neu gebaut,
   * sondern nur nachgeführt (Aussparung, Ring, Popup-Position). Ein Neubau je
   * Takt hatte zwei sichtbare Folgen: Der Ring startete seine Puls-Animation
   * jedes Mal von vorn (Flackern), und weil auch die Fusszeile neue Knöpfe
   * bekam, lagen ``mousedown`` und ``mouseup`` auf verschiedenen Elementen --
   * „Weiter“/„Zurück“ lösten dadurch oft gar kein ``click`` aus. Ein Neubau
   * findet nur statt, wenn sich Schritt, Anker-Verfügbarkeit oder
   * Blockier-Modus ändern (:func:`paintKey`).
   */
  function paint() {
    const step = current();
    const root = byId("tour-root");
    if (!step || !root) return;
    if (byId("modal-root").children.length) { clear(root); t.painted = null; return; }

    const anchor = step.anchor ? document.querySelector(step.anchor) : null;
    let missing = false;
    if (step.anchor && !anchor) {
      if (!t.anchorSince) t.anchorSince = Date.now();
      if (Date.now() - t.anchorSince < ANCHOR_TIMEOUT_MS) {
        clear(root);
        t.painted = null;
        return;
      }
      missing = true;
    } else {
      t.anchorSince = 0;
    }

    const key = paintKey(step, anchor, missing);
    const reuse = !!(t.painted && t.painted.key === key && t.painted.pop.isConnected);
    if (!reuse) clear(root);
    document.documentElement.setAttribute("data-tour-active", "1");
    // Vom unteren Rand belegter Platz (Demo-Banner). Hier gesetzt statt in
    // position(), weil das mittige Popup ohne Anker gar nicht dort vorbeikommt --
    // es braucht die Zahl aber genauso, sonst rutscht es hinter den Banner.
    document.documentElement.style.setProperty("--tour-bottom-inset", `${bottomInset()}px`);
    const rect = anchor ? anchor.getBoundingClientRect() : null;
    // Bei „simulate“ blockt der Scrim Klicks außerhalb des Ankers: Ein Klick an
    // die falsche Stelle würde die Aufzeichnung und das Gesehene auseinander-
    // laufen lassen. Sonst bleibt die Anwendung voll bedienbar.
    const blocking = step.action === "simulate";
    // Manche Schritte brauchen mehr als eine Stelle: Der Ablehnungs-Schritt
    // bittet zuerst darum, den neuen Schritt im Graph zu wählen, und erst dann
    // die Bindung im Tab zu setzen. Der Anker kann aber nur EINE Stelle zeigen
    // -- ohne die Zusatzbereiche läge der Graph unter dem blockenden Scrim und
    // der Schritt liesse sich gar nicht auswählen. ``step.also`` nennt daher
    // weitere Bereiche, die frei bedienbar bleiben.
    const rects = rect ? [rect] : [];
    (step.also || []).forEach((sel) => {
      const extra = document.querySelector(sel);
      if (extra) rects.push(extra.getBoundingClientRect());
    });
    const scrimStyle = rects.length ? cutoutStyle(rects) : "";
    const ringStyle = rect
      ? `left:${rect.left - 6}px;top:${rect.top - 6}px;` +
        `width:${rect.width + 12}px;height:${rect.height + 12}px`
      : "";

    if (reuse) {
      // Nur nachführen: dieselben Knoten behalten, damit Ring-Animation und
      // Klick-Ziele erhalten bleiben.
      t.painted.scrim.setAttribute("style", scrimStyle);
      if (t.painted.ring) t.painted.ring.setAttribute("style", ringStyle);
      if (rect) position(t.painted.pop, rect, step.placement, true);
      return;
    }

    const scrim = el("div", {
      class: "tour-scrim" + (blocking ? " blocking" : ""),
      style: scrimStyle,
    });
    if (blocking) makeScrimScrollable(scrim);
    root.appendChild(scrim);
    let ring = null;
    if (rect) {
      ring = el("div", { class: "tour-ring", style: ringStyle });
      root.appendChild(ring);
    }
    scrollTargetsIntoView(rects);
    const pop = popup(step, rect, missing);
    root.appendChild(pop);
    t.painted = { key, scrim, ring, pop };
  }

  /**
   * Lässt den blockenden Scrim Rollbewegungen durch.
   *
   * Der Scrim nimmt bei ``simulate`` Zeigerereignisse an (er soll Fehlklicks
   * abfangen) und liegt als ``position: fixed``-Kind von ``<body>`` über allem.
   * Damit landet auch jedes Mausrad-/Wischereignis bei ihm -- und der Browser
   * rollt daraufhin seinen rollbaren Vorfahren, also das Dokument. Das Dokument
   * rollt in dieser Anwendung aber gar nicht: Gerollt wird ``.main``
   * (``styles.css``). Ergebnis: Die Seite stand fest, und ein Zielbereich
   * oberhalb des Sichtfensters -- etwa der Kontrollfluss -- war nicht mehr
   * erreichbar.
   *
   * Deshalb wird die Bewegung von Hand weitergereicht. Nur das Rollen; Klicks
   * bleiben geblockt, die Schutzwirkung des Scrims bleibt also erhalten.
   *
   * @param {HTMLElement} scrim Der blockende Scrim.
   */
  function makeScrimScrollable(scrim) {
    const scroller = () => document.querySelector(".main");
    scrim.addEventListener("wheel", (ev) => {
      const box = scroller();
      if (!box) return;
      box.scrollTop += ev.deltaY;
      box.scrollLeft += ev.deltaX;
      ev.preventDefault();
    }, { passive: false });
    // Touch: die Fingerbewegung selbst nachrechnen (ein `touchmove` auf dem
    // Scrim rollt sonst ebenso wenig wie das Mausrad).
    let lastY = 0, lastX = 0;
    scrim.addEventListener("touchstart", (ev) => {
      const p = ev.touches[0];
      if (p) { lastY = p.clientY; lastX = p.clientX; }
    }, { passive: true });
    scrim.addEventListener("touchmove", (ev) => {
      const box = scroller();
      const p = ev.touches[0];
      if (!box || !p) return;
      box.scrollTop += lastY - p.clientY;
      box.scrollLeft += lastX - p.clientX;
      lastY = p.clientY;
      lastX = p.clientX;
      ev.preventDefault();
    }, { passive: false });
  }

  /**
   * Kennung des gezeichneten Zustands.
   *
   * Ändert sie sich, muss das Overlay neu gebaut werden; bleibt sie gleich,
   * genügt das Nachführen der Positionen. Bewusst grob: Der Inhalt eines
   * Schritts ist statisch, veränderlich sind nur Schritt-Nummer, ob der Anker
   * gefunden wurde (Popup mittig vs. am Element) und der Blockier-Modus. Dass
   * ein Neurendern der Sicht den Anker-KNOTEN austauscht, ist unerheblich --
   * verwendet wird nur dessen Rechteck, und das wird ohnehin jeden Takt neu
   * gemessen.
   *
   * @param {object} step Aktueller Schritt.
   * @param {Element|null} anchor Gefundenes Anker-Element.
   * @param {boolean} missing Anker dauerhaft nicht auffindbar.
   * @returns {string} Vergleichbare Kennung.
   */
  function paintKey(step, anchor, missing) {
    return [
      t.tour ? t.tour.id : "",
      t.index,
      missing ? "1" : "0",
      anchor ? "a" : "-",
      step.action === "simulate" ? "b" : "-",
    ].join("|");
  }

  /**
   * Erzeugt die Aussparungen im Abdunkel-Overlay (eine je Bereich).
   *
   * Ein ``clip-path: polygon`` kennt keine getrennten Teilpfade. Mehrere Löcher
   * entstehen deshalb über „Brücken“: Nach jedem Loch kehrt der Pfad zum
   * Ursprung zurück und läuft von dort ins nächste. Weil jedes Loch entgegen
   * dem Umlaufsinn des Außenrechtecks umrundet wird, hebt es sich nach der
   * nonzero-Regel heraus; die Brücken selbst sind entartet (Hin- und Rückweg
   * auf derselben Linie) und damit unsichtbar.
   *
   * @param {DOMRect[]} rects Bildschirmrechtecke der freizulassenden Bereiche.
   * @returns {string} Inline-Style mit der clip-path-Aussparung.
   */
  function cutoutStyle(rects) {
    const pad = 6;
    const holes = rects.map((r) => {
      const x1 = Math.max(0, r.left - pad), y1 = Math.max(0, r.top - pad);
      const x2 = r.right + pad, y2 = r.bottom + pad;
      // Gegen den Uhrzeigersinn, das Außenrechteck läuft im Uhrzeigersinn.
      return `${x1}px ${y1}px, ${x1}px ${y2}px, ${x2}px ${y2}px, ` +
             `${x2}px ${y1}px, ${x1}px ${y1}px, 0 0`;
    });
    return "clip-path: polygon(" +
      "0 0, 100% 0, 100% 100%, 0 100%, 0 0, " + holes.join(", ") + ")";
  }

  /**
   * Rollt die Zielbereiche des Schritts ins Sichtfeld.
   *
   * Bekommt **alle** freigelassenen Bereiche, nicht nur den Anker: Ein Schritt
   * mit ``also`` fordert zu mehreren Stellen auf, und die müssen zusammen
   * sichtbar sein. Beim Ablehnungs-Schritt zentrierte das frühere Verhalten den
   * Anker (den Tab unten rechts) und schob damit den Kontrollfluss über den
   * oberen Rand -- also genau die Stelle, an der zuerst ein Schritt zu wählen
   * war.
   *
   * Passt die Gesamthöhe ins Fenster, wird der Verbund mittig gestellt; sonst
   * wird nur so weit gerollt, dass der obere Bereich anliegt (er kommt in der
   * Aufforderung zuerst). Gerollt wird ``.main`` -- das Dokument selbst rollt
   * in dieser Anwendung nicht.
   *
   * @param {DOMRect[]} rects Bildschirmrechtecke der Zielbereiche.
   */
  function scrollTargetsIntoView(rects) {
    if (!rects.length) return;
    const box = document.querySelector(".main");
    if (!box) return;
    const pad = 16;
    const top = Math.min(...rects.map((r) => r.top));
    const bottom = Math.max(...rects.map((r) => r.bottom));
    const usableBottom = window.innerHeight - bottomInset();
    if (top >= pad && bottom <= usableBottom - pad) return;   // passt schon

    let delta;
    if (bottom - top <= usableBottom - 2 * pad) {
      delta = (top + bottom) / 2 - usableBottom / 2;          // mittig
    } else {
      delta = top - pad;                                      // oben anlegen
    }
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    try {
      box.scrollBy({ top: delta, behavior: smooth ? "smooth" : "auto" });
    } catch (_e) {
      box.scrollTop += delta;                                 // ältere Engines
    }
  }

  /**
   * Baut das Popup des Schritts.
   *
   * @param {object} step Aktueller Schritt.
   * @param {DOMRect|null} rect Ankerrechteck (null = mittiges Popup).
   * @param {boolean} missing true, wenn der Anker nicht gefunden wurde.
   * @returns {HTMLElement} Das fertige Popup.
   */
  function popup(step, rect, missing) {
    const total = t.tour.steps.length;
    const box = el("div", {
      class: "tour-pop" + (rect && !missing ? "" : " centered"),
      role: "dialog",
      "aria-labelledby": "tour-pop-title",
    },
      el("div", { class: "tour-pop-h" },
        el("h3", { id: "tour-pop-title", tabindex: "-1" }, step.title),
        el("button", {
          class: "tour-x", type: "button", title: "Tour beenden (Esc)",
          "aria-label": "Tour beenden", onClick: () => stop(),
        }, "×")),
      // Der veränderliche Teil sitzt in einem eigenen Behälter, damit NUR er
      // rollt (tour.css). Kopf und Fusszeile bleiben dadurch immer sichtbar --
      // sonst rutschte „Weiter“ bei langem Text aus dem Fenster.
      el("div", { class: "tour-pop-b" },
        el("p", { class: "tour-body" }, step.body),
        step.hint ? el("p", { class: "tour-hint" }, step.hint) : null,
        missing ? el("p", { class: "tour-warn" },
          "Das zugehörige Element ist gerade nicht sichtbar.") : null,
        step.doc
          ? el("p", { class: "tour-doc" },
              el("a", { href: docUrl(step.doc), target: "_blank", rel: "noopener" },
                "Ausführlich nachlesen"))
          : null),
      el("div", { class: "tour-foot" },
        el("span", { class: "tour-count" }, `${t.index + 1} von ${total}`),
        el("span", { class: "tour-spacer" }),
        t.index > 0
          ? el("button", { class: "btn ghost small", onClick: prev }, "Zurück")
          : null,
        step.action === "none" || missing
          ? el("button", { class: "btn primary small", onClick: next },
              t.index === total - 1 ? "Fertig" : "Weiter")
          : el("button", {
              class: "btn ghost small", title: "Diesen Schritt überspringen",
              onClick: next,
            }, "Überspringen")));

    if (rect && !missing) position(box, rect, step.placement);
    // Fokus auf die Überschrift, damit Screenreader den neuen Schritt vorlesen
    // und die Tastaturbedienung im Popup startet.
    requestAnimationFrame(() => {
      const h = box.querySelector("#tour-pop-title");
      if (h) h.focus({ preventScroll: true });
    });
    return box;
  }

  /**
   * Platziert das Popup am Anker -- bevorzugt darunter, bei Platzmangel
   * darüber, und immer innerhalb des Fensters.
   *
   * Die Größe steht erst nach dem Einhängen fest, deshalb wird im nächsten
   * Frame nachgemessen und korrigiert.
   *
   * @param {HTMLElement} box Das Popup.
   * @param {DOMRect} r Ankerrechteck.
   * @param {string} [placement] Wunschseite ("top"/"bottom").
   */
  /**
   * Höhe des unten fest stehenden Demo-Banners (0, wenn keiner da ist).
   *
   * Der Banner der öffentlichen Demo (``#demo-banner``) klebt am unteren Rand
   * und trägt denselben ``z-index`` wie das Tour-Overlay -- weil er später ins
   * DOM kommt, malt er darüber. Genau dort sass die Fusszeile des Popups mit
   * „Weiter“, der Schritt war dadurch nicht abschliessbar.
   *
   * Statt an der Stapelreihenfolge zu drehen (dann läge das Popup zwar oben,
   * verdeckte aber die Rollen-Umschaltung), wird der Platz **freigehalten**:
   * Das Popup endet oberhalb des Banners, beide sind gleichzeitig bedienbar.
   *
   * @returns {number} Belegte Höhe am unteren Rand in Pixeln, inkl. Abstand.
   */
  function bottomInset() {
    const banner = byId("demo-banner");
    if (!banner) return 0;
    const r = banner.getBoundingClientRect();
    if (!r.height) return 0;                       // ausgeblendet -> kein Platzbedarf
    return Math.max(0, window.innerHeight - r.top + 8);
  }

  /**
   * Platziert das Popup am Anker -- bevorzugt darunter, bei Platzmangel darüber.
   *
   * Der nutzbare Bereich endet über dem Demo-Banner (siehe :func:`bottomInset`);
   * dieselbe Zahl bekommt auch das Stylesheet als ``--tour-bottom-inset``, damit
   * der Höhendeckel des Popups den Banner mit einrechnet.
   *
   * @param {HTMLElement} box Das Popup.
   * @param {DOMRect} r Ankerrechteck.
   * @param {string} placement "top" erzwingt oberhalb, sonst automatisch.
   * @param {boolean} [quiet] true beim reinen Nachführen eines bereits
   *   stehenden Popups: Dann entfällt das Ausblenden bis zur Messung -- sonst
   *   blinkte das Popup in jedem Takt (TICK_MS) einmal auf.
   */
  function position(box, r, placement, quiet) {
    if (!quiet) box.style.visibility = "hidden";
    requestAnimationFrame(() => {
      const pad = 12;
      const inset = bottomInset();
      document.documentElement.style.setProperty("--tour-bottom-inset", `${inset}px`);
      const usableBottom = window.innerHeight - inset;
      const w = box.offsetWidth, h = box.offsetHeight;
      const below = usableBottom - r.bottom;
      const wantTop = placement === "top" || (below < h + pad && r.top > h + pad);
      let top = wantTop ? r.top - h - pad : r.bottom + pad;
      let left = r.left + r.width / 2 - w / 2;
      left = Math.max(pad, Math.min(left, window.innerWidth - w - pad));
      top = Math.max(pad, Math.min(top, usableBottom - h - pad));
      box.style.left = `${left}px`;
      box.style.top = `${top}px`;
      box.style.visibility = "visible";
    });
  }

  // --- Öffentliche Schnittstelle ------------------------------------------

  return {
    maybeOffer,
    afterRender,
    intercept,
    start,
    stop,
    availableTours,
    isDone,
    savedProgress,
    /** @returns {boolean} true, solange eine Tour läuft. */
    get running() { return !!t.tour; },
    /** @returns {boolean} true im schreibfreien Modus (für das GUI-Abzeichen). */
    get sandboxed() { return t.sandbox; },
  };
})();
