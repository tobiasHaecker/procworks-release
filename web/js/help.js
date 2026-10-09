// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - UI-Bausteine und Hilfe.
 *
 * Wiederverwendbare UI-Bausteine und die kontextsensitive Hilfe mit dem
 * Glossar der Regel-Codes.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// Wiederverwendbare UI-Bausteine
// --------------------------------------------------------------------------

function emptyState(text) { return el("div", { class: "empty" }, text); }
function kpi(label, value) { return el("div", { class: "kpi" }, el("div", { class: "label" }, label), el("div", { class: "value" }, String(value))); }

/**
 * Baut eine Standardtabelle aus Kopfzeilen und Zeilen.
 *
 * Jede Zelle erhaelt zusaetzlich ein `data-label`-Attribut mit dem Text ihrer
 * Spaltenueberschrift. Auf dem Desktop ist das Attribut wirkungslos; in der
 * mobilen Ansicht (styles.css, Breakpoint <= 720px) rendert die Tabelle als
 * gestapelte Karten und blendet die Kopfzeile aus – dort dient `data-label`
 * als vorangestellte Feldbeschriftung, sodass jede Zeile (z. B. eine Aufgabe)
 * ohne horizontales Scrollen lesbar bleibt. Leere Ueberschriften (etwa die
 * Aktionsspalte mit dem „Erledigen"-Knopf) tragen bewusst kein Label, damit die
 * Zelle mobil die volle Breite nutzt.
 *
 * Alle Tabellen laufen hierueber: Tabellen, die ``el("table")`` direkt bauten
 * (Instanzliste, Eskalationen, Zustaendigkeit, Migration), zeigten mobil
 * Karten ohne „Instanz/Schema/Status“ (Waechter
 * ``test_every_table_is_built_by_the_shared_helper``).
 *
 * @param {string[]} headers Spaltenueberschriften (leere Strings erlaubt).
 * @param {Array<Array>} rows Zeilen; jede ein Array von Zellen (Node oder Text).
 * @param {(i:number)=>(string|object|null)} [rowAttrs] je Zeilenindex eine
 *   CSS-Klasse (String) oder Attribute (Objekt, z. B. ``{class, onClick}``).
 * @param {object} [tableAttrs] Attribute der Tabelle (z. B. ``{class}``).
 * @returns {Node} Das <table>-Element.
 */
function table(headers, rows, rowAttrs, tableAttrs) {
  const attrsOf = (i) => {
    const a = rowAttrs ? rowAttrs(i) : null;
    if (a == null || a === "") return null;
    return typeof a === "string" ? { class: a } : a;
  };
  return el("table", tableAttrs || null,
    el("thead", null, el("tr", null, ...headers.map((h) => el("th", null, h)))),
    el("tbody", null, ...rows.map((r, i) => el("tr", attrsOf(i),
      ...r.map((c, j) => {
        const label = headers[j];
        const attrs = label ? { "data-label": String(label) } : null;
        return el("td", attrs, c instanceof Node ? c : String(c));
      })))));
}

// --------------------------------------------------------------------------
// View: Hilfe (kontextsensitive In-App-Hilfe + Glossar der Regel-Codes)
// --------------------------------------------------------------------------

// Public documentation targets. The source repository is private, so links must
// not point at it: the modeller guide is published on the website (procworks.de),
// customer guides live in the public release repo (procworks-release). docUrl()
// resolves a doc filename to the right public URL for the help view.
const SITE_DOCS = "https://procworks.de/docs/";
const RELEASE_DOCS = "https://github.com/tobiasHaecker/procworks-release/blob/main/docs/";
const DISCLAIMER_URL = "https://github.com/tobiasHaecker/procworks-release/blob/main/DISCLAIMER.md";
const DOC_URLS = {
  "Modellierer-Anleitung.md": SITE_DOCS + "modellierer-anleitung.html",
  // Verlinkt werden nur Dokumente, die öffentlich bereitstehen (Website oder
  // Kunden-Repo).
  "README.md": SITE_DOCS,
  "Mitarbeiter-Anleitung.md": RELEASE_DOCS + "Mitarbeiter-Anleitung.md",
  "Windows-Server-Setup.md": RELEASE_DOCS + "Windows-Server-Setup.md",
  "Integrations-Leitfaden.md": RELEASE_DOCS + "Integrations-Leitfaden.md",
  "Betriebs-Backup-Leitfaden.md": RELEASE_DOCS + "Betriebs-Backup-Leitfaden.md",
};
// Resolve a documentation filename to its public URL (falls back to the website).
function docUrl(doc) { return DOC_URLS[doc] || (SITE_DOCS + doc); }

