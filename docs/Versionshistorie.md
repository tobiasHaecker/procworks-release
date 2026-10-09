# Versionshistorie

> **Bereitgestellt wird ausschließlich die aktuelle Version v1.33.2.**
> Alle älteren Versionen sind **veraltet**, dürfen nicht mehr installiert
> werden und können nicht mehr heruntergeladen werden; im Bereich „Releases“
> hat jede eine Seite mit diesem Hinweis. Sie enthalten Fehler, die in späteren Versionen
> behoben sind – welche, steht unten bei jeder Version. Wer eine ältere
> Version betreibt, aktualisiert bitte auf die aktuelle Version (siehe
> README, Abschnitt „Versionen und Updates“).

Je Version stehen hier Datum, Status und die **behobenen Fehler**. Neue
Funktionen und Änderungen beschreibt die Release-Seite der aktuellen Version.

## v1.33.2 (09.10.2026) – aktuell

### Behoben

- Mit PostgreSQL ließ sich kein Vorgang mehr starten und kein Prozessmodell,
  keine Vorlage und kein Organisationsmodell mehr anlegen: Die Anfrage blieb
  ohne Ende hängen. Mit Beispieldaten startete der Server gar nicht.

## v1.33.1 (09.10.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Zahlenangaben im Prozessmodell (Wertgrenzen von Entscheidungen und
  Wiederholungen, Grenzen von Eingabefeldern, Höchstdauer, Soll-Zeit,
  Prozesstermin, Zeitpunkt einer Eskalationsstufe) nahmen über die
  Schnittstelle auch „keine Zahl“ oder „unendlich“ an und wurden dann still
  übergangen.
- Vorgangsdaten vom Typ Kommazahl nahmen über die Schnittstelle „keine Zahl“
  oder „unendlich“ an; ein solcher Wert umging die Grenzen des Eingabefelds,
  und eine Entscheidung nach Wertbereichen nahm still den letzten Weg.
- Beim Umstellen eines einzelnen Vorgangs über die Schnittstelle ließen sich
  bereits erfasste Werte ohne Begründung überschreiben, und Werte wurden
  ungeprüft übernommen.
- Nach einem Update konnte der Browser eine zwischengespeicherte ältere
  Fassung des Web-Clients verwenden.
- Der Filter „Nur kritische“ der Arbeitsliste war falsch beschriftet.

## v1.33.0 (08.10.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version. Sie ändert die Bereitstellung: Das
Repository enthält nur noch die aktuelle Version, das Update läuft über
`git fetch` und `git reset --hard` (README, Abschnitt „Versionen und
Updates“), und diese Versionshistorie nennt für jede Version die behobenen
Fehler.

## v1.32.2 (07.10.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Das Web-Image wird wieder gebaut. Bei v1.32.1 scheiterte sein Bau; dort gab
  es nur das API-Image.
- Alle Fehlerbehebungen aus v1.32.1 sind enthalten.

## v1.32.1 (07.10.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.** Diese Version wurde nie vollständig ausgeliefert: Ihr
Web-Image fehlte.

### Behoben

- Mehrere API-Prozesse gegen dieselbe Datenbank (etwa unter Kubernetes)
  vergeben nie mehr dieselbe Kennung für einen neuen Vorgang, Prozess, eine
  Vorlage oder ein Organisationsmodell. Bisher konnte ein neuer Eintrag ohne
  Hinweis einen bestehenden ersetzen. Docker Compose startet nur einen
  API-Prozess und war nicht betroffen.

## v1.32.0 (02.10.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Nach dem Abmelden oder einem Wechsel der Anmeldung im selben Browser blieb
  der zuletzt geöffnete Vorgang der vorigen Person sichtbar. Jetzt werden alle
  Daten der vorigen Sitzung verworfen.
- Aufgaben aus Test-Instanzen erschienen in echten Arbeitslisten und ließen
  sich dort übernehmen und abschließen.
