# Bedienung im späteren Pilot

Installation und Start ausschließlich nach separater Phase-1B-Freigabe.
Autostart ist zunächst ausgeschaltet. Es gibt keine Optionen für alternative Hostpfade.

Die App erzeugt `/share/scanpadde/{inbox,originals,failed,export}`.
Nur synthetische PDF-, TIFF-, PNG- oder JPEG-Dateien direkt nach `inbox` kopieren.
Unterverzeichnisse und Links werden nicht verarbeitet. Größe/mtime müssen mindestens
15 Sekunden unverändert beobachtet werden; der Poll dauert 2 Sekunden.
Das ist kein garantiertes Scanner-Fertig-Signal.

Dateien bleiben im Eingang. Archivnamen sind SHA-256 ohne Dateiendung. Fehler werden
in SQLite und im Dashboard angezeigt; `failed` und `export` bleiben reservierte,
leere Arbeitsordner. Es findet kein Verschieben statt. Unverändert fehlgeschlagene
Dateien werden nicht endlos erneut versucht. Geänderter Inhalt mit neuer Größe/mtime
führt nach erneuter Stabilisierung zu einer neuen Beobachtung.

Die Oberfläche zeigt System/DB/Worker, Inbox-/Job-Zahlen und die jüngsten 100 Quellen.
Die API bietet limit/offset (maximal 500) für Quellen und Jobs. Nur GET-Lesezugriffe.
Die DB liegt in `/data/scanpadde.db`; niemals ins Share verschieben.

## Sicherung und Fehler

Cold-Backup stoppt die App für den DB-Snapshot. Eine App-Sicherung allein ersetzt
keine separat verifizierte Sicherung von `/share/scanpadde`.
DB und Originalarchiv im gestoppten Zustand gemeinsam sichern/wiederherstellen.
Kein Einzelkopieren der DB während WAL-Betrieb. Vor Wiederherstellung Hashes prüfen.

Bei nicht erreichbarer DB oder Arbeitsordner liefert `api/health` 503 mit getrennter
Fehlerklasse. Bei Archiv-Integritätsfehlern nicht Dateien löschen/überschreiben; App
stoppen, Daten sichern und Ursache untersuchen. Es gibt bewusst keinen Web-Retry-Button.
Nach Prozessabbruch werden unter exklusiver Prozesssperre verwaiste running-Jobs
erneut eingeplant. Abgeschlossene Jobs bleiben abgeschlossen.
