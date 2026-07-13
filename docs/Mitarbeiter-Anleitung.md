# ProcWorks – Anleitung für Mitarbeiter (Aufgaben bearbeiten)

Diese Anleitung richtet sich an **Sachbearbeiter**, die sich anmelden, ihre
persönliche Arbeitsliste sehen und Aufgaben abarbeiten sollen. Sie besteht aus
zwei Teilen:

- **Teil A** – Was der Administrator **einmalig** vorbereitet (Voraussetzungen).
- **Teil B** – Die **fertige Anleitung zum Weitergeben** an den Mitarbeiter.

> ⚠️ **Hinweis zur Haftung.** ProcWorks wird ohne jede Gewährleistung und – soweit
> gesetzlich zulässig – **ohne jede Haftung** bereitgestellt; die Nutzung erfolgt
> auf eigenes Risiko. Fachliche Ergebnisse, Entscheidungen und Daten liegen in der
> Verantwortung des Betreibers und der Anwender. Details:
> [DISCLAIMER.md](../DISCLAIMER.md).

---

## Teil A: Voraussetzungen (durch den Administrator)

Damit ein Mitarbeiter überhaupt Aufgaben sieht, müssen diese Punkte erfüllt sein:

1. **Prozess ist freigegeben (RELEASED).**
   Nur ein freigegebenes Schema lässt sich instanziieren. Status in der Sicht
   **Modellieren** prüfen.

2. **Mindestens eine Instanz läuft.**
   In der Sicht **Ausführung** den Prozess starten, damit eine laufende Instanz
   mit offenen Aufgaben existiert.

3. **Der Agent ist für die Aufgabe berechtigt.**
   Die Bearbeiterregeln (Z/A, z. B. rollenbasiert) im Organisationsmodell
   (Sicht **Ressourcensicht**) müssen den Agenten als berechtigt ausweisen.
   Nur dann erscheint die Aufgabe in seiner Liste.

4. **Login anlegen – gebunden an den Agenten, mit Rolle „Bearbeiter".**
   In der **Ressourcensicht** in der Zeile des Agenten auf **„Login"** klicken:
   - Rolle **Bearbeiter** (operator) auswählen — **zwingend**; ohne diese Rolle
     sieht und erledigt der Mitarbeiter keine Aufgaben.
   - Den Login-Vorschlag (`vorname.nachname`) übernehmen oder anpassen.
   - Das **Initialpasswort** wird **einmalig** angezeigt → notieren und dem
     Mitarbeiter sicher übermitteln.

> Weil der Login direkt über den **„Login"-Button in der Agentenzeile** erzeugt
> wird, ist er fest mit genau diesem Agenten verknüpft. Dadurch zeigt
> „Meine Aufgaben" automatisch *seine* Aufgaben (inkl. Vertretungen) – ganz ohne
> Personenauswahl.

---

## Teil B: Anleitung für den Mitarbeiter (zum Weitergeben)

**ProcWorks – Anmelden und Aufgaben bearbeiten**

1. **Seite öffnen.**
   Rufe im Browser die ProcWorks-Adresse auf:
   `http://<server-adresse>` (bzw. `https://<eure-domain>`).
   Es erscheint sofort ein **Anmeldefenster**.

2. **Anmelden.**
   Gib deinen **Login** (Form `vorname.nachname`) und das **Initialpasswort**
   ein, das du erhalten hast.

3. **Eigenes Passwort vergeben.**
   Beim ersten Login wirst du aufgefordert, ein **eigenes Passwort** zu setzen
   (mindestens 8 Zeichen). Danach bist du direkt angemeldet.

4. **Zur Aufgabenliste wechseln.**
   Klicke links in der Navigation auf **„Meine Aufgaben"**.
   - Oben siehst du „✓ Angemeldet als &lt;dein Name&gt;".
   - Darunter stehen unter **„Offene Aufgaben"** deine Aufgaben
     (Spalten: Aufgabe, Prozess, Berechtigte).

5. **Aufgabe erledigen.**
   Klicke bei einer Aufgabe auf **„Erledigen"**, fülle ggf. die abgefragten
   Daten aus und bestätige. Die Aufgabe verschwindet danach aus der Liste;
   Folgeaufgaben erscheinen automatisch.

6. **Abwesenheit eintragen (Urlaub / Vertretung).**
   Unten in **„Meine Aufgaben"** findest du den Bereich
   **„Abwesenheit / Vertretung"**. Trage dort einen Zeitraum ein (**Von** / **Bis**,
   optional eine Notiz wie „Urlaub") und klicke auf **„Abwesenheit eintragen"**.
   Solange du abwesend bist, erhält deine hinterlegte **Vertretung** deine
   Aufgaben **zusätzlich** – du selbst behältst sie ebenfalls, es geht also nichts
   verloren. Bestehende Einträge kannst du dort auch wieder **entfernen**.
   - Ist **keine Vertretung** hinterlegt, weist ein Hinweis darauf hin: Deine
     Aufgaben bleiben dann während der Abwesenheit **dir** zugewiesen (die
     Vertretung legt der Administrator in der Organisation fest).

7. **Abmelden.**
   Über **„Abmelden"** in der linken Seitenleiste. Beim nächsten Mal meldest du
   dich mit deinem selbst gewählten Passwort an. Das Passwort kannst du jederzeit
   über **„Passwort ändern"** anpassen.

**Falls keine Aufgaben angezeigt werden:**

- „Keine offenen Aufgaben" bedeutet, dass dir aktuell nichts zugewiesen ist –
  das ist normal, solange nichts ansteht.
- Steht oben „Kein Schema ausgewählt", wähle den Prozess im Auswahlfeld
  (oben rechts) aus.

---

## Hinweise zur Einordnung

- **Eine einzige Seite** genügt dem Mitarbeiter: die normale App-Adresse.
  Login-Fenster, Aufgabenliste und Bearbeitung sind alle dort enthalten.
- Mit der Rolle **Bearbeiter** (operator) blendet die Navigation automatisch nur
  **„Meine Aufgaben"** (und **Monitoring**) ein – Modellier- und Admin-Sichten
  bleiben verborgen.
- Ein **Modellierer** (modeler) ist zugleich Bearbeiter: er sieht „Meine
  Aufgaben" und „Ausführung" zusätzlich zu den Modellier-Sichten und kann eigene
  **Entwürfe als Test-Instanz** starten (diese Testläufe zählen nicht ins
  Monitoring). Für reine Aufgabenbearbeitung genügt die Rolle **Bearbeiter**.
- Reine Sachbearbeiter-Aufgaben werden vollständig über **„Meine Aufgaben"**
  erledigt. **XOR-Verzweigungen entscheidet das System automatisch** anhand der
  erfassten Daten (vollständige, überschneidungsfreie Partition, K7) – es gibt
  keinen manuellen „Zweig wählen"-Schritt mehr.