- Die automatische Aktualisierung verwarf ungespeicherte Eingaben; ein Klick
  neben einen Dialog verwarf ebenfalls schon gemachte Eingaben.
- Das Initialpasswort eines neu angelegten Logins wurde nicht angezeigt.
- Leser bekamen Aktionen angeboten, die sie nicht ausführen dürfen.
- Unumkehrbare Aktionen ließen sich versehentlich per Enter auslösen.
- Der E-Mail-Ausgang meldete ohne Mailserver „zugestellt“ statt „verworfen“.
- „Niemand zuständig“ übersah Schritte, deren Zuständige keinen Login haben.
- Monitoring-Kacheln, Durchlaufzeit und Engpässe zählten unterschiedliche
  Vorgänge.
- Ein leerer Entscheidungszweig war unsichtbar und ließ sich nicht wieder
  füllen; das „+“ am Anfang eines Zweigs meldete einen Fehler.
- Die klassische Modellier-Ansicht bot nicht alle Funktionen der
  Karten-Ansicht.
- „+ Datenbindung“ in der Datensicht band immer als Pflicht.
- Eigene Vorlagen ließen sich nicht mehr löschen.
- Ein Entscheidungsmerkmal, das nicht gelöscht oder umgetypt werden kann,
  wurde mit einer irreführenden Meldung abgelehnt; Modellhinweise nannten
  interne Kennungen statt Schrittnamen.
- Im Order-to-Cash-Beispiel entschied ein unklar beschrifteter Haken über
  Verbuchung oder Mahnung; die Maske fragt jetzt „Zahlung eingegangen?“.
- Der Masken-Designer verlor den ersten Klick auf „+ Feld hinzufügen“;
  mehrspaltige Eingabemasken wurden gequetscht dargestellt; Ankreuzfelder
  trugen einen Pflicht-Stern ohne Wirkung.
- Darstellung: überlappende Beschriftungen im Kontrollfluss, zu schmale
  Dialoge, Meldungen über den Knöpfen offener Dialoge, Kopfzeile bei
  Laptop-Breite, Tour-Popups über ihrem Ziel, schwarze Kästen in der
  Prozesslandkarte, Smartphone-Ansicht der Listen und der Anmeldung, zu
  geringer Kontrast im hellen Farbschema.
- Englische Rohtexte und nicht-deutsche Zahlen- und Datumsformate in der
  Oberfläche.
- Der Webhook-Probelauf zeigte nach einer Änderung das alte Ergebnis.
- Hilfe, Touren und Mitarbeiter-Anleitung stimmten nicht mit der Oberfläche
  überein.

## v1.31.0 (01.10.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Das Helm-Chart ließ sich ohne eigene Werte nicht installieren, weil es einen
  Platzhalter statt der veröffentlichten Images verwendete.

## v1.30.0 (28.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Bearbeiter sahen und lösten Inzidente auch von Vorgängen, an denen sie nicht
  beteiligt sind.
- Eine falsche API-Adresse in der Seitenleiste führte zu unverständlichen
  Meldungen bei jedem Aufruf.
- Dialoge ließen sich nicht vollständig per Tastatur bedienen; die Seite
  dahinter blieb bedienbar.

## v1.29.0 (28.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Aktionen auf Vorgänge, an denen ein Bearbeiter nicht beteiligt
  ist, verraten nichts mehr über diese Vorgänge; der Server antwortet wie beim
  Lesen mit „nicht gefunden“.
- Ein Schritt mit Soll-Zeit, Priorität oder E-Mail-Benachrichtigung ließ sich
  nicht ad hoc aus einem laufenden Vorgang entfernen.

## v1.28.0 (27.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Laufende Vorgänge ändern nur noch Modellierer und
  Administratoren, bei echten Vorgängen mit Pflicht-Anlass; der Verlauf nennt
  Person, Anlass und Schritt.
- Sicherheit: Bearbeiter stellen nur noch Vorgänge, an denen sie beteiligt
  sind, auf eine neue Prozessversion um.
