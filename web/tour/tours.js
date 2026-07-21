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
//   also     optionale Liste weiterer Selektoren, die bedienbar bleiben.
//            Nur bei action:"simulate" wirksam, denn nur dort blockt der Scrim
//            alles ausserhalb der Aussparung. Noetig, sobald ein Schritt zwei
//            Stellen braucht (z. B. erst den Knoten im Graph waehlen, dann im
//            Tab binden) -- der Anker allein zeigt immer nur eine.
//   doc      optionaler Tiefen-Link in die Anleitungen (öffnet neuen Tab)
//
// Eine Tour: { id, role, title, subtitle, version, sandbox, mobile, steps }
//   version  hochzählen, wenn die Tour inhaltlich überarbeitet wurde -- dann
//            wird sie bestehenden Nutzern einmalig erneut angeboten
//   sandbox  true = die Tour arbeitet auf dem Tutorial-Beispielprozess und der
//            Client schaltet in den schreibfreien Modus (fixtures.js)
//   mobile   false = auf dem Smartphone nicht anbieten
// ---------------------------------------------------------------------------

/**
 * Beschriftung des Schritts, den die Modellierer-Tour einfügen lässt.
 *
 * MUSS mit ``LABEL_RESTURLAUB`` in ``core/tests/tour_fixture_build.py``
 * übereinstimmen: Die Engine findet den aufgezeichneten Knoten über diese
 * Beschriftung, um ihn auf den vom Nutzer getippten Text umzubenennen
 * (``applyLabel``). Laufen die beiden auseinander, bleibt der Knoten stumm auf
 * dem Fixture-Namen stehen. Der Wächter ``test_tour_insert_label_matches_fixture``
 * erzwingt die Gleichheit.
 */
const TOUR_NEW_STEP_LABEL = "Resturlaub prüfen";

