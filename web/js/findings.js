// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Meldungskatalog.
 *
 * Der eine Katalog, der Regelbefunde, Engine-Ablehnungen und Importfehler
 * des Kerns aus ``code``/``params`` in deutschen Text uebersetzt
 * (``findingText``).
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// Meldungskatalog fuer Regelbefunde
// --------------------------------------------------------------------------

/**
 * Deutsche Texte je Befund-Code (``ValidationFinding.code``).
 *
 * Der Kern bleibt sprachneutral: Er liefert neben der technischen, englischen
 * ``message`` einen Code und die menschenlesbaren Teile (Schritt- und
 * Elementnamen, nie IDs) als ``params``. Formuliert wird ausschliesslich hier --
 * EIN Katalog fuer alle Anzeigestellen (Fehlermeldung, Befundliste, Karte,
 * Statusleiste, Marker am Knoten, Migrationsassistent), damit sie nicht
 * auseinanderlaufen. Frueher erschienen Ablehnungen englisch und mit interner
 * Knoten-ID („act_246“).
 *
 * Jeder Eintrag liefert ``{ text, hint }``: ``text`` sagt, was nicht geht,
 * ``hint`` (optional) was zu tun ist. Fehlt ein Code, faellt ``findingText``
 * auf die Kernmeldung zurueck; ``FINDING_FALLBACK_RULES`` im Waechter
 * dokumentiert, welche Regeln bewusst noch nicht uebersetzt sind.
 */
/** Namen der Objektarten fuer „gibt es nicht / gibt es schon“ (Code OP.*). */
const OP_KIND_NAMES = {
  node: "Der Schritt", data_element: "Das Datenelement", role: "Die Rolle", org_unit: "Die Abteilung",
  agent: "Die Person", follow_up: "Der Folgeprozess", activity_template: "Die Dienst-Vorlage", connector: "Der Connector",
  schema: "Der Prozess", template: "Die Vorlage", org_model: "Die Organisation",
};

/** Texte fuer „geht an dieser Knotenart nicht“ (Code OP.wrong-node-kind, Parameter ``what``). */
const OP_WRONG_KIND = {
  loop_decision: "Eine Wiederholungsbedingung gehört an ein Schleifenende.",
  sync: "Warte-Beziehungen verbinden nur Aufgaben-Schritte.",
  rename: "Nur Schritte und Teilprozesse lassen sich umbenennen.",
  move: "Nur Schritte und Teilprozesse lassen sich verschieben.",
  empty_branch: "Einen leeren Zweig gibt es nur an einer Entscheidung.",
  data_access: "Daten lassen sich nur an Aufgaben-Schritte binden.",
  input_mask: "Eine Eingabemaske gibt es nur an Aufgaben-Schritten.",
  service: "Ein Dienst lässt sich nur an Aufgaben-Schritte binden.",
  staff_rule: "Bearbeiter lassen sich nur Aufgaben-Schritten zuordnen.",
  subprocess_convert: "Nur ein Aufgaben-Schritt lässt sich in einen Teilprozess umwandeln.",
  subprocess: "Das ist kein Teilprozess-Schritt.",
  value_class: "Eine Wertklasse tragen nur Schritte und Teilprozesse.",
  automation: "Eine Automatik lässt sich nur an Aufgaben-Schritten einstellen.",
  priority: "Eine Priorität tragen nur Schritte und Teilprozesse.",
  mail: "Eine E-Mail-Benachrichtigung gibt es nur an Aufgaben-Schritten.",
  time_constraint: "Zeitvorgaben tragen nur Schritte und Teilprozesse.",
};