// Short purpose of each navigation view (mirrors VIEW_META plus a one-liner of
// what the user actually does there).
const HELP_VIEWS = [
  ["\u25A3 Modellieren", "Schritte \u00FCber \u201E+\u201C einf\u00FCgen (seriell / parallel / bedingt), umbenennen, entfernen, Befunde pr\u00FCfen, freigeben."],
  ["\u2630 Datensicht", "Datenelemente anlegen, bearbeiten/l\u00F6schen und je Aktivit\u00E4t Lesen/Schreiben verbinden (Datenfluss D)."],
  ["\u265F Ressourcensicht", "Organisation (Rollen, Einheiten, Agenten) und Bearbeiterregeln je Schritt (Z/A)."],
  ["\u25B6 Ausf\u00FChrung", "Freigegebene Prozesse starten und die Schritte eines Vorgangs abarbeiten. Verzweigungen entscheidet das System anhand der erfassten Daten. Modellierer starten Entw\u00FCrfe als Test-Instanz."],
  ["\u2630 Meine Aufgaben", "Pers\u00F6nliche Arbeitsliste \u2013 Aufgaben mit \u201EErledigen\u201C abschlie\u00DFen."],
  ["\u2697 Pr\u00FCfinstanz", "Nur Modellierer und Administratoren: einen Entwurf als Test-Instanz durchspielen \u2013 alle Aufgaben an einer Stelle, ohne Mails und Eskalationen."],
  ["\u2609 Monitoring", "Live-Status aktiver Instanzen, Prozesslandkarte, Inzidente externer Aufgaben."],
  ["\u21C4 Integration", "Connectoren, externe Datenbindung, Automatik (External-Task / HTTP-Push), Webhooks."],
  ["\u2699 Administration", "Nur f\u00FCr Administratoren: Benutzer, Datensicherung, E-Mail-Ausgang, Wartung (Zur\u00FCcksetzen) und Beispieldaten."],
  ["\u24D8 Hilfe", "Diese Seite: Sichten, Schnellstart je Rolle, Glossar der Regel-Codes und die gef\u00FChrten Touren."],
];

// Role-oriented quick starts: each entry points at the matching how-to doc.
const HELP_QUICKSTART = [
  ["Modellierer", "Prozess erstellen, Daten/Bearbeiter verdrahten, testen, freigeben.", "Modellierer-Anleitung.md"],
  ["Sachbearbeiter", "Anmelden, eigene Aufgaben sehen und erledigen.", "Mitarbeiter-Anleitung.md"],
  ["Administrator", "Installation, Logins, Betrieb (Update/Backup), Beispieldaten.", "Windows-Server-Setup.md"],
  ["Integrator", "Fremdsysteme \u00FCber die offene /v1-Schnittstelle anbinden.", "Integrations-Leitfaden.md"],
];

