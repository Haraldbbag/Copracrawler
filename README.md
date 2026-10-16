# Copracrawler

Erfasst einmal täglich den Speicherverbrauch aller sichtbaren Projekte in
[COPRA](https://copra.app) und schreibt ihn in eine CSV-Datei – als Grundlage
für die spätere Auswertung und Weiterberechnung.

> COPRA hat keine offizielle API. Das Skript nutzt dieselbe Schnittstelle wie
> die Web-App (`https://api.copra.app/v2`). Ändert COPRA diese, bricht das
> Skript mit einer klaren Fehlermeldung ab, statt falsche Zahlen zu schreiben.

## Einrichtung

1. Python 3.8 oder neuer installieren.
2. Abhängigkeit installieren:
   ```
   pip install -r requirements.txt
   ```
3. `config.example.ini` nach `config.ini` kopieren und E-Mail und Passwort des
   COPRA-Accounts eintragen (startet man das Skript ohne `config.ini`, legt es
   die Datei selbst aus der Vorlage an). Optional `csv_ordner` setzen.
4. Testen, ohne etwas zu schreiben:
   ```
   python copra_speicher.py --probelauf
   ```
5. Täglichen Start in der Windows-Aufgabenplanung einrichten, z. B.
   Programm `python.exe` (bzw. `pyw.exe` ohne Konsolenfenster),
   Argumente `C:\Copracrawler\copra_speicher.py`.
   Empfehlung: *„Aufgabe so schnell wie möglich nach einem verpassten Start ausführen“*.

### Sicherheit

`config.ini` enthält das Passwort im Klartext und ist per `.gitignore` von Git
ausgenommen. Dateirechte so setzen, dass nur der Benutzer der geplanten Aufgabe
sie lesen kann, und am besten einen eigenen COPRA-Account nur für das Skript
verwenden (ohne Zwei-Faktor-Anmeldung).
Alternativ können `COPRA_EMAIL` und `COPRA_PASSWORT` als Umgebungsvariablen
gesetzt werden – sie haben Vorrang vor der Datei.

## Aufruf

| Option | Wirkung |
|---|---|
| *(keine)* | Abrufen und an die CSV anhängen. Ist der Tag schon erfasst, wird übersprungen. |
| `--probelauf` | Nur abrufen und anzeigen, nichts schreiben |
| `--ersetzen` | Werte von heute neu abrufen und überschreiben |
| `--config PFAD` | Andere `config.ini` verwenden |
| `-v` | Ausführlicheres Log |

Rückgabewerte: `0` = OK/übersprungen, `1` = Abruf- oder Schreibfehler, `2` = Konfigurationsfehler.

## Ausgabedateien

Alle im `csv_ordner` (Standard: Ordner des Skripts). Format: Semikolon als
Trenner, UTF-8 mit BOM – öffnet sich direkt korrekt im deutschen Excel.

**`copra_speicher.csv`** – eine Zeile pro Projekt und Tag

| Spalte | Inhalt |
|---|---|
| `datum` | Tag des Abrufs (JJJJ-MM-TT) |
| `projekt_id` | Eindeutige COPRA-ID – bleibt bei Umbenennung gleich |
| `titel` | Projektname |
| `manager` | COPRA-Manager |
| `produktion` | Produktion (falls in COPRA gepflegt) |
| `erstellt_am` | Anlagedatum in COPRA → Grundlage für „online seit“ |
| `groesse_bytes` | Exakte Größe inkl. aller Unterordner |
| `groesse_gb` | Dieselbe Größe in GB (1 GB = 10⁹ Bytes, wie in COPRA angezeigt) |

**`copra_laeufe.csv`** – eine Zeile pro Lauf (`OK`, `FEHLER`, `WARNUNG`,
`UEBERSPRUNGEN`). Damit lässt sich unterscheiden, ob ein Projekt gelöscht wurde
oder das Skript an dem Tag einfach nicht gelaufen ist.

**`copra_speicher.log`** – Protokoll, rotiert bei 1 MB (5 Generationen).

## Verhalten bei Problemen

- Netz-/Serverfehler: erneuter Versuch (Anzahl und Wartezeit in `config.ini`).
- Falsche Zugangsdaten oder 2FA: sofortiger Abbruch ohne Wiederholung.
- Leere Projektliste: wird nicht geschrieben (Warnung), damit nicht fälschlich
  alle Projekte als gelöscht gelten.
- CSV in Excel geöffnet: das Skript wartet kurz und versucht es erneut,
  danach Fehler im Log.
- Es werden nur vollständige Abrufe geschrieben – nie halbe Tage.
