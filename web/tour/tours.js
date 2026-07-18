// SPDX-License-Identifier: BUSL-1.1
// ---------------------------------------------------------------------------
// Inhalte der geführten Tour (docs/Tutorial-Konzept.md).
//
// Reine DATEN -- kein Verhalten. Die Engine (engine.js) kennt keinen einzigen
// dieser Texte, und diese Datei ruft nichts auf. Wer eine Tour ändert oder eine
// neue anlegt, fasst ausschließlich diese Datei an.
//
// Ein Schritt (TourStep):
//   id       stabile Kennung (Fortschritt, Tests)
//   view     Sicht, in der der Schritt spielt (null = ansichtsunabhängig)
//   anchor   CSS-Selektor des hervorgehobenen Elements, per Konvention über
//            [data-tour="…"]. null = mittiges Popup ohne Spotlight.
//   title    Überschrift des Popups
//   body     ein bis drei Sätze Fließtext
//   hint     Handlungsaufforderung (fett dargestellt), nennt Beschriftungen
//            wörtlich, damit der Blick sie findet
//   action   "none" (nur Weiter) | "click" (echter, lesender Klick)
//            | "simulate" (schreibender Klick -- der Sandkasten fängt ihn ab)
//   advance  Fortschrittsbedingung (ctx) => bool. Wird nach jedem render()
//            ausgewertet -- die Tour erkennt das ERGEBNIS, nicht den Weg dorthin.
//            Fehlt sie, geht es nur über den Weiter-Knopf.
//   sim      nur bei action:"simulate" -- was der Sandkasten auf den
//            schreibenden Aufruf antwortet (siehe engine.js, applySimulation)
//   doc      optionaler Tiefen-Link in die Anleitungen (öffnet neuen Tab)
//
// Eine Tour: { id, role, title, subtitle, version, sandbox, mobile, steps }
//   version  hochzählen, wenn die Tour inhaltlich überarbeitet wurde -- dann
//            wird sie bestehenden Nutzern einmalig erneut angeboten
//   sandbox  true = die Tour arbeitet auf dem Tutorial-Beispielprozess und der
//            Client schaltet in den schreibfreien Modus (fixtures.js)
//   mobile   false = auf dem Smartphone nicht anbieten
// ---------------------------------------------------------------------------

/** Beschriftung des Schritts, den die Modellierer-Tour einfügen lässt. */
const TOUR_NEW_STEP_LABEL = "Antragsteller informieren";