// Glossary of the correctness rule codes that surface in the findings list and
// error toasts. Grouped by family so a user can look up exactly what e.g. "D1"
// or "B2" means. Kept in sync with validator.py.
const HELP_RULES = [
  ["Struktur & Kontrollfluss (K)", [
    ["K1", "Blockstruktur: jeder Split hat genau einen passenden Join desselben Typs."],
    ["K2", "Genau ein START und ein END; jede Aktivit\u00E4t hat genau eine Ein- und Ausgangskante."],
    ["K3", "Erreichbarkeit: kein isolierter Knoten, keine Sackgasse \u2013 alles liegt auf START\u2192END."],
    ["K4", "Sync-Kanten nur zwischen Aktivit\u00E4ten verschiedener UND-Zweige."],
    ["K5", "Soundness: jeder Zustand kann ordentlich zum Ende gelangen."],
    ["K6", "Strukturierte Schleifen (REPEAT-UNTIL mit definierter Abbruchbedingung)."],
    ["K7", "XOR-Pr\u00E4dikate decken den Wertebereich vollst\u00E4ndig und \u00FCberlappungsfrei ab."],
  ]],
  ["Datenfluss (D)", [
    ["D1", "Pflicht-Eingaben sind auf ALLEN Pfaden vor dem Lesen geschrieben."],
    ["D2", "Keine konkurrierenden Schreibzugriffe paralleler Zweige auf dasselbe Element."],
    ["D3", "Typkonformit\u00E4t von Quelle und Senke."],
    ["D4", "Optionale Eingaben d\u00FCrfen unversorgt bleiben; Join-Knoten tragen keine Daten."],
    ["D5", "Datenfluss wird live gepr\u00FCft und muss vor Freigabe sauber sein."],
    ["D6", "Beim Abschlie\u00DFen eines Schritts nur Werte, die dieser Schritt schreibt \u2013 nichts, was er nur liest, und keine unbekannten Felder."],
  ]],
  ["Externe Datenbindung (C)", [
    ["C1", "EXTERNE Elemente brauchen eine g\u00FCltige Connector-Bindung; INSTANCE-Elemente keine."],
    ["C2", "Das Schl\u00FCsselelement der Bindung ist ein existierendes INSTANCE-Element (nicht es selbst)."],
    ["C3", "Der gebundene Entit\u00E4tsname ist nicht leer."],
    ["C4\u2013C6", "SQL-Abfrage: Ergebnistyp passt zum Element, Filter passen zu ihren Spalten und Quellen, die Anzahl gelesener Zeilen ist eindeutig (eine Zeile, Zusammenfassung, erste nach Sortierung)."],
    ["C7\u2013C9", "Zur\u00FCckschreiben: Zielspalte passt zum Typ, Filter passen, genau eine Zeile wird getroffen (eindeutige Spalte)."],
  ]],
  ["Eingabemasken & Anzeige (U)", [
    ["U1", "Die Maske h\u00E4ngt an einem Aufgaben-Schritt und nennt existierende Datenelemente."],
    ["U2", "Felder sind stimmig: eindeutig, beschriftet, Bedienelement passt zum Typ, Pr\u00FCfregeln passen zum Feld."],
    ["U3", "Jedes Feld hat die passende Daten\u00ADbindung am Schritt (Eingabe schreibt, Anzeige liest)."],
    ["U4", "Beim Abschlie\u00DFen: Werte halten die Pr\u00FCfregeln der Maske ein (Grenzen, Muster, L\u00E4nge)."],
    ["U5", "Den Vorgang benennen h\u00F6chstens zwei vorhandene Vorgangsdaten."],
    ["U6", "Bezeichnungen sind h\u00F6chstens 200 Zeichen lang."],
  ]],
  ["Bearbeiter / Ressourcen (Z, A)", [
    ["Z1", "Bearbeiterregel ist syntaktisch g\u00FCltig und referenziert existierende Rollen/Einheiten."],
    ["Z2", "Die Regel ist erf\u00FCllbar \u2013 sie liefert mindestens einen Agenten."],
    ["Z3", "NodePerformingAgent(\u2026) verweist nur auf garantiert vorher laufende Schritte."],
    ["Z4", "Interaktive Schritte brauchen eine Bearbeiterregel; automatische nicht."],
    ["A1\u2013A3", "Zugeordneter Dienst (Template) ist vorhanden und typkonform an die Daten gebunden."],
  ]],
  ["Integration / Automatik (I)", [
    ["I1", "Automatik wohlgeformt: External-Task braucht Topic, HTTP-Push eine Endpunkt-Referenz."],
    ["I2", "Genau ein Automatik-Muster; automatisierte Bindung ist als automatisch markiert."],
    ["I3", "Parameter-Mapping zeigt auf existierende Datenelemente."],
    ["I4", "Topic/Endpunkt enthalten keine Inline-URL oder Zugangsdaten."],
    ["WH", "Webhook-Ziel: https, ein erlaubter \u00F6ffentlicher Server, keine Zugangsdaten in der Adresse."],
  ]],
  ["Komposition (H, F)", [
    ["H1\u2013H4", "Sub-Prozesse: nur freigegebene, gepinnte Version; typkonforme Schnittstelle; zyklenfrei."],
    ["F1\u2013F4", "Folgeprozesse: Ziel existiert freigegeben; typkonformes Handover; lose Kopplung bei ASYNC; Bedingungen lesen nur sicher gesetzte Daten."],
  ]],
  ["Zeit & Release (T, B)", [
    ["T1\u2013T2", "Fristen/Dauern wohldefiniert und entlang der Blockstruktur widerspruchsfrei."],
    ["T3", "Eskalation: nur an Aufgaben-Schritten mit Soll-Zeit, Stufen aufsteigend, Ziele finden jemanden."],
    ["B1", "Release-Reife: jeder Schritt ist ausf\u00FChrbar \u2013 automatische tragen einen Dienst, interaktive Maske und Bearbeiterzuordnung (der interaktive Teil ist B2)."],
    ["B2", "Release-Reife: jeder interaktive Schritt hat eine Bearbeiterzuordnung."],
    ["B3", "Release-Reife: alle Pflichtdaten sind gebunden, alle Pr\u00E4dikate spezifiziert."],
  ]],
  ["Benachrichtigung (N)", [
    ["N1", "E-Mail-Adressen und Postf\u00E4cher sind wohlgeformt."],
    ["N2", "Mails gibt es nur an Aufgaben-Schritten mit Bearbeiterzuordnung."],
    ["N3", "Jeder m\u00F6gliche Empf\u00E4nger ist erreichbar (Adresse bzw. Gruppenpostfach)."],
    ["N4", "Platzhalter im Mailtext verweisen auf sicher gesetzte Vorgangsdaten."],
  ]],
  ["Bearbeitungsschritte (OP)", [
    ["OP", "Vorbedingung einer Bearbeitung nicht erf\u00FCllt \u2013 etwa: das Element gibt es nicht (mehr), der Schritt liegt an der falschen Stelle, der Name fehlt."],
    ["USERS", "Benutzerverwaltung: Login schon vergeben, Rolle unbekannt, eigener oder letzter Administrator-Login nicht l\u00F6schbar."],
    ["PW", "Passwortregeln: Mindestl\u00E4nge, und das neue Passwort muss sich vom bisherigen unterscheiden."],
  ]],
  ["Laufzeit & Migration (R, M)", [
    ["R0", "Nur Entw\u00FCrfe sind editierbar; freigegebene Schemata sind unver\u00E4nderlich."],
    ["LC", "Lebenszyklus: nur ein Entwurf wird freigegeben, eine neue Revision entsteht nur aus einer freigegebenen Version."],
    ["M0", "Migration: die Instanz l\u00E4uft auf einer fr\u00FCheren Version genau dieses Schemas."],
    ["R1\u2013R2", "Ad-hoc-\u00C4nderungen nur zustandsvertr\u00E4glich und unter Erhalt aller K/D-Regeln."],
    ["M1\u2013M5", "Migration nur, wenn Ziel korrekt ist und der bisherige Verlauf vertr\u00E4glich bleibt."],
    ["EX", "Laufzeit: Die Aktion passt nicht zum Zustand des Vorgangs \u2013 etwa: jemand anderes hat den Schritt \u00FCbernommen, er ist angehalten oder gescheitert, oder man ist nicht zust\u00E4ndig."],
    ["BPMN", "BPMN-Import: Die Datei ist kein g\u00FCltiges BPMN oder enth\u00E4lt Elemente au\u00DFerhalb der unterst\u00FCtzten Block-Teilsprache (etwa ODER-Gateways oder Zwischenereignisse)."],
  ]],
  ["Modellhinweise (G, 7PMG)", [
    ["G1", "Hinweis: sehr gro\u00DFes Modell (>50 Knoten) \u2013 ggf. in Sub-Prozesse zerlegen."],
    ["G2", "Hinweis: hoher Gateway-Grad \u2013 Verzweigung vereinfachen."],
    ["G6", "Hinweis: hohe Verschachtelungstiefe (>5)."],
    ["G7", "Hinweis: Aktivit\u00E4t ohne sprechenden Namen."],
    ["G8", "Hinweis: interaktiver Schritt ohne Soll-Reaktionszeit, obwohl andere Schritte eine tragen \u2013 er nimmt nicht an der zeitbasierten Priorisierung teil."],
  ]],
];