const FINDING_TEXTS = {
  "D1.read-before-write": (p) => ({
    text: `„${p.step}“ würde „${p.element}“ lesen, bevor es auf jedem Weg geschrieben wurde.`,
    hint: `Den Schritt hinter einen Schritt legen, der „${p.element}“ schreibt – oder die Bindung nicht als Pflicht setzen.`,
  }),
  "K2.start-count": (p) => ({ text: `Ein Prozess braucht genau einen Start, hier sind es ${p.count}.` }),
  "K2.end-count": (p) => ({ text: `Ein Prozess braucht genau ein Ende, hier sind es ${p.count}.` }),
  "K1.unbalanced": (p) => ({
    text: `Die Verzweigungen sind nicht vollständig: ${p.splits}× ${p.kind}, aber ${countLabel(Number(p.joins), "passende Zusammenführung", "passende Zusammenführungen")}.`,
    hint: "Jede Verzweigung braucht genau eine Zusammenführung derselben Art. Beim BPMN-Import zählt auch ein Schritt mit mehreren ausgehenden bzw. eingehenden Pfeilen als Verzweigung bzw. Zusammenführung.",
  }),
  "K1.wrong-join": (p) => ({
    text: `„${p.split}“ wird von „${p.join}“ geschlossen – das ist die falsche Art der Zusammenführung.`,
    hint: "Eine parallele Verzweigung schließt eine parallele Zusammenführung, eine Entscheidung eine Entscheidungs-Zusammenführung.",
  }),
  "K1.shared-join": (p) => ({
    text: `„${p.join}“ schließt zwei Verzweigungen zugleich – die Blöcke überkreuzen sich.`,
    hint: "Jeder Block muss vollständig innerhalb oder außerhalb eines anderen liegen.",
  }),
  "K1.unpaired": (p) => ({
    text: `Zu „${p.step}“ gibt es keine eindeutig passende Zusammenführung.`,
    hint: "Das Modell ist nicht sauber blockstrukturiert (typisch bei importiertem BPMN).",
  }),
  "K6.crosses-branch": (p) => ({
    text: `Die Schleife ab „${p.loop}“ überkreuzt die Verzweigung „${p.split}“: Anfang und Ende liegen nicht im selben Zweig.`,
    hint: "Eine Schleife muss ganz innerhalb eines Zweigs liegen oder die ganze Verzweigung umschließen (typisch bei importiertem BPMN).",
  }),
  "K3.unreachable": (p) => ({
    text: `„${p.step}“ ist vom Start aus nicht erreichbar.`,
    hint: "Typisch bei importiertem BPMN: der Schritt hängt an keiner Verbindung vom Start.",
  }),
  "K3.dead-end": (p) => ({
    text: `Von „${p.step}“ aus geht es nicht zum Ende weiter (Sackgasse).`,
    hint: "Jeder Schritt braucht einen Weg zum Ende.",
  }),
  "D2.parallel-writes": (p) => ({
    text: `„${p.a}“ und „${p.b}“ laufen parallel und schreiben beide „${p.element}“ – welcher Wert gilt, wäre Zufall.`,
    hint: "Nur einen der beiden Schritte schreiben lassen oder die Schritte nacheinander anordnen.",
  }),
  "Z2.nobody": (p) => ({
    text: `Für „${p.step}“ gibt es niemanden, der die Bearbeiterregel erfüllt.`,
    hint: "Der Rolle oder Abteilung mindestens eine Person zuordnen.",
  }),
  "Z3.reference-not-before": (p) => ({
    text: `„${p.step}“ bezieht sich auf die Person aus „${p.ref}“ – dieser Schritt läuft aber nicht auf jedem Weg vorher.`,
    hint: "Einen Schritt wählen, der garantiert vorher erledigt ist.",
  }),
  "Z4.automatic-with-staff": (p) => ({
    text: `„${p.step}“ läuft automatisch und darf deshalb keine Bearbeiterzuordnung haben.`,
  }),
  "T2.deadline": (p) => ({
    text: `Der längste Weg dauert ${fmtDuration(Number(p.critical))} und passt nicht in den Termin von ${fmtDuration(Number(p.deadline))}.`,
    hint: "Soll-Zeiten der Schritte verkürzen oder den Termin verlängern.",
  }),
  "B2.no-staff": (p) => ({
    text: `„${p.step}“ hat keine Bearbeiterzuordnung und würde in keiner Arbeitsliste erscheinen.`,
    hint: "Dem Schritt eine Rolle, Abteilung oder Person zuordnen.",
  }),
  "R0.not-draft": () => ({
    text: "Dieses Schema ist freigegeben und damit unveränderlich.",
    hint: "Für Änderungen eine neue Revision anlegen.",
  }),
  "LC.not-draft": () => ({ text: "Nur ein Entwurf kann freigegeben werden." }),
  "LC.not-released": () => ({ text: "Eine neue Revision lässt sich nur von einer freigegebenen Version anlegen." }),
  "D3.unknown-element": (p) => ({ text: `Das Datenelement „${p.element}“ gibt es in diesem Schema nicht.` }),
  "D6.not-writable": (p) => ({
    text: `„${p.step}“ setzt „${p.element}“ nicht – dieser Wert lässt sich beim Abschluss nicht ändern.`,
    hint: "Werte gibt nur der Schritt ein, der sie schreibt. Eine Korrektur nimmt die Prozessverantwortung vor.",
  }),
  "D6.unknown-element": (p) => ({ text: `„${p.element}“ ist kein Datenelement dieses Prozesses.` }),
  "D3.wrong-type": (p) => ({
    text: `Der Wert für „${p.element}“ passt nicht zum Typ ${typeName(p.type)}.`,
    hint: p.type === "DECIMAL" ? "Ein Betrag hat höchstens zwei Nachkommastellen." : undefined,
  }),
  "M0.not-candidate": () => ({ text: "Diese Instanz läuft nicht auf einer früheren Version dieses Schemas." }),
  "M1.not-released": (p) => ({
    text: `Die Zielversion v${p.version} ist noch nicht freigegeben.`,
    hint: "Erst freigeben, dann migrieren.",
  }),
  "M1.incorrect": (p) => ({ text: `Die Zielversion ist nicht korrekt (Regel ${p.rule}).` }),
  "M2.step-removed": (p) => ({
    text: `Der bereits bearbeitete Schritt „${p.step}“ fehlt in der neuen Version.`,
    hint: "Bereits erledigte Schritte dürfen in der neuen Version nicht entfernt werden. Diese Instanz läuft sicher auf ihrer Version weiter.",
  }),
  "M2.step-changed": (p) => ({ text: `Der bereits bearbeitete Schritt „${p.step}“ ist in der neuen Version kein Schritt derselben Art mehr.` }),
  "M2.path-changed": (p) => ({
    text: `Die neue Version ändert den bereits durchlaufenen Weg zwischen „${p.from}“ und „${p.to}“.`,
    hint: "Eine laufende Instanz kann nur in den Teil wechseln, der noch vor ihr liegt – der bereits durchlaufene Weg muss gleich bleiben.",
  }),
  "M3.rewired": (p) => ({
    text: `Nach dem erledigten Schritt „${p.step}“ ginge es in der neuen Version anders weiter.`,
    hint: "Die Instanz steht schon dahinter – sie läuft sicher auf ihrer Version weiter.",
  }),
  "M3.running-removed": (p) => ({ text: `Der gerade laufende Schritt „${p.step}“ ist in der neuen Version kein ausführbarer Schritt mehr.` }),
  "M4.missing-data": (p) => ({
    text: `„${p.step}“ braucht „${p.element}“, aber die Instanz hat dafür keinen Wert, und kein späterer Schritt liefert ihn.`,
    hint: `Einen Startwert für „${p.element}“ angeben.`,
  }),
  // --- Ziel-Pruefung der Webhooks (Regel I6, SSRF) -----------------------
  // Der Kern bleibt sprachneutral; formuliert wird hier, wie bei jedem Befund.
  // Laufzeit: Die Engine lehnt eine Aktion ab (409). Bis 1.28.0 kam
  // nur der englische Text, etwa „activity 'act_1' is already claimed by
  // 'a-tom' (W1)“. ``step`` ist der Schrittname, ``agent`` eine Agenten-ID.
  "EX.not-allowed": () => ({ text: "Das ist im aktuellen Zustand des Vorgangs nicht möglich." }),
  "EX.not-released": () => ({
    text: "Nur ein freigegebener Prozess lässt sich starten.",
    hint: "Den Entwurf freigeben oder als Prüfinstanz starten.",
  }),
  "EX.automatic": (p) => ({ text: `${stepOf(p)} ist ein automatischer Schritt – ihn übernimmt niemand von Hand.` }),
  "EX.not-ready": (p) => ({
    text: `${stepOf(p)} ist gerade nicht zu bearbeiten – der Vorgang steht an anderer Stelle.`,
    hint: "Die Ansicht aktualisieren; vermutlich hat jemand anderes den Schritt schon erledigt.",
  }),
  "EX.claimed-by-other": (p) => ({
    text: `${stepOf(p)} hat bereits ${p.agent ? agentNameOf(p.agent) : "jemand anderes"} übernommen.`,
    hint: "Abschließen oder zurücklegen kann nur, wer den Schritt übernommen hat.",
  }),
  "EX.not-eligible": (p) => ({ text: `Für ${stepOf(p)} bist du nicht zuständig.` }),
  "EX.failed": (p) => ({
    text: `${stepOf(p)} ist als gescheitert gemeldet und wartet auf den Wiederanlauf.`,
    hint: "Erst den Wiederanlauf auslösen, dann weiterarbeiten.",
  }),
  "EX.not-claimed": (p) => ({ text: `${stepOf(p)} hat niemand übernommen – es gibt nichts zurückzulegen.` }),
  "EX.suspended-by-other": (p) => ({ text: `${stepOf(p)} hat eine andere Person angehalten.` }),
  "EX.not-suspended": (p) => ({ text: `${stepOf(p)} ist nicht angehalten.` }),
  "EX.owner-only": (p) => ({ text: `Das darf bei ${stepOf(p)} nur, wer den Schritt übernommen hat.` }),
  "EX.suspended": (p) => ({ text: `${stepOf(p)} ist angehalten – bitte zuerst fortsetzen.` }),
  "EX.not-failed": (p) => ({ text: `${stepOf(p)} ist nicht als gescheitert gemeldet.` }),
  "EX.loop-broken": () => ({
    text: "Eine Schleife wiederholt sich ohne Arbeit dazwischen – das Modell ist nicht sauber geschachtelt.",
    hint: "Das Schema in einer neuen Revision korrigieren und den Vorgang migrieren.",
  }),
  "EX.no-decision": () => ({ text: "Eine Entscheidung hat keine Regel, nach der sie verzweigt." }),
  "EX.value-missing": (p) => ({
    text: `Für die Entscheidung fehlt der Wert „${p.element}“.`,
    hint: "Den Wert im Schritt davor setzen.",
  }),
  "EX.no-branch": (p) => ({ text: `Für den Wert von „${p.element}“ passt kein Zweig der Entscheidung.` }),
  "EX.follow-up-condition": () => ({ text: "Die Bedingung eines Folgeprozesses lässt sich nicht auswerten." }),
  "EX.target-missing": () => ({ text: "Ein Teil- oder Folgeprozess ist nicht (mehr) vorhanden." }),
  "EX.not-running": () => ({ text: "Der Vorgang läuft nicht mehr." }),
  "EX.unknown-step": () => ({ text: "Diesen Schritt gibt es im Vorgang nicht." }),
  "EX.not-activity": (p) => ({ text: `${stepOf(p)} ist kein Aufgaben-Schritt.` }),
  // BPMN-Import: vorher roh „unsupported BPMN element 'inclusiveGateway'“.
  "BPMN.invalid": () => ({ text: "Die BPMN-Datei lässt sich nicht übernehmen." }),
  "BPMN.no-process": () => ({ text: "Die Datei enthält keinen Prozess (<process>)." }),
  "BPMN.bad-extension": () => ({
    text: "Die ProcWorks-Zusatzdaten in der Datei sind beschädigt.",
    hint: "Die Datei ohne den Block <extensionElements> importieren oder neu exportieren.",
  }),
  "BPMN.invalid-xml": () => ({
    text: "Die Datei ist kein gültiges XML.",
    hint: "Ist es wirklich die .bpmn-Datei aus dem Modellierwerkzeug?",
  }),
  "BPMN.flow-incomplete": () => ({ text: "Ein Pfeil (sequenceFlow) hat keinen Anfang oder kein Ziel." }),
  "BPMN.missing-id": (p) => ({ text: `Ein Element <${p.element}> hat keine ID.` }),
  "BPMN.unsupported": (p) => ({
    text: `ProcWorks übernimmt das Element „${bpmnElementName(p.element)}“ nicht.`,
    hint: "Unterstützt werden Start, Ende, Aufgaben, Teilprozesse sowie exklusive (XOR) und parallele (UND) Gateways in sauberer Blockstruktur.",
  }),
  "BPMN.mixed-gateway": () => ({
    text: "Ein Gateway verzweigt und führt zugleich zusammen.",
    hint: "In zwei Gateways aufteilen: eines führt zusammen, das nächste verzweigt.",
  }),
  "WH.egress-locked": () => ({
    text: "Auf dieser Instanz sind ausgehende Verbindungen gesperrt.",
    hint: "Der Probelauf zeigt trotzdem, was gesendet würde.",
  }),
  "WH.scheme": (p) => ({
    text: `Die Adresse beginnt mit „${p.scheme}“ – erlaubt sind nur http und https.`,
  }),
  "WH.no-host": () => ({ text: "Die Adresse nennt keinen Server." }),
  "WH.credentials": () => ({
    text: "Die Adresse darf keine Zugangsdaten enthalten.",
    hint: "Ein Secret wird serverseitig hinterlegt und als Referenz angegeben.",
  }),
  "WH.port": () => ({ text: "Die Adresse hat keinen gültigen Port." }),
  "WH.not-allow-listed": (p) => ({
    text: `„${p.host}“ steht nicht auf der Freigabeliste dieser Instanz.`,
    hint: "Die Liste pflegt die Administration (PROCWORKS_WEBHOOK_ALLOWLIST).",
  }),
  "WH.no-resolve": (p) => ({
    text: `Der Name „${p.host}“ lässt sich nicht auflösen.`,
    hint: "Tippfehler, oder der Name existiert nur im internen Netz.",
  }),
  "WH.internal-address": (p) => ({
    text: `„${p.host}“ zeigt auf eine interne Adresse – solche Ziele sind nicht zulässig.`,
    hint: "Das verhindert, dass ein Modell nach innen telefoniert (SSRF-Schutz).",
  }),

  // --- Benutzerverwaltung -------------------------------------------------
  "USERS.delete-self": () => ({
    text: "Den eigenen Login kann man nicht löschen – sonst wäre man ausgesperrt.",
    hint: "Von einem anderen Administrator-Login aus löschen.",
  }),
  "USERS.last-admin": (p) => ({
    text: `„${p.login}“ ist der letzte Login mit der Rolle Administrator und bleibt deshalb bestehen.`,
    hint: "Erst einen weiteren Administrator-Login anlegen.",
  }),
  "USERS.login-taken": (p) => ({
    text: `Den Login „${p.login}“ gibt es schon.`,
    hint: "Einen anderen Login-Namen w\u00E4hlen oder den vorhandenen Login in der Benutzerverwaltung zur\u00FCcksetzen.",
  }),
  "USERS.unknown-role": (p) => ({ text: `Diese Rolle gibt es nicht: ${p.roles}.` }),
  // Passwortregeln: je Grund ein eigener Satz statt einer Sammelmeldung.
  "PW.too-short": (p) => ({ text: `Das neue Passwort ist zu kurz \u2013 mindestens ${p.min} Zeichen.` }),
  "PW.unchanged": () => ({ text: "Das neue Passwort muss sich vom bisherigen unterscheiden." }),

  // --- Vorbedingungen der Operationen (Regel OP) -------------------------
  "OP.not-found": (p) => ({
    text: `${OP_KIND_NAMES[p.kind] || "Das Element"} „${nm(p.name)}“ gibt es nicht (mehr).`,
    hint: "Ansicht neu laden – vermutlich wurde es inzwischen geändert oder entfernt.",
  }),
  "OP.already-exists": (p) => ({
    text: `${OP_KIND_NAMES[p.kind] || "Ein Element"} mit der Kennung „${p.name}“ gibt es schon.`,
    hint: "Eine andere Kennung wählen oder das vorhandene Element verwenden.",
  }),
  "OP.no-edge": (p) => ({
    text: `Zwischen „${p.source}“ und „${p.target}“ gibt es keine Verbindung, auf der sich einfügen ließe.`,
    hint: "Ansicht neu laden – vermutlich wurde das Modell inzwischen geändert.",
  }),
  "OP.after-end": () => ({ text: "Hinter dem Ende lässt sich nichts einfügen.", hint: "Am „+“ vor dem Ende einfügen." }),
  "OP.anchor-not-serial": () => ({
    text: "Hier lässt sich nicht einfügen – der Schritt hat mehrere Nachfolger.",
    hint: "Das „+“ auf der Verbindungslinie verwenden, an der eingefügt werden soll.",
  }),
  "OP.too-few-branches": () => ({ text: "Eine Verzweigung braucht mindestens zwei Zweige." }),
  "OP.discriminator-type": (p) => ({
    text: p.where === "LOOP"
      ? "Dieses Datenelement kann nicht über eine Wiederholung entscheiden."
      : "Nach diesem Datenelement kann nicht verzweigt werden.",
    hint: "Ein Element vom Typ Ja/Nein, Zahl oder Text wählen.",
  }),
  "OP.loop-discriminator-source": () => ({
    text: "Über die Wiederholung kann nur ein Datenelement des Vorgangs entscheiden, kein extern geliefertes.",
  }),
  "OP.loop-discriminator-type": () => ({
    text: "Für diese Wiederholungsbedingung muss das Merkmal Ja/Nein sein.",
    hint: "Ein Ja/Nein-Element wählen oder die Wiederhol-Werte angeben.",
  }),
  "OP.label-missing": () => ({ text: "Der neue Schritt braucht eine Bezeichnung." }),
  "OP.sync-self": () => ({ text: "Ein Schritt kann nicht auf sich selbst warten." }),
  "OP.sync-exists": () => ({ text: "Diese Warte-Beziehung gibt es schon." }),
  "OP.sync-missing": () => ({ text: "Diese Warte-Beziehung gibt es nicht." }),
  "OP.sync-sets-empty": () => ({ text: "Für einen Querschritt bitte Schritte davor und danach wählen." }),
  "OP.sync-not-parallel": () => ({
    text: "Warte-Beziehungen gibt es nur zwischen Schritten paralleler Zweige desselben Blocks.",
  }),
  "OP.move-self": () => ({ text: "Ein Schritt kann nicht hinter sich selbst verschoben werden." }),
  "OP.not-serial": () => ({
    text: "Dieser Schritt steht an einer Verzweigung und lässt sich so nicht verschieben oder entfernen.",
    hint: "Den ganzen Block über seine Verzweigung bearbeiten.",
  }),
  "OP.sole-loop-node": () => ({
    text: "Das ist der einzige Schritt der Schleife.",
    hint: "Erst einen weiteren Schritt in die Schleife einfügen oder die Schleife als Ganzes entfernen.",
  }),
  "OP.sole-parallel-node": () => ({
    text: "Das ist der einzige Schritt dieses parallelen Zweigs.",
    hint: "Erst einen weiteren Schritt einfügen oder den Zweig entfernen.",
  }),
  "OP.last-xor-branch": () => ({
    text: "Eine Entscheidung braucht mindestens einen Zweig mit Schritten.",
    hint: "Die ganze Verzweigung über die Entscheidung entfernen.",
  }),
  "OP.delete-start-end": () => ({ text: "Start und Ende lassen sich nicht entfernen." }),
  "OP.delete-split-instead": () => ({
    text: "Eine Zusammenführung wird nicht einzeln entfernt.",
    hint: "Die Verzweigung auswählen und entfernen – dann geht der ganze Block auf einmal.",
  }),
  "OP.delete-loop-instead": () => ({
    text: "Ein Schleifenende wird nicht einzeln entfernt.",
    hint: "Den Schleifenanfang auswählen und entfernen – dann geht die ganze Schleife auf einmal.",
  }),
  "OP.block-unclear": () => ({
    text: "Zu dieser Verzweigung lässt sich der Block nicht eindeutig bestimmen.",
    hint: "Das Modell ist nicht sauber blockstrukturiert – bitte melden, wenn es so entstanden ist.",
  }),
  "OP.no-empty-branch": () => ({ text: "Diese Entscheidung hat keinen leeren Zweig." }),
  "OP.no-data-access": () => ({ text: "Diese Datenbindung gibt es an diesem Schritt nicht." }),
  "OP.mask-empty": () => ({ text: "Eine Eingabemaske braucht mindestens ein Feld." }),
  "OP.mask-duplicate-field": () => ({
    text: "Ein Datenelement steht zweimal in der Maske.",
    hint: "Jedes Element nur einmal aufnehmen.",
  }),
  "OP.mask-columns": () => ({ text: "Eine Maske hat eine bis drei Spalten." }),
  "OP.no-mask": () => ({ text: "Dieser Schritt hat keine Eingabemaske." }),
  "OP.org-cycle": () => ({
    text: "So entstünde eine Schleife in der Abteilungsstruktur.",
    hint: "Eine Abteilung kann nicht unter sich selbst oder unter einer ihrer Unterabteilungen hängen.",
  }),
  "OP.shared-org": () => ({
    text: "Dieses Schema nutzt eine geteilte Organisation.",
    hint: "Rollen, Abteilungen und Personen dort pflegen – die Änderung gilt dann für alle verknüpften Schemata.",
  }),
  "OP.no-service": () => ({ text: "Dieser Schritt hat keinen Dienst." }),
  "OP.no-staff-rule": () => ({ text: "Dieser Schritt hat keine Bearbeiterzuordnung." }),
  "OP.no-subprocess": () => ({ text: "Dieser Schritt ist an keinen Teilprozess gebunden." }),
  "OP.automation-needs-service": () => ({
    text: "Für eine Automatik braucht der Schritt zuerst einen Dienst.",
    hint: "Unter „Dienst“ einen Dienst zuweisen, dann die Automatik einstellen.",
  }),
  "OP.wrong-node-kind": (p) => ({ text: OP_WRONG_KIND[p.what] || "Das geht an diesem Knoten nicht." }),
  "M5.adhoc": () => ({
    text: "Diese Instanz wurde einzeln angepasst (Ad-hoc-Änderung) und wird deshalb nicht automatisch migriert.",
    hint: "Sie läuft sicher auf ihrer Version weiter.",
  }),
  // --- Jeder Befund des Kerns traegt einen
  // Code. IDs in den Parametern loest ``nm()`` gegen das gezeigte Modell auf,
  // ``p.step`` ergaenzt ``findingText`` aus ``node_id``.
  "K2.degree": (p) => ({
    text: `${stepOf(p)} hat ${p.in} ${Number(p.in) === 1 ? "Eingang" : "Eingänge"} und ${p.out} ${Number(p.out) === 1 ? "Ausgang" : "Ausgänge"}; erwartet ist ${degreeText(p.expected)}.`,
    hint: "Typisch bei importiertem BPMN: Verbindungen fehlen oder ein Schritt hat mehrere Ein- oder Ausgänge.",
  }),
  "K4.unknown-node": () => ({ text: "Eine Warte-Beziehung verweist auf einen Schritt, den es nicht gibt." }),
  "K4.not-activity": () => ({ text: "Warte-Beziehungen verbinden nur Aufgaben-Schritte." }),
  "K4.not-parallel": () => ({
    text: "Eine Warte-Beziehung verbindet nur Schritte verschiedener Zweige derselben parallelen Verzweigung.",
  }),
  "K4.cycle": () => ({ text: "Die Warte-Beziehungen bilden mit dem Ablauf einen Kreis – die Schritte würden aufeinander warten." }),
  "K6.decision-not-loop-end": () => ({ text: "Eine Wiederholungsbedingung hängt an einem Knoten, der kein Schleifenende ist." }),
  "K6.unbalanced": (p) => ({ text: `Es gibt ${p.starts} Schleifenanfänge, aber ${p.ends} Schleifenenden.` }),
  "K6.start-unpaired": () => ({ text: "Zu einem Schleifenanfang gibt es kein passendes Schleifenende." }),
  "K6.end-unpaired": () => ({ text: "Zu einem Schleifenende gibt es keinen passenden Schleifenanfang." }),
  "K6.end-claimed-twice": () => ({ text: "Zwei Schleifenanfänge enden am selben Schleifenende – die Schleifen überkreuzen sich." }),
  "K6.empty-body": () => ({ text: "Die Schleife enthält keinen Schritt.", hint: "Einen Schritt in die Schleife legen oder sie entfernen." }),
  "K6.no-decision": () => ({ text: "Das Schleifenende hat keine Wiederholungsbedingung.", hint: "Am Schleifenende festlegen, wann wiederholt wird." }),
  "K6.discriminator-missing": (p) => ({ text: `Das Merkmal „${nm(p.element)}“ der Wiederholungsbedingung gibt es nicht.` }),
  "K6.discriminator-not-instance": (p) => ({ text: `Das Merkmal „${nm(p.element)}“ muss ein Vorgangsdatum sein, kein externes Datum.` }),
  "K6.discriminator-not-boolean": (p) => ({ text: `Das Merkmal „${nm(p.element)}“ muss Ja/Nein sein (ist ${typeName(p.type)}).` }),
  "K6.cells-need-boolean": () => ({ text: "Eine Wiederholungsbedingung ohne Wertbereiche braucht ein Ja/Nein-Merkmal." }),
  "K6.discriminator-type": (p) => ({
    text: `${merkmalOf(p)} vom Typ ${typeName(p.type)} kann über keine Wiederholung entscheiden.`,
    hint: "Erst die Wiederholungsbedingung auf ein anderes Merkmal umstellen.",
  }),
  "K6.kind-mismatch": (p) => ({
    text: `Die Wiederholungsbedingung passt nicht zum Typ ${typeName(p.type)} von ${merkmalOf(p, "ihrem Merkmal")}.`,
    hint: "Erst die Wiederholungsbedingung anpassen, dann den Typ ändern.",
  }),
  "K6.no-exit-cell": () => ({ text: "Die Wiederholungsbedingung hat keinen Wert, bei dem die Schleife endet – sie liefe endlos." }),
  "K6.no-repeat-cell": () => ({ text: "Die Wiederholungsbedingung hat keinen Wert, bei dem wiederholt wird." }),
  "K6.max-iterations": (p) => ({ text: `Die Höchstzahl der Durchläufe muss mindestens 2 sein (ist ${p.value}).` }),
  "K6.discriminator-not-written": (p) => ({
    text: `„${nm(p.element)}“ wird nicht in jedem Durchlauf der Schleife neu gesetzt – die Schleife entschiede auf altem Stand.`,
    hint: `In der Schleife einen Schritt „${nm(p.element)}“ schreiben lassen (Pflichtbindung).`,
  }),
  "K7.decision-not-split": () => ({ text: "Eine Verzweigungsbedingung hängt an einem Knoten, der keine Entscheidung ist." }),
  "K7.condition-not-on-split": () => ({ text: "Nur Verbindungen, die eine Entscheidung verlassen, können eine Bedingung tragen." }),
  "K7.no-decision": (p) => ({
    text: `${stepOf(p, "Die Entscheidung")} hat keine Bedingung, nach der ein Zweig gewählt wird.`,
    hint: "Ein Merkmal und die Wertbereiche der Zweige festlegen.",
  }),
  "K7.targets-mismatch": () => ({ text: "Die Zweige der Bedingung passen nicht zu den Ausgängen der Entscheidung." }),
  "K7.too-few-branches": () => ({ text: "Eine Entscheidung braucht mindestens zwei Zweige." }),
  "K7.two-empty-branches": () => ({ text: "Eine Entscheidung darf höchstens einen leeren Zweig haben." }),
  // Diese Befunde kommen meist aus einer abgelehnten Aenderung am Merkmal
  // (Loeschen, Typwechsel): Sie nennen deshalb Merkmal *und* Verzweigung und
  // sagen, was zuerst zu tun ist -- nicht den hypothetischen Folgezustand.
  "K7.discriminator-missing": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)} – ohne dieses Merkmal kann dort nicht entschieden werden.`,
    hint: "Erst die Entscheidung auf ein anderes Merkmal umstellen oder die Verzweigung entfernen.",
  }),
  "K7.discriminator-not-instance": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)} und muss deshalb ein Vorgangsdatum sein, kein externes Datum.`,
  }),
  "K7.discriminator-type": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)}, kann das als ${typeName(p.type)} aber nicht.`,
    hint: "Erst die Entscheidung auf ein anderes Merkmal umstellen.",
  }),
  "K7.kind-mismatch": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)}; deren Bedingung passt nicht zum Typ ${typeName(p.type)}.`,
    hint: "Erst die Bedingung der Entscheidung anpassen, dann den Typ ändern.",
  }),
  "K7.discriminator-unset": (p) => ({
    text: `Das Merkmal von ${stepOf(p, "der Entscheidung")} ist nicht auf jedem Weg gesetzt, wenn entschieden wird.`,
    hint: "Das Merkmal vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "K7.threshold-last-unbounded": () => ({ text: "Der letzte Wertbereich muss nach oben offen sein." }),
  "K7.threshold-only-last-unbounded": () => ({ text: "Nur der letzte Wertbereich darf nach oben offen sein." }),
  "K7.threshold-ascending": () => ({ text: "Die Grenzen der Wertbereiche müssen aufsteigen." }),
  "K7.threshold-not-finite": () => ({ text: "Die Grenzen der Wertbereiche müssen endliche Zahlen sein." }),
  "K7.boolean-two-branches": () => ({ text: "Eine Ja/Nein-Bedingung hat genau zwei Zweige." }),
  "K7.boolean-cover": () => ({ text: "Eine Ja/Nein-Bedingung braucht genau einen Ja- und einen Nein-Zweig." }),
  "K7.enum-one-otherwise": () => ({ text: "Eine Auswahl-Bedingung braucht genau einen Zweig „sonst“." }),
  "K7.otherwise-values": () => ({ text: "Der Zweig „sonst“ darf keine Werte aufzählen." }),
  "K7.enum-empty-branch": () => ({ text: "Jeder Zweig einer Auswahl-Bedingung braucht mindestens einen Wert." }),
  "K7.enum-duplicate": (p) => ({ text: `Der Wert ${p.value} steht in mehr als einem Zweig.` }),
  "D3.param-type": (p) => ({ text: `Der Parametertyp ${typeName(p.param_type)} passt nicht zu „${nm(p.element)}“ (${typeName(p.type)}).` }),
  "D4.unknown-node": () => ({ text: "Eine Datenbindung verweist auf einen Schritt, den es nicht gibt." }),
  "D4.not-activity": () => ({ text: "Daten lassen sich nur an Aufgaben-Schritte binden." }),
  "D4.unknown-element": (p) => ({ text: `Eine Datenbindung verweist auf das unbekannte Datenelement „${nm(p.element)}“.` }),
  "U1.unknown-node": () => ({ text: "Eine Eingabemaske hängt an einem Schritt, den es nicht gibt." }),
  "U1.not-activity": () => ({ text: "Eingabemasken gibt es nur an Aufgaben-Schritten." }),
  "U1.unknown-element": (p) => ({ text: `Ein Maskenfeld verweist auf das unbekannte Datenelement „${nm(p.element)}“.` }),
  "U2.duplicate-field": () => ({ text: "Zwei Felder der Maske haben dieselbe Kennung." }),
  "U2.element-twice": (p) => ({ text: `„${nm(p.element)}“ steht zweimal in derselben Maske.` }),
  "U2.empty-label": () => ({ text: "Ein Maskenfeld hat keine Beschriftung." }),
  "U2.widget-type": (p) => ({
    text: `In der Maske von ${stepOf(p, "einem Schritt")} kann das Bedienelement „${nm(p.element)}“ (${typeName(p.type)}) nicht darstellen.`,
    hint: "Das Feld in der Maske auf ein passendes Bedienelement umstellen oder entfernen.",
  }),
  "U2.dropdown-options": () => ({ text: "Eine Auswahlliste braucht mindestens zwei Einträge." }),
  "U2.duplicate-options": () => ({ text: "Eine Auswahlliste enthält einen Eintrag doppelt." }),
  "U2.options-not-dropdown": () => ({ text: "Nur eine Auswahlliste trägt Einträge." }),
  "U3.input-no-write": (p) => ({ text: `Das Eingabefeld für „${nm(p.element)}“ hat keine Schreibbindung am Schritt.` }),
  "U3.display-no-read": (p) => ({ text: `Das Anzeigefeld für „${nm(p.element)}“ hat keine Lesebindung am Schritt.` }),
  "C1.instance-with-binding": (p) => ({ text: `„${nm(p.element)}“ ist ein Vorgangsdatum und darf keine externe Anbindung tragen.` }),
  "C1.binding-kinds": (p) => ({ text: `„${nm(p.element)}“ braucht genau eine Art externer Anbindung.` }),
  "C1.binding-missing": (p) => ({ text: `Das externe Datenelement „${nm(p.element)}“ hat keine Anbindung.` }),
  "C1.unknown-connector": (p) => ({ text: `„${nm(p.element)}“ verweist auf den unbekannten Connector „${p.connector}“.` }),
  "C3.empty-entity": (p) => ({ text: `Für „${nm(p.element)}“ fehlt die Tabelle.` }),
  "C2.self-key": (p) => ({ text: `„${nm(p.element)}“ kann nicht sein eigener Schlüssel sein.` }),
  "C2.unknown-key": (p) => ({ text: `Der Schlüssel „${nm(p.key)}“ von „${nm(p.element)}“ existiert nicht.` }),
  "C2.key-not-instance": (p) => ({ text: `Der Schlüssel „${nm(p.key)}“ von „${nm(p.element)}“ muss ein Vorgangsdatum sein.` }),
  "C2.key-not-set": (p) => ({
    text: `Der Schlüssel „${nm(p.key)}“ für „${nm(p.element)}“ ist nicht auf jedem Weg gesetzt, bevor gelesen wird.`,
    hint: "Den Schlüssel vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "C4.type-mismatch": (p) => ({ text: `„${nm(p.element)}“ ist ${typeName(p.type)}, die Abfrage liefert aber ${typeName(p.result_type)}.` }),
  "C5.unknown-connector": (p) => ({ text: `„${nm(p.element)}“ verweist auf den unbekannten Connector „${p.connector}“.` }),
  "C5.empty-entity": (p) => ({ text: `Für die Abfrage von „${nm(p.element)}“ fehlt die Tabelle.` }),
  "C5.empty-column": (p) => ({ text: `Für die Abfrage von „${nm(p.element)}“ fehlt die Spalte.` }),
  "C5.operator-type": (p) => ({ text: `Der Vergleich ${p.operator} passt nicht zu einer Spalte vom Typ ${typeName(p.column_type)} (${nm(p.element)}).` }),
  "C5.unknown-source": (p) => ({ text: `Ein Filter von „${nm(p.element)}“ verweist auf das unbekannte Datenelement „${nm(p.source)}“.` }),
  "C5.source-not-instance": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ für „${nm(p.element)}“ muss ein Vorgangsdatum sein.` }),
  "C5.source-type": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ (${typeName(p.source_type)}) für „${nm(p.element)}“ passt nicht zur Spalte (${typeName(p.column_type)}).` }),
  "C5.source-unset": (p) => ({
    text: `Der Filterwert „${nm(p.source)}“ für „${nm(p.element)}“ ist nicht auf jedem Weg gesetzt, bevor gelesen wird.`,
    hint: "Den Filterwert vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "C6.no-unique-column": (p) => ({ text: `„${nm(p.element)}“ liest genau einen Datensatz, nennt aber keine eindeutige Spalte.` }),
  "C6.no-unique-filter": (p) => ({ text: `„${nm(p.element)}“ liest genau einen Datensatz, filtert aber nicht auf die eindeutige Spalte „${p.column}“.` }),
  "C6.aggregate-plain-column": (p) => ({ text: `„${nm(p.element)}“ fasst Datensätze zusammen, liest aber eine einfache Spalte.` }),
  "C6.empty-order": (p) => ({ text: `„${nm(p.element)}“ nimmt den ersten Datensatz, gibt aber keine Sortierung an.` }),
  "C7.type-mismatch": (p) => ({ text: `„${nm(p.element)}“ ist ${typeName(p.type)}, die Zielspalte aber ${typeName(p.column_type)}.` }),
  "C8.unknown-connector": (p) => ({ text: `Das Zurückschreiben von „${nm(p.element)}“ verweist auf den unbekannten Connector „${p.connector}“.` }),
  "C8.empty-entity": (p) => ({ text: `Für das Zurückschreiben von „${nm(p.element)}“ fehlt die Tabelle.` }),
  "C8.empty-column": (p) => ({ text: `Für das Zurückschreiben von „${nm(p.element)}“ fehlt die Zielspalte.` }),
  "C8.operator-type": (p) => ({ text: `Der Vergleich ${p.operator} passt nicht zu einer Spalte vom Typ ${typeName(p.column_type)} (${nm(p.element)}).` }),
  "C8.unknown-source": (p) => ({ text: `Ein Filter beim Zurückschreiben von „${nm(p.element)}“ verweist auf das unbekannte Datenelement „${nm(p.source)}“.` }),
  "C8.source-not-instance": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ für „${nm(p.element)}“ muss ein Vorgangsdatum sein.` }),
  "C8.source-type": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ (${typeName(p.source_type)}) für „${nm(p.element)}“ passt nicht zur Spalte (${typeName(p.column_type)}).` }),
  "C8.source-unset": (p) => ({
    text: `Der Filterwert „${nm(p.source)}“ ist nicht auf jedem Weg gesetzt, bevor „${nm(p.element)}“ zurückgeschrieben wird.`,
    hint: "Den Filterwert vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "C9.no-unique-column": (p) => ({ text: `Das Zurückschreiben von „${nm(p.element)}“ nennt keine eindeutige Spalte – es muss genau einen Datensatz treffen.` }),
  "C9.no-unique-filter": (p) => ({ text: `Das Zurückschreiben von „${nm(p.element)}“ filtert nicht auf die eindeutige Spalte „${p.column}“.` }),
  "Z1.unknown-role": (p) => ({ text: `Die Bearbeiterregel nennt die unbekannte Rolle „${nm(p.ref)}“.` }),
  "Z1.unknown-unit": (p) => ({ text: `Die Bearbeiterregel nennt die unbekannte Abteilung „${nm(p.ref)}“.` }),
  "Z1.unknown-agent": (p) => ({ text: `Die Bearbeiterregel nennt die unbekannte Person „${nm(p.ref)}“.` }),
  "Z1.unknown-node-ref": (p) => ({ text: `Die Bearbeiterregel verweist auf den unbekannten Schritt „${nm(p.ref)}“.` }),
  "Z1.unknown-node": () => ({ text: "Eine Bearbeiterregel hängt an einem Schritt, den es nicht gibt." }),
  "Z1.not-activity": () => ({ text: "Bearbeiter lassen sich nur Aufgaben-Schritten zuordnen." }),
  "Z1.no-operands-allowed": () => ({ text: "Diese Art von Bearbeiterregel darf keine Teilregeln enthalten." }),
  "Z1.reference-missing": () => ({ text: "Der Bearbeiterregel fehlt die Angabe, wer gemeint ist." }),
  "Z1.except-two": () => ({ text: "„außer“ braucht genau zwei Teilregeln: wer, und wer davon nicht." }),
  "Z1.too-few-operands": (p) => ({ text: `Diese Kombination braucht mindestens ${countLabel(Number(p.count), "Teilregel", "Teilregeln")}.` }),
  "Z1.agent-unknown-role": (p) => ({ text: `„${nm(p.agent)}“ hat die unbekannte Rolle „${nm(p.role)}“.` }),
  "Z1.agent-unknown-unit": (p) => ({ text: `„${nm(p.agent)}“ gehört zur unbekannten Abteilung „${nm(p.unit_ref)}“.` }),
  "Z1.own-deputy": (p) => ({ text: `„${nm(p.agent)}“ kann nicht die eigene Vertretung sein.` }),
  "Z1.unknown-deputy": (p) => ({ text: `Die Vertretung „${nm(p.deputy)}“ von „${nm(p.agent)}“ gibt es nicht.` }),
  "Z1.unknown-manager": (p) => ({ text: `Die Leitung „${nm(p.manager)}“ der Abteilung „${nm(p.unit)}“ gibt es nicht.` }),
  "Z1.unknown-parent": (p) => ({ text: `Die übergeordnete Abteilung „${nm(p.parent)}“ von „${nm(p.unit)}“ gibt es nicht.` }),
  "Z1.unit-cycle": () => ({ text: "Die Abteilungen sind im Kreis untergeordnet." }),
  "Z4.not-activity": () => ({ text: "Ein Dienst lässt sich nur an Aufgaben-Schritte binden." }),
  "A1.unknown-template": (p) => ({ text: `Die Dienst-Vorlage „${p.template}“ gibt es nicht.` }),
  "A2.executor-mismatch": (p) => ({ text: `„automatisch“ passt nicht zur Ausführungsart der Dienst-Vorlage „${p.template}“.` }),
  "A3.param-unbound": (p) => ({ text: `Der Pflichtparameter „${p.param}“ des Dienstes ist nicht belegt.` }),
  "A3.unknown-param": (p) => ({ text: `Die Dienst-Vorlage „${p.template}“ hat keinen Parameter „${p.param}“.` }),
  "A3.unknown-element": (p) => ({ text: `Der Parameter „${p.param}“ ist an das unbekannte Datenelement „${nm(p.element)}“ gebunden.` }),
  "A3.type-mismatch": (p) => ({ text: `Der Parameter „${p.param}“ (${typeName(p.param_type)}) passt nicht zu „${nm(p.element)}“ (${typeName(p.type)}).` }),
  "I1.no-topic": () => ({ text: "Eine externe Aufgabe braucht ein Thema (Topic)." }),
  "I1.no-endpoint": () => ({ text: "Ein HTTP-Aufruf braucht einen Endpunkt." }),
  "I2.topic-with-endpoint": () => ({ text: "Eine externe Aufgabe darf keinen Endpunkt tragen." }),
  "I2.endpoint-with-topic": () => ({ text: "Ein HTTP-Aufruf darf kein Thema (Topic) tragen." }),
  "I2.automated-not-automatic": () => ({ text: "Eine angebundene Automatik muss als „automatisch“ markiert sein." }),
  "I3.unknown-element": (p) => ({ text: `Der Parameter „${p.param}“ verweist auf das unbekannte Datenelement „${nm(p.element)}“.` }),
  "I4.inline-reference": () => ({
    text: "Adressen und Zugangsdaten gehören nicht ins Modell, nur ein Verweis auf die Konfiguration.",
  }),
  "T1.negative-deadline": () => ({ text: "Der Termin des Prozesses darf nicht negativ sein." }),
  "T1.unknown-node": () => ({ text: "Eine Zeitvorgabe hängt an einem Schritt, den es nicht gibt." }),
  "T1.negative-duration": () => ({ text: "Die Höchstdauer darf nicht negativ sein." }),
  "T1.negative-lead": () => ({ text: "Die Soll-Zeit darf nicht negativ sein." }),
  "T1.deadline-not-finite": () => ({ text: "Der Termin des Prozesses muss eine endliche Zahl sein." }),
  "T1.duration-not-finite": () => ({ text: "Die Höchstdauer muss eine endliche Zahl sein." }),
  "T1.lead-not-finite": () => ({ text: "Die Soll-Zeit muss eine endliche Zahl sein." }),
  "T3.not-activity": () => ({ text: "Eine Eskalation gibt es nur an Aufgaben-Schritten." }),
  "T3.automatic": () => ({ text: "Ein automatischer Schritt eskaliert nicht – dafür gibt es Störungen und Wiederholungen." }),
  "T3.no-target-time": () => ({ text: "Für eine Eskalation braucht der Schritt eine Soll-Zeit oder Höchstdauer." }),
  "T3.no-stages": () => ({ text: "Eine Eskalation braucht mindestens eine Stufe." }),
  "T3.negative-offset": () => ({ text: "Eine Eskalationsstufe darf nicht vor der Fälligkeit liegen." }),
  "T3.offsets-ascending": () => ({ text: "Die Eskalationsstufen müssen zeitlich aufeinander folgen." }),
  "T3.offset-not-finite": () => ({ text: "Der Zeitpunkt einer Eskalationsstufe muss eine endliche Zahl sein." }),
  "T3.node-ref-target": () => ({ text: "Eine Eskalation richtet sich an Rollen oder Abteilungen, nicht an den Bearbeiter eines Schritts." }),
  "T3.nobody": () => ({ text: "Das Ziel der Eskalation findet niemanden im Organisationsmodell." }),
  "N1.agent-mail": (p) => ({ text: `Die E-Mail-Adresse von „${nm(p.agent)}“ ist ungültig.` }),
  "N1.role-mail": (p) => ({ text: `Das Gruppenpostfach der Rolle „${nm(p.role)}“ ist ungültig.` }),
  "N1.unit-mail": (p) => ({ text: `Das Postfach der Abteilung „${nm(p.unit)}“ ist ungültig.` }),
  "N2.unknown-node": () => ({ text: "Eine E-Mail-Benachrichtigung hängt an einem Schritt, den es nicht gibt." }),
  "N2.not-activity": () => ({ text: "E-Mail-Benachrichtigungen gibt es nur an Aufgaben-Schritten." }),
  "N2.no-staff-rule": () => ({ text: "Ohne Bearbeiterzuordnung gibt es niemanden, der benachrichtigt werden kann." }),
  "N3.not-static": () => ({
    text: "Die Empfänger hängen vom Bearbeiter eines früheren Schritts ab – persönliche Mails lassen sich hier nicht modellieren.",
    hint: "Ein Gruppenpostfach verwenden.",
  }),
  "N3.no-address": (p) => ({ text: `„${nm(p.agent)}“ könnte zuständig sein, hat aber keine E-Mail-Adresse.` }),
  "N3.no-group": () => ({ text: "Die Bearbeiterregel nennt keine Rolle oder Abteilung – es gibt kein Gruppenpostfach." }),
  "N3.role-no-mailbox": (p) => ({ text: `Die Rolle „${nm(p.ref)}“ hat kein Gruppenpostfach.` }),
  "N3.unit-no-mailbox": (p) => ({ text: `Die Abteilung „${nm(p.ref)}“ hat kein Postfach.` }),
  "N3.performer-no-mailbox": () => ({ text: "Der Bearbeiter eines früheren Schritts hat kein Gruppenpostfach – dafür persönliche Mails verwenden." }),
  "N4.unknown-element": (p) => ({ text: `Der Platzhalter {${p.ref}} verweist auf ein unbekanntes Datenelement.` }),
  "N4.not-instance": (p) => ({ text: `Der Platzhalter {${p.ref}} ist kein Vorgangsdatum und steht im Mailtext nicht zur Verfügung.` }),
  "N4.not-set": (p) => ({ text: `Der Platzhalter {${p.ref}} ist nicht sicher gesetzt, wenn die Aufgabe bereit wird.` }),
  "H1.no-binding": () => ({ text: "Der Teilprozess-Schritt ist an keinen Teilprozess gebunden." }),
  "H1.not-subprocess": () => ({ text: "Eine Teilprozess-Bindung hängt an einem Schritt, der kein Teilprozess ist." }),
  "H1.target-missing": (p) => ({ text: `Den Teilprozess „${schemaName(p.target)}“ (Version ${p.version}) gibt es nicht.` }),
  "H1.target-not-released": (p) => ({ text: `Der Teilprozess „${schemaName(p.target)}“ ist nicht freigegeben.` }),
  "H2.unknown-parent-element": (p) => ({ text: `Die Übergabe verweist auf das unbekannte Datenelement „${nm(p.parent_element)}“.` }),
  "H2.unknown-target-element": (p) => ({ text: `Die Übergabe nennt das unbekannte Datenelement „${p.target_element}“ im Teilprozess.` }),
  "H2.type-mismatch": (p) => ({ text: `Die Übergabe verbindet „${nm(p.parent_element)}“ (${typeName(p.parent_type)}) mit „${p.target_element}“ (${typeName(p.target_type)}).` }),
  "H2.output-not-written": (p) => ({ text: `Der Teilprozess setzt „${p.target_element}“ nicht auf jedem Weg – „${nm(p.parent_element)}“ bliebe leer.` }),
  "H2.input-not-written": (p) => ({ text: `„${nm(p.parent_element)}“ ist nicht auf jedem Weg gesetzt, bevor der Teilprozess startet.` }),
  "H3.cycle": () => ({ text: "Die Teilprozesse enthalten sich gegenseitig – ein Prozess kann sich nicht selbst enthalten." }),
  "F1.no-released-version": () => ({ text: "Vom Folgeprozess gibt es keine passende freigegebene Version." }),
  "F1.not-released": () => ({ text: "Der Folgeprozess ist nicht freigegeben." }),
  "F2.unknown-source": (p) => ({ text: `Die Übergabe an den Folgeprozess verweist auf das unbekannte Datenelement „${nm(p.source_element)}“.` }),
  "F2.unknown-target": (p) => ({ text: `Die Übergabe nennt das unbekannte Datenelement „${p.target_element}“ im Folgeprozess.` }),
  "F2.type-mismatch": (p) => ({ text: `Die Übergabe verbindet „${nm(p.source_element)}“ (${typeName(p.source_type)}) mit „${p.target_element}“ (${typeName(p.target_type)}).` }),
  "F4.no-condition": () => ({ text: "Ein bedingter Folgeprozess hat keine Bedingung." }),
  "F4.invalid-condition": (p) => ({ text: `Die Bedingung des Folgeprozesses ist ungültig: ${p.error}` }),
  "F4.unknown-element": (p) => ({ text: `Die Bedingung des Folgeprozesses verweist auf das unbekannte Datenelement „${p.element}“.` }),
  "F4.condition-not-written": (p) => ({
    text: `Die Bedingung des Folgeprozesses liest „${nm(p.element)}“, das nicht auf jedem Weg gesetzt wird.`,
    hint: "Das Datenelement in einem Schritt auf jedem Weg schreiben lassen, z. B. über ein Ankreuzfeld.",
  }),
  "R1.not-found": () => ({ text: "Diesen Schritt gibt es in dem Vorgang nicht." }),
  "R1.after-end": () => ({ text: "Hinter dem Ende lässt sich nichts einfügen." }),
  "R1.anchor-not-serial": (p) => ({ text: `Hinter ${stepOf(p, "diesem Knoten")} lässt sich nichts einfügen – er hat mehr als einen Ausgang.` }),
  "R1.already-passed": (p) => ({ text: `Der Vorgang ist schon über ${stepOf(p, "diese Stelle")} hinaus – dort lässt sich nichts mehr ändern.` }),
  "R1.already-reached": (p) => ({ text: `${stepOf(p)} ist im Vorgang schon erreicht – ad hoc ändern lässt sich nur, was der Vorgang noch nicht erreicht hat.` }),
  "R1.delete-not-activity": () => ({ text: "Ad hoc lassen sich nur Aufgaben-Schritte entfernen." }),
  "R1.not-serial": () => ({ text: "Dieser Schritt liegt nicht auf einer einfachen Strecke und lässt sich ad hoc nicht entfernen." }),
  "R1.rename-not-step": () => ({ text: "Ad hoc lassen sich nur Schritte und Teilprozesse umbenennen." }),
  "OP.own-deputy": () => ({ text: "Eine Person kann nicht ihre eigene Vertretung sein." }),
  "OP.label-empty": (p) => ({ text: `${OP_KIND_NAMES[p.kind] || "Das Element"} braucht einen Namen.` }),
  "OP.label-too-long": (p) => ({ text: `Der Name ist zu lang – höchstens ${p.max} Zeichen.` }),
  "U6.label-too-long": (p) => ({ text: `${stepOf(p, "Eine Bezeichnung")} ist zu lang – höchstens ${p.max} Zeichen.` }),
  "U2.bounds-not-number": (p) => ({ text: `„${p.field}“: Unter- und Obergrenze gibt es nur bei Zahlenfeldern.` }),
  "U2.bounds-order": (p) => ({ text: `„${p.field}“: Die Untergrenze liegt über der Obergrenze.` }),
  "U2.bounds-not-finite": (p) => ({ text: `„${p.field}“: Unter- und Obergrenze müssen endliche Zahlen sein.` }),
  "U2.text-rule-not-text": (p) => ({ text: `„${p.field}“: Muster und Höchstlänge gibt es nur bei Textfeldern.` }),
  "U2.pattern-invalid": (p) => ({ text: `„${p.field}“: Das Muster ist kein gültiger regulärer Ausdruck.` }),
  "U2.length-invalid": (p) => ({ text: `„${p.field}“: Die Höchstlänge muss mindestens 1 sein.` }),
  "U4.below-min": (p) => ({ text: `„${p.field}“ muss mindestens ${p.min} sein.` }),
  "U4.above-max": (p) => ({ text: `„${p.field}“ darf höchstens ${p.max} sein.` }),
  "U4.too-long": (p) => ({ text: `„${p.field}“ darf höchstens ${p.max} Zeichen lang sein.` }),
  "U4.pattern": (p) => ({ text: `„${p.field}“ passt nicht zum vorgegebenen Format.` }),
  "U5.too-many": (p) => ({ text: `Höchstens ${p.max} Datenelemente können einen Vorgang benennen.` }),
  "U5.unknown-element": (p) => ({ text: `Das Datenelement „${nm(p.element)}“ zur Benennung des Vorgangs gibt es nicht.` }),
  "U5.not-instance": (p) => ({ text: `„${p.element}“ ist ein externes Datum und kann den Vorgang nicht benennen.` }),
};

/**
 * Anzeigetext eines Befunds: aus dem Katalog, sonst die Kernmeldung.
 *
 * Im Rueckfall wird eine enthaltene Knoten-ID durch den Schrittnamen ersetzt,
 * soweit das geladene Schema ihn kennt -- so erscheint auch ein noch nicht
 * uebersetzter Befund nicht mit „act_246“.
 *
 * @param {{rule:string, message:string, node_id?:string|null, code?:string|null, params?:Object<string,string>}} f Befund
 * @param {{withHint?: boolean}} [opts] ``withHint`` haengt den Handlungsvorschlag an
 * @returns {string}
 */
/**
 * Anzeigename fuer eine ID aus Befund-Parametern.
 *
 * Viele Befunde des Kerns nennen IDs (Datenelement, Rolle, Person, Schritt),
 * weil der Kern an der Stelle nur die ID kennt. Aufgeloest wird gegen das
 * gezeigte Modell: Schritte (auch im Ad-hoc-Wandel des Vorgangs),
 * Datenelemente, Rollen, Abteilungen, Personen. Unbenannte Knoten heissen
 * nach ihrem Zusammenhang (``nodeCaptionInContext``). Unbekanntes bleibt, wie es
 * ist -- ein Name aus dem Kern loest sich so auf sich selbst auf.
 * @param {string|undefined} id ID oder bereits ein Name
 * @returns {string}
 */
function nm(id) {
  if (id == null || id === "") return "\u2013";
  const models = [state.schema, state.instance && state.instance.ad_hoc_schema].filter(Boolean);
  for (const m of models) {
    const n = (m.nodes || {})[id];
    // Unbenannte Verzweigungen hiessen sonst alle „XOR ▶“ -- im Befund
    // unbrauchbar; mit Zusammenhang „Entscheidung nach „Betrag erfassen““.
    if (n) return nodeCaptionInContext(m, n);
    const d = (m.data_elements || {})[id];
    if (d) return d.name || id;
    const org = m.org_model || {};
    for (const map of [org.roles, org.org_units, org.agents]) {
      if (map && map[id]) return map[id].name || id;
    }
  }
  if (state.agentDirectory && state.agentDirectory[id]) return state.agentDirectory[id].name || id;
  return String(id);
}

/**
 * Fachliche Namen der Datentypen fuer **jede** Anzeige.
 * INTEGER/FLOAT/STRING/BOOLEAN/URI standen roh in Dialogen und Tabellen; die
 * API-Werte bleiben unveraendert (``value`` der Auswahllisten), nur die
 * Beschriftung ist deutsch.
 */
const DATA_TYPE_LABELS = {
  INTEGER: "Ganzzahl", FLOAT: "Kommazahl", DECIMAL: "Betrag", STRING: "Text", DATE: "Datum", BOOLEAN: "Ja/Nein", URI: "Link",
};
function typeName(t) { return DATA_TYPE_LABELS[t] || t || "?"; }

/**
 * Titel eines Vorgangs aus seinen benennenden Werten (``display_fields``):
 * „Müller GmbH · Auftragswert: 8.880,00“ statt ``instance_14``. Texte stehen
 * pur; Zahlen und Ja/Nein tragen den Feldnamen davor, sonst sagt eine nackte
 * „8“ nichts. Formatiert wird nach Datentyp (``formatValue``): Betrag mit zwei
 * Stellen, Ganzzahl ohne Tausenderpunkt (Kennnummern). Leere Werte fallen
 * weg; ohne Werte ist der Titel leer und die Aufrufer fallen auf
 * ``instanceName`` (Startzeit) zurueck.
 * @param {{name: string, value: *, data_type?: string}[]|undefined} values
 *   ``context`` einer Aufgabe bzw. ein Eintrag aus ``GET /instance-titles``
 * @returns {string}
 */
function contextTitle(values) {
  return (values || [])
    .filter((v) => v.value !== null && v.value !== undefined && v.value !== "")
    .map((v) => {
      if (typeof v.value === "string") return v.value;
      const shown = formatValue({ data_type: v.data_type }, v.value);
      return v.name ? `${v.name}: ${shown}` : shown;
    })
    .join(" \u00B7 ");
}

/**
 * Text eines Modellhinweises (G-Gruppe) ohne interne Kennung.
 *
 * Der Kern formuliert Hinweise schon deutsch, nennt den Knoten darin aber per
 * Kennung („Das Gateway 'split_482' …“). Hier wird sie durch den lesbaren Namen
 * ersetzt -- bei unbenannten Verzweigungen „Entscheidung nach „Betrag
 * erfassen““ (``nodeCaptionInContext``). Hinweise sind beratend; die
 * Darstellung bleibt neutral (Klasse ``rule-hint``), nie rot wie ein Befund.
 *
 * @param {{code: string, message: string, node_id?: string|null}} h Hinweis
 * @param {object} schema Schema, auf das er sich bezieht
 * @returns {string}
 */
function hintText(h, schema) {
  const msg = (h && h.message) || "";
  const node = h && h.node_id && schema && (schema.nodes || {})[h.node_id];
  if (!node) return msg;
  return msg.split(`'${h.node_id}'`).join(`\u201E${nodeCaptionInContext(schema, node)}\u201C`);
}

/**
 * Lesbarer Name eines Vorgangs -- nie die interne Kennung allein.
 *
 * Reihenfolge: die benennenden Werte (``display_fields``, z. B. „Müller GmbH
 * · A-4711“); fehlen sie (noch -- beim ersten Schritt sind sie meist leer),
 * „Vorgang vom 01.10., 14:03“ aus der Startzeit; ohne Startzeit (Altbestand)
 * schlicht „Vorgang“. Die Kennung zeigt ``instanceNameCell`` klein darunter.
 *
 * @param {string|null|undefined} startedAt ISO-Zeitpunkt des Starts
 * @param {{name: string, value: *}[]|undefined} values benennende Werte
 * @returns {string}
 */
function instanceName(startedAt, values) {
  const title = contextTitle(values);
  if (title) return title;
  const d = startedAt ? new Date(startedAt) : null;
  if (d && !isNaN(d.getTime())) {
    const when = d.toLocaleString("de-DE",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    return `Vorgang vom ${when}`;
  }
  return "Vorgang";
}

/**
 * Tabellenzelle bzw. Kopfzeile fuer einen Vorgang: Name gross, Kennung klein
 * darunter (fuer Rueckfragen und den Bezug zum Audit-Log), Kennung auch im
 * Tooltip.
 * @param {string} id Kennung des Vorgangs
 * @param {string|null|undefined} startedAt Startzeit (ISO)
 * @param {{name: string, value: *}[]|undefined} values benennende Werte
 * @returns {HTMLElement}
 */
function instanceNameCell(id, startedAt, values) {
  return el("div", { title: `Kennung: ${id}` }, instanceName(startedAt, values),
    el("div", { class: "task-context" }, id));
}

/**
 * Benennende Werte eines geladenen Vorgangs gemaess seinem Schema.
 * @param {object} inst Vorgang (mit ``data_values``)
 * @param {object} schema Schema, gegen das er laeuft
 * @returns {{name: string, value: *}[]}
 */
function instanceValues(inst, schema) {
  const elems = (schema && schema.data_elements) || {};
  return ((schema && schema.display_fields) || []).map((eid) => ({
    name: (elems[eid] && elems[eid].name) || eid, value: (inst.data_values || {})[eid],
    data_type: elems[eid] && elems[eid].data_type,
  }));
}

/**
 * Anzeigename eines Vorgangs in seiner Detailansicht (ohne Kennung -- die
 * steht im Tooltip der Aufrufer).
 * @param {object} inst Vorgang
 * @param {object} schema Schema, gegen das er laeuft
 * @returns {string}
 */
function instanceCaption(inst, schema) {
  return instanceName(inst.started_at, instanceValues(inst, schema));
}

/** Name eines Schemas (Teilprozess, Folgeprozess) aus seiner ID, ohne Version. */
function schemaName(id) { return (state.schemaNames && state.schemaNames[id]) || id; }

/**
 * Der Schritt eines Befunds als Satzanfang: „„Pruefen““ oder ein Ersatzwort.
 * @param {object} p Parameter (``step`` ergaenzt ``findingText`` aus ``node_id``)
 * @param {string} [fallback] Ersatz, wenn der Befund keinen Schritt nennt
 */
/**
 * Deutscher Name eines BPMN-Elements, das der Import ablehnt.
 * @param {string} element lokaler Elementname, z. B. "inclusiveGateway"
 * @returns {string} Name mit dem BPMN-Begriff in Klammern
 */
function bpmnElementName(element) {
  const names = {
    inclusiveGateway: "ODER-Gateway (inclusiveGateway)",
    eventBasedGateway: "ereignisbasiertes Gateway (eventBasedGateway)",
    complexGateway: "komplexes Gateway (complexGateway)",
    intermediateCatchEvent: "Zwischenereignis (intermediateCatchEvent)",
    intermediateThrowEvent: "ausl\u00F6sendes Zwischenereignis (intermediateThrowEvent)",
    boundaryEvent: "angeheftetes Ereignis (boundaryEvent)",
    transaction: "Transaktion (transaction)",
    adHocSubProcess: "Ad-hoc-Teilprozess (adHocSubProcess)",
  };
  return names[element] || element || "?";
}

function stepOf(p, fallback) { return p.step ? `„${p.step}“` : (fallback || "Dieser Schritt"); }

/**
 * Das Merkmal eines Entscheidungs- oder Schleifenbefunds als Satzteil.
 *
 * Der Kern liefert die Element-ID in ``p.element``; ``nm`` loest sie gegen das
 * gezeigte Modell auf. Nach einer abgelehnten Loeschung steht das Element dort
 * noch -- so erscheint der Name, nicht der hypothetische Folgezustand.
 * @param {object} p Befund-Parameter
 * @param {string} [fallback] Satzteil, wenn der Kern kein Element nennt
 *   (aeltere Befunde); Standard „Das Merkmal“
 * @returns {string} z. B. „Betrag“ in deutschen Anfuehrungszeichen
 */
function merkmalOf(p, fallback) { return p.element ? `„${nm(p.element)}“` : (fallback || "Das Merkmal"); }

/**
 * Die betroffene Verzweigung eines K7-Befunds als Satzteil.
 *
 * Benannt: „die Verzweigung „Betrag hoch?““. Unbenannt traegt ``p.step`` schon
 * den Zusammenhang („Entscheidung nach „Betrag erfassen““, siehe ``nm``) und
 * wird nur mit Artikel versehen -- sonst stuende das Wort doppelt und die
 * Anfuehrungszeichen geschachtelt. ``p.step``/``p._nodeId`` ergaenzt
 * ``findingText`` aus ``node_id``.
 * @param {object} p Befund-Parameter
 * @returns {string} Satzteil ohne Satzzeichen; ohne Schritt „eine Verzweigung“
 */
function branchOf(p) {
  if (!p.step) return "eine Verzweigung";
  const node = p._nodeId && state.schema && (state.schema.nodes || {})[p._nodeId];
  if (node && !(node.label || "").trim()) return `die ${p.step}`;
  return `die Verzweigung „${p.step}“`;
}

/** „in=1, out>=2“ aus K2 als Satzteil. */
function degreeText(expected) {
  const m = /in(>=|=)(\d+), out(>=|=)(\d+)/.exec(expected || "");
  if (!m) return expected || "?";
  const q = (op, n, one, many) => `${op === ">=" ? "mindestens " : "genau "}${countLabel(Number(n), one, many)}`;
  return `${q(m[1], m[2], "Eingang", "Eingänge")} und ${q(m[3], m[4], "Ausgang", "Ausgänge")}`;
}

function findingText(f, opts) {
  const entry = f && f.code && FINDING_TEXTS[f.code];
  if (entry) {
    // ``step`` ergaenzen, wo der Kern nur ``node_id`` mitgibt.
    const params = Object.assign({}, f.params || {});
    if (!params.step && f.node_id) params.step = nm(f.node_id);
    // Fuer Satzteile, die die Knotenart brauchen (``branchOf``).
    if (f.node_id) params._nodeId = f.node_id;
    const t = entry(params);
    return opts && opts.withHint && t.hint ? `${t.text} ${t.hint}` : t.text;
  }
  let msg = (f && f.message) || "";
  const nodes = state.schema && state.schema.nodes;
  if (nodes) {
    msg = msg.replace(/'([A-Za-z0-9_-]+)'/g, (m, id) =>
      nodes[id] && nodes[id].label ? `„${nodes[id].label}“` : m);
  }
  return msg;
}

/** Befundzeile fuer Listen und Meldungen: „Regel – Text“ (Regel bleibt als Kuerzel stehen). */
function findingLine(f, opts) {
  return `${f.rule}: ${findingText(f, opts)}`;
}

function describeError(err) {
  // err.detail can be: string, {message}, {findings:[{rule,message,node_id,code,params}]}
  const d = err && err.detail;
  if (!d) return { title: err.message || "Fehler", lines: [] };
  // 403 der Rollenpruefung kommt als nacktes „forbidden“.
  if (err.status === 403 && d === "forbidden") {
    return { title: "Daf\u00FCr fehlt dir die Berechtigung.", lines: ["Wende dich an deine Administration, wenn du diese Aktion brauchst."] };
  }
  if (typeof d === "string") return { title: d, lines: [] };
  if (d.findings) {
    // Titel ohne Fachjargon; die Regel steht als Kuerzel vor jeder Zeile.
    return {
      title: d.findings.length === 1 ? "Diese Änderung ist nicht zulässig" : "Diese Änderung ist nicht zulässig – mehrere Gründe",
      // Gleich lautende Zeilen nur einmal: Mehrere Befunde koennen fachlich
      // identisch formuliert sein (gleiche Regel, gleicher Bezug).
      lines: [...new Set(d.findings.map((f) => findingLine(f, { withHint: true })))],
    };
  }
  // Ein Boundary-Befund mit Code (z. B. die SSRF-Pruefung der Webhooks) wird im
  // selben Katalog formuliert wie ein Regelbefund -- sonst stuende hier wieder
  // ein englischer Satz.
  if (d.code) return { title: findingText(d, { withHint: true }), lines: [] };
  if (d.message) return { title: d.message, lines: [] };
  return { title: "Fehler", lines: [] };
}

/**
 * Zeigt einen gescheiterten API-Aufruf als Fehlermeldung an.
 *
 * Buendelt das Muster ``describeError`` + ``toast("err", …)``, das fast jede
 * Aktion in ihrem ``catch`` braucht. Formuliert wird ausschliesslich in
 * ``describeError`` (Regelbefunde ueber ``findingText``, 403 als Satz); diese
 * Funktion reicht Titel und Zeilen nur unveraendert an ``toast`` weiter.
 *
 * @param {*} err Der gefangene Fehler (typisch ``{status, detail}`` aus
 *   ``request``, aber auch ein gewoehnlicher ``Error``).
 * @returns {void}
 */
function toastError(err) {
  const d = describeError(err);
  toast("err", d.title, d.lines);
}

