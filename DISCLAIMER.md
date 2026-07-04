<!-- SPDX-License-Identifier: BUSL-1.1 -->

# Haftungsausschluss / Disclaimer of Liability

> **Kurzfassung:** ProcWorks wird **„wie besehen" (as is)**, **ohne jede
> Gewährleistung** und **ohne jede Haftung** bereitgestellt. Wer die Software
> herunterlädt, installiert, in Betrieb nimmt oder nutzt, tut dies
> **ausschließlich auf eigenes Risiko und in eigener Verantwortung**. Es wird –
> im größtmöglichen gesetzlich zulässigen Umfang – **keine Haftung für irgendeinen
> Schaden** übernommen, weder im Zusammenhang mit der **Inbetriebnahme** (z. B.
> Schäden an Servern, Hardware, Betriebssystemen, paralleler oder
> Drittsoftware, Netzwerken oder sonstiger Infrastruktur) noch mit der
> **Nutzung** (z. B. Verlust, Beschädigung oder Offenlegung von Daten,
> fehlerhafte, verzögerte oder unterbrochene Geschäftsprozesse, Betriebs- oder
> Vermögensschäden).

Dieser Haftungsausschluss ergänzt und konkretisiert den Gewährleistungs- und
Haftungsausschluss der Lizenz (**Business Source License 1.1**, Abschnitt 8
„Disclaimer", siehe [LICENSE](LICENSE)). Bei Widersprüchen gilt der jeweils
**weitestgehende** zulässige Haftungsausschluss.

---

## 1. Bereitstellung „wie besehen", keine Gewährleistung

Die Software, ihr Quellcode, ihre Dokumentation, die Beispieldaten und alle
zugehörigen Materialien (zusammen das „**Werk**") werden **„wie besehen" und „wie
verfügbar"** bereitgestellt. Der Lizenzgeber (Tobias Häcker, „**wir**") gibt
**keine** ausdrücklichen oder stillschweigenden Zusicherungen oder
Gewährleistungen ab. Ausdrücklich ausgeschlossen sind – soweit gesetzlich
zulässig – insbesondere Gewährleistungen der

- **Marktgängigkeit** und **Eignung für einen bestimmten Zweck**,
- **Mangelfreiheit**, **Fehler-, Unterbrechungs- oder Virenfreiheit**,
- **Verfügbarkeit**, **Sicherheit**, **Vollständigkeit** oder **Aktualität**,
- **Nicht-Verletzung** von Rechten Dritter,
- **Richtigkeit von Ergebnissen** (insbesondere von Modell-, Validierungs-,
  Ausführungs-, Monitoring- oder Migrationsergebnissen).

Insbesondere sichern wir **nicht** zu, dass das Werk fehlerfrei arbeitet,
ununterbrochen verfügbar ist, bestimmte Korrektheits-, Sicherheits- oder
Compliance-Anforderungen erfüllt oder für einen konkreten Einsatzzweck geeignet
ist. Die im Projekt beschriebene „Correctness by Construction" bezieht sich auf
definierte **strukturelle Modellregeln** und stellt **keine** Zusicherung der
fachlichen Richtigkeit, Rechtskonformität oder Betriebssicherheit eines real
ausgeführten Prozesses dar.

## 2. Haftungsausschluss bei Inbetriebnahme und Betrieb

Soweit gesetzlich zulässig, haften wir **nicht** für Schäden, die im
Zusammenhang mit dem **Bezug, der Installation, der Konfiguration, der
Inbetriebnahme, dem Betrieb, der Aktualisierung oder der Deinstallation** des
Werks entstehen. Das umfasst – ohne Beschränkung – Schäden an oder durch:

- **Server, Hardware, Speichermedien** und sonstige Geräte,
- **Betriebssysteme**, **Container-/Virtualisierungsumgebungen** (z. B. Docker,
  Kubernetes) und deren Konfiguration,
- **parallel betriebene oder andere Software**, Dienste und Datenbanken auf
  demselben oder einem verbundenen System,
- **Netzwerke**, **Cloud-Ressourcen** und sonstige **Infrastruktur**,
- **Ressourcenverbrauch** (z. B. Rechenzeit, Speicher, Bandbreite, Kosten von
  Cloud-Diensten),
- **Sicherheitsvorfälle**, unbefugten Zugriff oder Fehlkonfigurationen
  (einschließlich des Betriebs im offenen Entwicklungsmodus ohne
  Authentifizierung, siehe [SECURITY.md](SECURITY.md)).

Der Betreiber ist allein dafür verantwortlich, das Werk vor einem produktiven
Einsatz **in einer geeigneten, isolierten Umgebung zu prüfen**, geeignete
**Sicherungs- (Backup-)**, **Wiederherstellungs-** und **Sicherheitsmaßnahmen**
zu treffen und die Eignung für die eigene Systemlandschaft sicherzustellen.

## 3. Haftungsausschluss bei Nutzung (Daten und Prozesse)

Soweit gesetzlich zulässig, haften wir **nicht** für Schäden, die im
Zusammenhang mit der **Nutzung** des Werks entstehen, insbesondere nicht für:

- **Verlust, Beschädigung, Verfälschung, Offenlegung oder Nichtverfügbarkeit von
  Daten** (einschließlich Prozess-, Instanz-, Audit-, Organisations-,
  Anmelde- und Stammdaten sowie über Connectoren angebundener externer Daten),
- **fehlerhafte, unvollständige, verzögerte, übersprungene oder nicht
  ausgeführte Geschäftsprozesse, Aufgaben, Migrationen oder Entscheidungen**,
- **fehlerhafte Auswertungen** (KPIs, Monitoring, Prozesskarten) und darauf
  gestützte geschäftliche Entscheidungen,
- **Betriebsunterbrechungen**, entgangenen Gewinn, entgangene Einnahmen oder
  Einsparungen, Rufschädigung, Vertrags- oder Compliance-Verstöße,
- jede **mittelbare, unmittelbare, zufällige, besondere, pönale Schäden sowie
  Folgeschäden** gleich welcher Art und unabhängig von der Haftungsgrundlage
  (Vertrag, Delikt, Gefährdungshaftung oder sonst).

Die Verantwortung für die **fachliche Richtigkeit**, die **Rechts- und
Compliance-Konformität**, die **Datensicherung** und die **Eignung** der mit dem
Werk modellierten und ausgeführten Prozesse liegt **ausschließlich beim
Betreiber/Nutzer**.

## 4. Externe Systeme und Connectoren

Das Werk kann über Connectoren (z. B. MS SQL, MySQL, MS Dynamics 365, SAP) auf
**externe Systeme** zugreifen. Für diese externen Systeme, ihre Daten, ihre
Verfügbarkeit, ihre Lizenzbedingungen sowie für die Folgen lesender oder
schreibender Zugriffe übernehmen wir **keine** Verantwortung und **keine**
Haftung. Proprietäre Treiber/Client-Bibliotheken Dritter werden **nicht**
mitgeliefert und unterliegen den Bedingungen ihrer jeweiligen Anbieter.

## 5. Drittkomponenten

Das Werk nutzt Open-Source-Abhängigkeiten Dritter (siehe
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)). Für deren Inhalt, Sicherheit
und Funktion gelten ausschließlich die jeweiligen Lizenz- und
Haftungsbestimmungen der Drittanbieter; eine Haftung unsererseits hierfür ist
ausgeschlossen.