function viewHelp() {
  const content = byId("content");
  clear(content);

  // Gefuehrte Tour -- ganz oben, weil sie der schnellste Einstieg ist. Zeigt je
  // Rolle des Angemeldeten eine Karte, inkl. Wiedereinstieg an der Stelle, an
  // der eine abgebrochene Tour stehengeblieben ist.
  content.appendChild(tourPanel());

  // Intro / principle.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "ProcWorks \u2013 Hilfe")),
    el("div", { class: "panel-b" },
      el("p", { class: "muted" },
        "ProcWorks h\u00E4lt jedes Modell \u201Ekorrekt per Konstruktion\u201C: Das Werkzeug ",
        "bietet nur Operationen an, die das Modell g\u00FCltig halten. Einen ",
        "\u201EValidieren\u201C-Knopf gibt es bewusst nicht \u2013 was noch zur Ausf\u00FChrbarkeit ",
        "fehlt, zeigt die Befunde-Liste laufend an."))));

  // The seven views.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Die Sichten im \u00DCberblick")),
    el("div", { class: "panel-b" },
      table(["Sicht", "Wof\u00FCr"], HELP_VIEWS.map(([n, d]) => [n, d])))));

  // Role-oriented quick starts with deep links to the how-to docs.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Schnellstart je Rolle")),
    el("div", { class: "panel-b" },
      table(["Rolle", "Ziel", "Anleitung"], HELP_QUICKSTART.map(([role, goal, doc]) => [
        role, goal,
        el("a", { href: docUrl(doc), target: "_blank", rel: "noopener" }, doc),
      ])))));

  // Glossary of rule codes, grouped by family.
  const glossary = el("div", { class: "panel-b" });
  for (const [group, rules] of HELP_RULES) {
    glossary.appendChild(el("div", { class: "sub-h" }, el("h3", null, group)));
    glossary.appendChild(table(["Code", "Bedeutung"], rules.map(([c, m]) => [
      el("span", { class: "pill pill-gray" }, c), m,
    ])));
  }
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Glossar der Regel-Codes"),
      el("span", { class: "spacer", style: "flex:1" }),
      el("span", { class: "muted", style: "font-size:12px" }, "erscheinen in Befunden & Fehlermeldungen")),
    glossary));

  // Further reading.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Weiterf\u00FChrend")),
    el("div", { class: "panel-b" },
      el("ul", { style: "margin:4px 0;padding-left:18px;line-height:1.7" },
        el("li", null, el("a", { href: docUrl("README.md"), target: "_blank", rel: "noopener" }, "Dokumentations-\u00DCbersicht (nach Rolle)")),
        el("li", null, el("a", { href: DISCLAIMER_URL, target: "_blank", rel: "noopener" }, "Haftungsausschluss"))))));
}