- Sicherheit: Beispieldaten legen keine Anmeldungen mit bekanntem Passwort
  mehr an.
- Vier-Augen-Prinzip: Eine Abteilungsleitung konnte ihre eigene Arbeit
  genehmigen. Jetzt ist die übergeordnete Abteilung zuständig, und keine
  Vertretung gibt eine Genehmigung an die Person zurück, deren Arbeit
  genehmigt wird.

## v1.27.2 (27.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Nach einem Neustart des Servers konnten neue Vorgänge und Prozesse
  gespeicherte mit gleicher Kennung ersetzen. Die Zählung setzt jetzt hinter
  dem gespeicherten Bestand fort.

## v1.27.1 (27.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Beim Abschließen eines Schritts lassen sich nur noch Werte
  setzen, die dieser Schritt schreibt.

## v1.27.0 (27.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Der Abschluss eines Schritts prüfte übergebene Werte nicht gegen ihren
  Datentyp; ein ungültiger Ja/Nein-Wert konnte still eine Verzweigung
  steuern.

## v1.26.0 (27.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Ein nicht-numerischer Schwellwert verschwand still statt als Fehler gemeldet
  zu werden.
- Lange Bezeichnungen sprengten Dialoge; Links waren im dunklen Farbschema
  schlecht lesbar.
- Die Einführung für Administratoren ließ sich am Smartphone nicht
  durchlaufen.
- Uneinheitliche Begriffe und Umschreibungen statt Umlauten in der
  Oberfläche.

## v1.25.0 (27.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Ein Neustart des Servers meldete alle Personen ab. Mit PostgreSQL
  überstehen Anmeldungen jetzt Neustart und Update; Passwort-Reset und
  Löschen beenden die Anmeldung sofort.
- Fehlermeldungen blieben auch nach einer erfolgreichen Folgeaktion stehen.
- Das Monitoring zeigte eine ausgewählte Instanz nicht sofort im aktuellen
  Zustand.

## v1.24.0 (26.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Vorgangsdaten lassen sich nicht mehr unbemerkt ändern.
  Bearbeiter setzen nur Werte ihres eigenen offenen Schritts, Korrekturen
  durch Modellierer und Administratoren brauchen eine Begründung, jede
  Änderung steht im Verlauf.
- Sicherheit: Bearbeiter sehen nur noch Vorgänge, an denen sie beteiligt sind
  oder waren.
- Sicherheit: Anmeldeversuche werden nach wiederholten Fehlversuchen
  gedrosselt.
- Sicherheit: Die Oberfläche lässt sich nicht mehr in fremde Seiten einbetten
  und lädt nur eigene Skripte.
- Sicherheit: Das Testlesen eines Connectors ist auf angebotene Tabellen und
  auf Modellierer und Administratoren beschränkt.
- Etwa 180 Prüfmeldungen erschienen als englischer Rohtext.
- Vorgänge, für die niemand zuständig ist, fielen nur in der Detailansicht
  auf; das Monitoring zeigt sie jetzt.
- Eine Bearbeiterregel mit „außer“, die niemanden übrig lässt, ließ sich
  freigeben.
- Ein ad hoc eingefügter Schritt hatte keinen Bearbeiter und stand in keiner
  Arbeitsliste.
- Ja/Nein-, Zahl- und Datumsfelder wurden ohne Eingabemaske still umgedeutet.

## v1.23.2 (24.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Eine Schleife, die eine Verzweigung überkreuzt, wurde beim BPMN-Import nicht
  abgewiesen; Vorgänge solcher Modelle konnten hängen bleiben.
- Ein Login ohne Bearbeiterzuordnung konnte im Namen einer anderen Person
  arbeiten. Persönliche Logins handeln jetzt nur noch als sie selbst.

## v1.23.1 (23.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Ein BPMN-Import konnte ein vorhandenes, auch freigegebenes Modell
  überschreiben. Ein Import legt jetzt immer einen neuen Prozess an.