## 6. Beiträge

Beiträge zum Projekt werden ebenfalls ohne Gewährleistung und ohne Haftung
bereitgestellt. Wer Code, Dokumentation oder andere Inhalte beiträgt, übernimmt
hierfür keine Gewähr gegenüber Nutzern des Werks.

## 7. Eigenverantwortung des Betreibers (Obliegenheiten)

Vor und während des Einsatzes obliegt es dem Betreiber/Nutzer insbesondere,

1. das Werk in einer **Test-/Staging-Umgebung** zu evaluieren, bevor es
   produktiv oder mit echten Daten eingesetzt wird,
2. **regelmäßige, geprüfte Backups** anzulegen und deren Wiederherstellung zu
   testen,
3. angemessene **Sicherheits-, Zugriffs- und Netzwerkmaßnahmen** umzusetzen
   (kein offener Modus in exponierten Umgebungen),
4. die **rechtlichen, regulatorischen und datenschutzrechtlichen** Anforderungen
   (z. B. DSGVO) eigenständig zu erfüllen,
5. die **Eignung** des Werks für den konkreten Zweck eigenverantwortlich zu
   beurteilen.

## 8. Gesetzlich zwingende Haftung (salvatorische Klarstellung)

Die vorstehenden Haftungsausschlüsse und -beschränkungen gelten **nur, soweit
gesetzlich zulässig**. Sie schließen oder beschränken **nicht** eine Haftung, die
nach zwingendem anwendbarem Recht nicht ausgeschlossen oder beschränkt werden
kann. Nach **deutschem Recht** bleibt insbesondere unberührt die Haftung

- für Schäden aus der **Verletzung des Lebens, des Körpers oder der Gesundheit**,
  die auf einer fahrlässigen oder vorsätzlichen Pflichtverletzung beruhen,
- für **sonstige Schäden** aus **Vorsatz** oder **grober Fahrlässigkeit**,
- nach dem **Produkthaftungsgesetz (ProdHaftG)**,
- im Umfang einer ausdrücklich übernommenen **Garantie** oder bei **arglistig
  verschwiegenen** Mängeln.

Im Übrigen ist – soweit nicht zwingendes Recht entgegensteht – jede Haftung
ausgeschlossen. Sollte eine Bestimmung dieses Haftungsausschlusses ganz oder
teilweise unwirksam sein, bleibt die Wirksamkeit der übrigen Bestimmungen
unberührt; an die Stelle der unwirksamen Bestimmung tritt die gesetzlich
zulässige Regelung, die dem wirtschaftlichen Zweck am nächsten kommt.

## 9. Keine Rechtsberatung

Dieser Text ist Teil der Projektunterlagen und stellt **keine Rechtsberatung**
dar. Für eine auf den konkreten Einsatz zugeschnittene, rechtsverbindliche
Gestaltung sollte fachkundiger Rechtsrat eingeholt werden.

---

# Disclaimer of Liability (English summary)

> ProcWorks is provided **"as is"**, **without any warranty** and **without any
> liability**. You download, install, deploy and use it **entirely at your own
> risk and responsibility**. To the maximum extent permitted by applicable law,
> we accept **no liability for any damage whatsoever**, whether arising from
> **deployment/operation** (e.g. damage to servers, hardware, operating systems,
> parallel or third-party software, networks or other infrastructure) or from
> **use** (e.g. loss, corruption or disclosure of data; faulty, delayed or
> interrupted business processes; business interruption, lost profits or other
> economic loss).

This disclaimer supplements Section 8 ("Disclaimer") of the Business Source
License 1.1 (see [LICENSE](LICENSE)). Nothing herein excludes or limits
liability that cannot be excluded or limited under mandatory applicable law
(for example, under German law: injury to life, body or health; intent or gross
negligence; the Product Liability Act; an expressly assumed guarantee; or
fraudulently concealed defects). The German text above is authoritative.

© 2026 Tobias Häcker · Business Source License 1.1