const TOURS = [
  // -------------------------------------------------------------------------
  // Modellierer -- die Kern-Erfahrung des Produkts.
  // -------------------------------------------------------------------------
  {
    id: "modeler",
    role: "modeler",
    title: "Prozesse modellieren",
    subtitle: "Warum du hier nichts Ungültiges bauen kannst",
    // v2: Einstieg fachlich statt mechanisch erklärt und der eingefügte Schritt
    // von "Antragsteller informieren" zu "Resturlaub prüfen" geändert -- siehe
    // die Notiz in tour_fixture_build.py (build_stages).
    version: 2,
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
        // Bewusst keine Schrittzahl im Text: die Fusszeile des Popups zaehlt
        // ohnehin mit („3 von 9"), und eine im Text mitgeschleppte Zahl lief
        // beim ersten Umbau schon einmal auseinander (Text acht, Zaehler neun).
        body: "Du wirst gleich einen Arbeitsschritt zu einem Urlaubsantrag hinzufügen — und dabei absichtlich einen Fehler machen, den das Werkzeug nicht durchgehen lässt. Dauert nur ein paar Minuten.",
        hint: "Es wird nichts gespeichert. Du kannst jederzeit mit Esc abbrechen.",
        action: "none",
      },
      {
        id: "graph",
        view: "model",
        anchor: '[data-tour="model.graph"]',
        title: "Der Beispielprozess: ein Urlaubsantrag",
        body: "Von links nach rechts: Erika (Sachbearbeiterin) erfasst den Antrag, ihr Teamleiter Tom prüft ihn. Wichtig für gleich: „Antrag erfassen“ trägt die beantragten Urlaubstage ein — „Antrag prüfen“ liest sie. Wer welche Daten braucht, steht also im Modell.",
        hint: "Sieh dir die zwei Schritte kurz an. Ziehen verschiebt, das Mausrad zoomt.",
        action: "none",
      },
      {
        id: "insert",
        view: "model",
        anchor: '[data-tour="model.plus"][data-tour-src="start"]',
        title: "Einen Schritt einfügen",
        body: "Neue Anforderung: Bevor ein Antrag bearbeitet wird, soll Erika sehen, wie viel Resturlaub noch offen ist. Also fügen wir einen Schritt „" + TOUR_NEW_STEP_LABEL + "“ ein — ganz vorn, gleich hinter Start. Neue Schritte entstehen nur über das „+“ auf einer Verbindungslinie — deshalb kann dabei nie ein loses Ende entstehen.",
        hint: "Klicke auf das markierte „+“ hinter Start und lege einen Schritt „" + TOUR_NEW_STEP_LABEL + "“ an.",
        action: "simulate",
        sim: { stage: 1, applyLabel: true },
        advance: (ctx) => ctx.stage >= 1,
      },
      {
        id: "findings",
        view: "model",
        anchor: '[data-tour="model.findings"]',
        title: "Der Haken bleibt stehen",
        body: "Einen „Prüfen“-Knopf suchst du hier vergebens — den braucht es nicht. Jede Änderung wird geprüft, bevor sie überhaupt übernommen wird. Deshalb steht hier praktisch immer ein Haken. Aber was passiert, wenn man etwas Unsinniges versucht?",
        hint: "Weiter — jetzt kommt der interessante Teil.",
        action: "none",
      },
      {
        id: "reject",
        view: "model",
        anchor: '[data-tour="model.tab.data"]',
        title: "Der Versuch, etwas Unmögliches zu tun",
        body: "Um den Resturlaub zu prüfen, braucht Erika die beantragten Urlaubstage. Aber: Unser neuer Schritt steht ganz vorn — die Tage werden erst danach in „Antrag erfassen“ eingetragen. Er würde also etwas lesen, das es noch gar nicht gibt. Versuchen wir es trotzdem.",
        hint: "Wähle links den neuen Schritt aus, öffne den Tab Datenelemente und binde „Urlaubstage“ mit ⊕ lesend an ihn.",
        action: "simulate",
        // Der Schritt braucht zwei Stellen: erst den Knoten im Kontrollfluss
        // wählen, dann in der Palette binden. Ohne den Zusatzbereich läge der
        // Graph unter dem blockenden Scrim und der neue Schritt liesse sich
        // nicht auswählen. Die Palette muss GANZ frei sein -- der Tab ist nur
        // der Umschalter, gebunden wird über das ⊕ in der Liste darunter.
        also: ['[data-tour="model.graph"]', '[data-tour="model.palette"]'],
        sim: { reject: true },
        advance: (ctx) => ctx.rejected,
        doc: "Modellierer-Anleitung.md",
      },
      {
        id: "rejected",
        view: "model",
        anchor: null,
        title: "Abgelehnt — und genau das ist der Punkt",
        body: "Die Änderung wurde zurückgewiesen, mit Begründung: Der Schritt könnte die Urlaubstage lesen, bevor sie überhaupt jemand einträgt. Genau der Fehler, den wir eingebaut haben. Richtig wäre „" + TOUR_NEW_STEP_LABEL + "“ hinter „Antrag erfassen“. Wichtig: Dein Prozess ist dabei unverändert geblieben. Es gibt hier keinen halb kaputten Zwischenzustand, den du hinterher aufräumen müsstest.",
        hint: "Weiter.",
        action: "none",
      },
      {
        id: "staff",
        view: "model",
        anchor: '[data-tour="model.tab.res"]',
        title: "Wer macht die Arbeit?",
        body: "„" + TOUR_NEW_STEP_LABEL + "“ ist Arbeit für eine Sachbearbeiterin — die muss jemandem zugeordnet werden, sonst landet sie in keiner Aufgabenliste. Eingetragen wird dabei nicht unbedingt ein Name: Du kannst genauso eine Rolle oder eine Abteilung angeben. Läuft der Prozess später, landet die Aufgabe automatisch bei den passenden Personen — und bei ihrer Vertretung, wenn jemand im Urlaub ist.",
        hint: "Öffne den Tab Ressourcen und binde die Rolle „Sachbearbeiter“ mit ⊕ an den neuen Schritt.",
        action: "simulate",
        // Wie beim Ablehnungs-Schritt: das Bindungsziel ist der im Graph
        // gewählte Knoten, die Auswahl muss also erreichbar bleiben -- und die
        // Palette ganz, weil das ⊕ der Rolle unter dem Tab sitzt.
        also: ['[data-tour="model.graph"]', '[data-tour="model.palette"]'],
        sim: { stage: 2 },
        advance: (ctx) => ctx.stage >= 2,
      },
      {
        id: "release",
        view: "model",
        anchor: '[data-tour="model.release"]',
        title: "Freigeben — dann geht es los",
        body: "Solange du modellierst, ist der Prozess ein Entwurf: sichtbar, aber noch nicht benutzbar. Mit „Freigeben“ wird daraus die verbindliche Fassung, nach der ab jetzt gearbeitet wird. Sie lässt sich später nicht mehr überschreiben — Änderungen ergeben immer eine neue Fassung, damit laufende Vorgänge nicht unter den Füßen wegbrechen.",
        hint: "Musst du jetzt nicht — im Tutorial bleibt es beim Entwurf.",
        action: "none",
      },
      {
        id: "done",
        view: "model",
        anchor: null,
        title: "Das war’s",
        body: "Du hast einen Schritt eingefügt, ihm jemanden zugeordnet und gesehen, was passiert, wenn etwas nicht zusammenpasst: Es wird abgelehnt, statt später im Betrieb schiefzugehen. Der Beispielprozess verschwindet gleich wieder — gespeichert wurde nichts.",
        hint: "Mehr dazu? Die Modellierer-Anleitung zeigt jede Ansicht im Detail.",
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
    // v2: Texte fuer Erstnutzer vereinfacht (Fachjargon raus, Nutzen zuerst).
    version: 2,
    sandbox: false,
    mobile: true,
    steps: [
      {
        id: "welcome",
        view: null,
        anchor: null,
        title: "Willkommen",
        body: "ProcWorks sagt dir, welche Arbeit gerade bei dir liegt und was als Nächstes dran ist. Sehen wir uns kurz an, wo das steht.",
        hint: "Weiter — es dauert nur einen Moment.",
        action: "none",
      },
      {
        id: "nav",
        view: null,
        anchor: '[data-tour="nav.tasks"]',
        title: "Hier liegt deine Arbeit",
        body: "„Meine Aufgaben“ ist deine Liste: alles, was gerade auf dich wartet. Auch Aufgaben, die du für jemanden übernimmst, der im Urlaub ist.",
        hint: "Klicke links im Menü auf Meine Aufgaben.",
        action: "click",
        advance: (ctx) => ctx.state.view === "tasks",
      },
      {
        id: "list",
        view: "tasks",
        anchor: '[data-tour="tasks.list"]',
        title: "Was zuerst dran ist, steht oben",
        body: "Die Liste sortiert sich selbst: Was am dringendsten ist, rutscht nach oben, Überfälliges steht ganz vorn. Daneben steht in Farbe der Grund — etwa „überfällig“ oder „wird knapp“. Du musst also nicht selbst abwägen, womit du anfängst.",
        hint: "Ein Klick auf eine Zeile öffnet die Aufgabe, „Erledigen“ schließt sie ab.",
        action: "none",
        doc: "Mitarbeiter-Anleitung.md",
      },
      {
        id: "absence",
        view: "tasks",
        anchor: '[data-tour="tasks.absence"]',
        title: "Wenn du im Urlaub bist",
        body: "Trag hier ein, von wann bis wann du weg bist. Deine Vertretung sieht deine Aufgaben dann zusätzlich in ihrer Liste — deine eigene Liste behältst du trotzdem. So bleibt nichts liegen, nur weil jemand nicht da ist.",
        hint: "Das war’s — mehr brauchst du für den Anfang nicht.",
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
    // v2: Texte fuer Erstnutzer vereinfacht (Fachjargon raus, Nutzen zuerst).
    version: 2,
    sandbox: false,
    mobile: true,
    steps: [
      {
        id: "welcome",
        view: null,
        anchor: null,
        title: "Willkommen",
        body: "Als Administrator willst du drei Dinge wissen: Sind die Daten gesichert? Kommen die E-Mails an? Und wie setze ich das System für eine Schulung zurück? Alles drei steht auf einer Seite.",
        hint: "Weiter — es dauert nur einen Moment.",
        action: "none",
      },
      {
        id: "nav",
        view: null,
        anchor: '[data-tour="nav.admin"]',
        title: "Alles an einer Stelle",
        body: "Was den laufenden Betrieb betrifft, findest du unter „Administration“ — du musst nichts zusammensuchen.",
        hint: "Klicke links im Menü auf Administration.",
        action: "click",
        advance: (ctx) => ctx.state.view === "admin",
      },
      {
        id: "backups",
        view: "admin",
        anchor: '[data-tour="admin.backups"]',
        title: "Sind die Daten gesichert?",
        body: "Eine Sicherung läuft jede Nacht automatisch. Aufbewahrt werden standardmäßig die letzten 14 Tage, dazu 8 Wochen- und 6 Monatsstände — Älteres räumt das System selbst weg. Diese Seite zeigt nur an, wann zuletzt gesichert wurde; an die Sicherungen selbst kommt sie nicht heran.",
        hint: "Weiter.",
        action: "none",
        doc: "Betriebs-Backup-Leitfaden.md",
      },
      {
        id: "mail",
        view: "admin",
        anchor: '[data-tour="admin.mail"]',
        title: "Kommen die E-Mails an?",
        body: "Benachrichtigungen werden erst vorgemerkt und dann verschickt. Klappt der Versand nicht, versucht das System es später noch einmal. Gibt es endgültig auf, siehst du die Mail hier — sie geht nie still verloren.",
        hint: "Weiter.",
        action: "none",
      },
      {
        id: "maintenance",
        view: "admin",
        anchor: '[data-tour="admin.maintenance"]',
        title: "Zurücksetzen für eine Schulung",
        body: "Zwei Knöpfe, beide räumen vorher komplett auf: „Beispieldaten laden“ stellt den Schulungsstand her (zwei Prozesse, eine Organisation, drei laufende Vorgänge), „Auf Null zurücksetzen“ hinterlässt ein leeres System. Vorsicht: Beides löscht alles Vorhandene unwiderruflich — auch die Prozesse, die du modelliert hast.",
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
    // v2: Texte fuer Erstnutzer vereinfacht (Fachjargon raus, Nutzen zuerst).
    version: 2,
    sandbox: false,
    mobile: true,
    steps: [
      {
        id: "nav",
        view: null,
        anchor: '[data-tour="nav.monitor"]',
        title: "Willkommen",
        body: "Du kannst hier zusehen, wie die Arbeit läuft, ohne selbst etwas zu ändern. Unter „Monitoring“ steht alles, was gerade unterwegs ist.",
        hint: "Klicke links im Menü auf Monitoring.",
        action: "click",
        advance: (ctx) => ctx.state.view === "monitor",
      },
      {
        id: "instances",
        view: "monitor",
        anchor: '[data-tour="monitor.instances"]',
        title: "Wo steht gerade was?",
        body: "Jede Zeile ist ein laufender Vorgang — zum Beispiel ein einzelner Urlaubsantrag — und zeigt, bei welchem Schritt er gerade hängt. Schließt irgendwo jemand eine Aufgabe ab, aktualisiert sich die Liste von selbst. Neu laden musst du nie.",
        hint: "Das war’s — mehr gibt es für den Anfang nicht zu wissen.",
        action: "none",
      },
    ],
  },
];

// Rangfolge, in der bei mehreren Rollen die anzubietende Tour gewählt wird.
// Die inhaltsreichste zuerst; die übrigen bietet der Abschluss-Schritt an.
const TOUR_ROLE_ORDER = ["modeler", "admin", "operator", "viewer"];