## v1.23.0 (23.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Zwei Schritte der geführten Tour ließen sich bei Laptop-Höhe nicht
  ausführen.
- Die Soll/Ist-Karte war beim ersten Öffnen leer.
- Teilprozesse galten im Soll/Ist als „nie ausgeführt“; endete ein Vorgang
  mit einem Teilprozess, fehlte sein Abschluss im Verlauf und in den
  Kennzahlen.
- Der Verlauf nannte bei einer Migration „System“ statt der auslösenden
  Person.
- Die Mehrfachzuordnung fehlte im Dialog der Ressourcensicht.
- Die Beispieldaten hatten keinen Zeitverlauf, die Zeitauswertung zeigte
  deshalb nichts.
- Webhook-Meldungen erschienen auf Englisch und ohne Vorschlag.

## v1.22.0 (23.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Das Testlesen einer Datenanbindung zeigte keine Datensätze.
- „Meine Aufgaben“ hing vom oben gewählten Prozess ab und zeigte teils
  interne Kennungen statt Namen.
- Ein BPMN-Export und -Reimport verlor Bearbeiterregeln, Eingabemasken,
  Termine, Prioritäten, Eskalationen und E-Mail-Bindungen.

## v1.21.0 (22.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.20.0 (21.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Der Masken-Designer verlor beim Speichern die Hilfetexte der Felder.

## v1.19.0 (21.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Ausgehende Webhooks erreichen nur zulässige Ziele. Das Ziel wird
  bei jeder Zustellung erneut geprüft, Weiterleitungen werden nicht befolgt,
  und eine Egress-Sperre stoppt auch bereits eingereihte Zustellungen.

## v1.18.0 (21.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Eine Migration konnte einem späteren Schritt eine Pflichteingabe entziehen.
  Sie verlangt dafür jetzt einen Startwert.

## v1.17.1 (21.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Fehlermeldungen verschwanden nach sieben Sekunden, oft bevor sie gelesen
  waren.
- Die Schritt-Karte verlangte fälschlich einen Dienst für die Freigabe.
- Zwei Schritte der Modellierer-Tour ließen sich in der Karten-Ansicht nicht
  ausführen.
- Die Hinweisleiste der Online-Demo verdeckte Bedienelemente.

## v1.17.0 (21.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Ein Login ohne Bearbeiterzuordnung konnte einen Schritt mit
  Bearbeiterregel ohne Prüfung abschließen, auch am Vier-Augen-Prinzip vorbei.
  Ein solcher Abschluss verlangt jetzt eine Begründung und steht als
  Aufsichtseingriff im Verlauf.
- Der Verlauf nannte bei solchen Abschlüssen „System“ statt der Person.

## v1.16.3 (09.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Bibliotheken des Webservers im Web-Image auf Stände ohne
  bekannte Lücken angehoben.

## v1.16.2 (09.09.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Überkreuzte Verzweigungen wurden nicht erkannt; solche Modelle ließen sich
  freigeben, ihre Vorgänge aber nie abschließen. Betroffen war vor allem der
  BPMN-Import.
- Bedingte Folgeprozesse konnten den Abschluss eines Vorgangs dauerhaft
  blockieren.
- Teilprozess-Eingaben wurden nicht darauf geprüft, ob sie beim Aufruf
  gefüllt sind.
- Externe Datenelemente ließen sich nicht als Pflichtangabe lesen; bei
  Datensatz-Bindungen fehlte die Prüfung des Schlüsselfelds.
- Die geführte Tour zeigte bei einer Ablehnung nur „Fehler“ statt der
  Begründung.

## v1.16.1 (30.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.16.0 (30.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Das Web-Image übersetzt seinen Webserver mit aktueller
  Go-Version; bekannte Lücken der vorher übernommenen Fassung entfallen.
- Sicherheit: Das API-Image spielt die Sicherheitsupdates seines Basis-Image
  ein.