const TOURS = [
  // -------------------------------------------------------------------------
  // Modellierer -- die Kern-Erfahrung des Produkts.
  // -------------------------------------------------------------------------
  {
    id: "modeler",
    role: "modeler",
    title: "Prozesse modellieren",
    subtitle: "Warum du hier nichts Ungültiges bauen kannst",
    version: 1,
    sandbox: true,
    // Modellieren ist auf dem Smartphone bewusst zweitrangig (keine
    // Bindungs-Palette), deshalb wird diese Tour dort nicht angeboten.
    mobile: false,
    steps: [
      {
        id: "welcome",
        view: "model",
        anchor: null,
        title: "Willkommen bei ProcWorks",
        body: "In acht kurzen Schritten zeige ich dir, wie hier modelliert wird — und warum das Werkzeug dich gar nicht erst etwas Kaputtes bauen lässt. Wir arbeiten dabei auf einem Beispielprozess, der nur in deinem Browser existiert.",
        hint: "Es wird nichts gespeichert. Du kannst jederzeit mit Esc abbrechen.",
        action: "none",
      },
      {
        id: "graph",
        view: "model",
        anchor: '[data-tour="model.graph"]',
        title: "Der Kontrollfluss",
        body: "Die Schritte liegen auf einer Achse, Verzweigungen fächern symmetrisch auf und laufen wieder zusammen. Ziehen verschiebt die Ansicht, das Mausrad zoomt.",
        hint: "Sieh dich kurz um.",
        action: "none",
      },
      {
        id: "insert",
        view: "model",
        anchor: '[data-tour="model.plus"][data-tour-src="start"]',
        title: "Einen Schritt einfügen",
        body: "Neue Schritte entstehen nur über geführte Operationen — deshalb kann nie ein loses Ende oder eine unerreichbare Stelle entstehen. Das „+“ sitzt auf der Kante, an der eingefügt wird.",
        hint: "Klicke auf das markierte „+“ hinter Start und lege einen Schritt „" + TOUR_NEW_STEP_LABEL + "“ an.",
        action: "simulate",
        sim: { stage: 1, applyLabel: true },
        advance: (ctx) => ctx.stage >= 1,
      },
      {
        id: "findings",
        view: "model",
        anchor: '[data-tour="model.findings"]',
        title: "Korrektheit, laufend",
        body: "Einen „Validieren“-Knopf gibt es bewusst nicht. Hier steht im Normalfall dauerhaft ein Haken — weil jede Änderung schon vor dem Speichern gegen den vollständigen Regelkatalog geprüft wird. Was passiert also, wenn man es doch versucht?",
        hint: "Weiter — jetzt kommt der interessante Teil.",
        action: "none",
      },
      {
        id: "reject",
        view: "model",
        anchor: '[data-tour="model.tab.data"]',
        title: "Der Versuch, etwas Unmögliches zu tun",
        body: "Der neue Schritt liegt gleich hinter „Start“ — also vor der Stelle, an der die Urlaubstage überhaupt erfasst werden. Trotzdem versuchen wir jetzt, ihn diese Zahl lesen zu lassen.",
        hint: "Wähle links den neuen Schritt aus, öffne den Tab Datenelemente und binde „Urlaubstage“ mit ⊕ lesend an ihn.",
        action: "simulate",
        sim: { reject: true },
        advance: (ctx) => ctx.rejected,
        doc: "Modellierer-Anleitung.md",
      },
      {
        id: "rejected",
        view: "model",
        anchor: null,
        title: "Abgelehnt — und genau das ist der Punkt",
        body: "Der Kern hat die Änderung zurückgewiesen: „mandatory input 'Urlaubstage' may be read before it is written on some execution path“ (Regel D1). Das Modell ist dabei unverändert gültig geblieben — es gibt hier keinen Zustand, in dem etwas halb kaputt ist. Genau das meint „Correctness by Construction“.",
        hint: "Weiter.",
        action: "none",
      },
      {
        id: "staff",
        view: "model",
        anchor: '[data-tour="model.tab.res"]',
        title: "Bearbeiter binden",
        body: "Wer einen Schritt bearbeitet, wird nicht als Person eingetragen, sondern als Regel: eine Rolle, eine Abteilung oder eine konkrete Person. Zur Laufzeit löst der Kern daraus die Arbeitslisten auf — inklusive Vertretung.",
        hint: "Öffne den Tab Ressourcen und binde die Rolle „Sachbearbeiter“ mit ⊕ an den neuen Schritt.",
        action: "simulate",
        sim: { stage: 2 },
        advance: (ctx) => ctx.stage >= 2,
      },
      {
        id: "release",
        view: "model",
        anchor: '[data-tour="model.release"]',
        title: "Freigeben",
        body: "Die Freigabe macht aus dem Entwurf eine ausführbare, unveränderliche Revision. Danach lassen sich Instanzen starten; Änderungen laufen über eine neue Revision.",
        hint: "Nicht nötig — im Tutorial bleibt es beim Entwurf.",
        action: "none",
      },
      {
        id: "done",
        view: "model",
        anchor: null,
        title: "Das war’s",
        body: "Du kennst jetzt den Kern: einfügen, binden, freigeben — und der Regelkatalog passt bei jedem Schritt auf. Der Beispielprozess verschwindet gleich wieder, gespeichert wurde nichts.",
        hint: "Tiefer einsteigen? Die Modellierer-Anleitung führt jede Sicht im Detail vor.",
        action: "none",
        doc: "Modellierer-Anleitung.md",
      },
    ],
  },

  // -------------------------------------------------------------------------
  // Bearbeiter -- rein lesend, deshalb ohne Sandkasten auf echten Daten.
  // -------------------------------------------------------------------------
  {
    id: "operator",
    role: "operator",
    title: "Aufgaben erledigen",
    subtitle: "Deine Liste sortiert sich selbst",
    version: 1,
    sandbox: false,
    mobile: true,
    steps: [
      {
        id: "welcome",
        view: null,
        anchor: null,
        title: "Willkommen",
        body: "Drei kurze Schritte, dann weißt du, wie du deine Aufgaben abarbeitest.",
        hint: "Los geht’s.",
        action: "none",
      },
      {
        id: "nav",
        view: null,
        anchor: '[data-tour="nav.tasks"]',
        title: "Deine Startseite",
        body: "„Meine Aufgaben“ zeigt alles, was gerade dir zugeordnet ist — auch das, was du in Vertretung übernimmst.",
        hint: "Klicke auf Meine Aufgaben.",
        action: "click",
        advance: (ctx) => ctx.state.view === "tasks",
      },
      {
        id: "list",
        view: "tasks",
        anchor: '[data-tour="tasks.list"]',
        title: "Die Reihenfolge hat einen Grund",
        body: "Sortiert wird nach Zeit-Kritikalität: Was seine Soll-Zeit zu reißen droht, steigt von selbst nach oben, Überfälliges steht ganz oben. Das Band daneben sagt dir, warum — es ist keine Blackbox.",
        hint: "Ein Klick auf eine Zeile öffnet die Aufgabe.",
        action: "none",
        doc: "Mitarbeiter-Anleitung.md",
      },
      {
        id: "absence",
        view: "tasks",
        anchor: '[data-tour="tasks.absence"]',
        title: "Urlaub eintragen",
        body: "Trägst du hier eine Abwesenheit ein, bekommt deine Vertretung deine Aufgaben zusätzlich — du behältst sie trotzdem. Es kann also nie passieren, dass eine Aufgabe niemandem gehört.",
        hint: "Das war’s schon.",
        action: "none",
        doc: "Mitarbeiter-Anleitung.md",
      },
    ],
  },

  // -------------------------------------------------------------------------
  // Administrator -- Betriebssichten, rein lesend.
  // -------------------------------------------------------------------------
  {
    id: "admin",
    role: "admin",
    title: "Betrieb im Blick",
    subtitle: "Sicherung, Zustellung, Wartung",
    version: 1,
    sandbox: false,
    mobile: true,
    steps: [
      {
        id: "welcome",
        view: null,
        anchor: null,
        title: "Willkommen",
        body: "Vier Schritte durch die Betriebssichten.",
        hint: "Los geht’s.",
        action: "none",
      },
      {
        id: "nav",
        view: null,
        anchor: '[data-tour="nav.admin"]',
        title: "Administration",
        body: "Alles, was den laufenden Betrieb betrifft, liegt in einer Sicht.",
        hint: "Klicke auf Administration.",
        action: "click",
        advance: (ctx) => ctx.state.view === "admin",
      },
      {
        id: "backups",
        view: "admin",
        anchor: '[data-tour="admin.backups"]',
        title: "Datensicherung",
        body: "Die Sicherung läuft täglich von selbst, mit Großvater-Vater-Sohn-Aufbewahrung. Diese Ansicht liest nur mit — sie zeigt Stand und Historie, ohne je in die Sicherungen selbst zu greifen.",
        hint: "Weiter.",
        action: "none",
        doc: "Betriebs-Backup-Leitfaden.md",
      },
      {
        id: "mail",
        view: "admin",
        anchor: '[data-tour="admin.mail"]',
        title: "E-Mail-Ausgang",
        body: "Benachrichtigungen gehen über eine dauerhafte Warteschlange: Erst wird eingereiht, dann zugestellt, Fehlversuche werden wiederholt. Was endgültig scheitert, landet hier sichtbar — statt still verloren zu gehen.",
        hint: "Weiter.",
        action: "none",
      },
      {
        id: "maintenance",
        view: "admin",
        anchor: '[data-tour="admin.maintenance"]',
        title: "Wartung",
        body: "Hier lädst du die Beispieldaten oder setzt das System für eine Schulung zurück. Achtung: Der Reset löscht Laufzeitdaten.",
        hint: "Das war’s.",
        action: "none",
        doc: "Windows-Server-Setup.md",
      },
    ],
  },

  // -------------------------------------------------------------------------
  // Leser -- die kürzeste Tour.
  // -------------------------------------------------------------------------
  {
    id: "viewer",
    role: "viewer",
    title: "Prozesse beobachten",
    subtitle: "Live sehen, wo alles steht",
    version: 1,
    sandbox: false,
    mobile: true,
    steps: [
      {
        id: "nav",
        view: null,
        anchor: '[data-tour="nav.monitor"]',
        title: "Willkommen",
        body: "Zwei Schritte, dann kennst du deine Sicht.",
        hint: "Klicke auf Monitoring.",
        action: "click",
        advance: (ctx) => ctx.state.view === "monitor",
      },
      {
        id: "instances",
        view: "monitor",
        anchor: '[data-tour="monitor.instances"]',
        title: "Aktive Instanzen",
        body: "Jeder laufende Vorgang mit seinem aktuellen Schritt. Die Ansicht aktualisiert sich selbst, sobald irgendwo jemand eine Aufgabe abschließt — Neuladen ist nie nötig.",
        hint: "Das war’s.",
        action: "none",
      },
    ],
  },
];

// Rangfolge, in der bei mehreren Rollen die anzubietende Tour gewählt wird.
// Die inhaltsreichste zuerst; die übrigen bietet der Abschluss-Schritt an.
const TOUR_ROLE_ORDER = ["modeler", "admin", "operator", "viewer"];