/**
 * Baut das Panel „Gefuehrte Tour" der Hilfe-Sicht.
 *
 * Listet alle Touren, die zur Rolle des Angemeldeten passen. Eine abgebrochene
 * Tour bietet zusaetzlich das Fortsetzen an der gemerkten Stelle an; eine
 * bereits durchlaufene laesst sich jederzeit erneut starten -- die Hilfe ist
 * der dauerhafte Zugang, nachdem das automatische Angebot verstummt ist.
 *
 * @returns {HTMLElement} Das Panel (leer, falls keine Tour passt).
 */
function tourPanel() {
  if (typeof Tour === "undefined") return el("div");
  const tours = Tour.availableTours();
  if (!tours.length) return el("div");
  const cards = el("div", { class: "tour-cards" });
  tours.forEach((tour) => {
    const at = Tour.savedProgress(tour);
    cards.appendChild(el("div", { class: "tour-card" },
      el("b", null, tour.title),
      el("span", null,
        `${tour.subtitle} · ${tour.steps.length} Schritte` +
        (Tour.isDone(tour) ? " · bereits gesehen" : "")),
      el("div", { class: "row", style: "gap:6px" },
        at
          ? el("button", { class: "btn small primary", onClick: () => Tour.start(tour.id, { resume: true }) },
              `Bei Schritt ${at + 1} fortsetzen`)
          : null,
        el("button", { class: at ? "btn small ghost" : "btn small primary",
          onClick: () => Tour.start(tour.id, { resume: false }) },
          at ? "Von vorn" : "Tour starten"))));
  });
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, "Geführte Tour"),
      el("span", { class: "sub" }, "Popups führen dich durch die wichtigsten Funktionen")),
    el("div", { class: "panel-b" },
      // Der Hinweis gilt nur der Modellierer-Tour (Sandkasten) -- ohne sie
      // nur der allgemeine Teil.
      el("p", { class: "muted", style: "font-size:13px;margin:0 0 12px" },
        tours.some((t) => t.sandbox)
          ? "Die Modellierer-Tour arbeitet auf einem Beispielprozess in deinem Browser – es wird dabei nichts gespeichert. "
          : "",
        "Abbrechen jederzeit mit Esc."),
      cards));
}