- Dokumentation: zerstörte Sonderzeichen in README und Anleitungen
  wiederhergestellt; die Kern-README beschreibt den tatsächlichen
  Funktionsumfang.

## v1.15.0 (09.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.14.0 (09.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Ein Schritt mit Zeitvorgabe oder Benachrichtigung ließ sich nicht
  entfernen.

## v1.13.0 (03.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Der Beispiel-Beschaffungsantrag ließ sich starten, aber von niemandem zu
  Ende spielen, weil für den Einkauf keine Anmeldung existierte.

## v1.12.0 (02.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.11.0 (02.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.10.0 (02.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Sicherheit: Das API-Image enthält kein Build-Werkzeug mehr; der
  Sicherheits-Scan meldet keinen Fund.

## v1.9.2 (02.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Erste Version der Reihe 1.9 mit beiden Container-Images; für v1.9.0 und
  v1.9.1 fehlten sie ganz oder teilweise.
- Sicherheit: eine Bibliothek im API-Image auf einen Stand ohne bekannte Lücke
  angehoben.

## v1.9.1 (02.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.** Diese Version wurde nie vollständig ausgeliefert: Ihr
API-Image fehlte.

### Behoben

- Sicherheit: eine Bibliothek im API-Image auf einen Stand ohne bekannte Lücke
  angehoben.

## v1.9.0 (02.08.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.** Diese Version wurde nie vollständig ausgeliefert: Ihre
Container-Images fehlten.

### Behoben

- „Frist ändern“ öffnete den Dialog ohne die gesetzten Werte.
- Die Datenherkunft-Pfeile lagen nach dem Anklicken eines Schritts meist
  außerhalb des sichtbaren Bereichs.

## v1.8.1 (21.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Auf niedrigen Fenstern war das untere Ende von Menü und rechter Spalte
  nicht erreichbar.

## v1.8.0 (21.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Der Seitenhintergrund verrutschte beim Scrollen.
- „Weiter“ und „Zurück“ im Tutorial flackerten und ließen sich kaum klicken.
- Nach einem Benutzerwechsel im selben Browser wurde das Tutorial nicht mehr
  angeboten.
- Zwei Schritte der Modellierer-Tour ließen sich nicht ausführen; die Seite
  ließ sich während eines Mitmach-Schritts nicht rollen.

## v1.7.1 (19.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- In der Online-Demo verdeckte die Hinweisleiste das Tutorial-Fenster.
- Ein Tutorial-Fenster mit langem Text ließ sich nicht weiterklicken.

## v1.7.0 (19.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Ein Schritt der Modellierer-Tour ließ sich nicht ausführen.
- In „Meine Aufgaben“ konnten Bereiche doppelt erscheinen.
- Während der geführten Tour ließ sich kein Datum auswählen.
- Online-Demo: Beim Start erschien gelegentlich eine weiße Seite.

## v1.6.0 (18.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- iOS: Safari zoomte beim Antippen eines Eingabefelds hinein und nicht wieder
  heraus; Seitenende und Menü lagen hinter der Browserleiste.

## v1.5.0 (16.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.4.0 (16.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Online-Demo: Neue Demo-Instanzen starteten nicht (Fehler 502). Die
  ausgelieferte Software war nicht betroffen.

## v1.3.0 (13.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Beim Verschieben des Kontrollflusses wurden Seiteninhalte markiert.
- Beschriftungen der Datenherkunft-Linien wurden am oberen Rand
  abgeschnitten.

## v1.2.0 (08.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.1.0 (04.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.0.1 (04.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

Keine Fehlerbehebungen in dieser Version.

## v1.0.0 (04.07.2026) – veraltet

**Veraltet – nicht mehr verfügbar; bitte auf die aktuelle Version
aktualisieren.**

### Behoben

- Der Web-Client startete nicht und zeigte dauerhaft „getrennt“.
- Der Beispielprozess „Beschaffungsantrag“ ließ sich in der Oberfläche nicht
  abschließen.
