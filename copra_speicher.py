#!/usr/bin/env python3
"""
Copracrawler – erfasst täglich den Speicherverbrauch aller sichtbaren COPRA-Projekte.

Ablauf:
  1. Anmelden an der COPRA-API (E-Mail + Passwort aus config.ini)
  2. Alle sichtbaren Projekte mit Gesamtgröße abfragen (inkl. Unterordner)
  3. Abmelden
  4. Pro Projekt eine Zeile an copra_speicher.csv anhängen,
     pro Lauf eine Zeile an copra_laeufe.csv

Hinweis: COPRA hat keine offizielle API. Das Skript nutzt dieselbe Schnittstelle
wie die Web-App (https://api.copra.app/v2). Ändert COPRA diese, bricht das Skript
mit einer klaren Fehlermeldung ab, statt falsche Zahlen zu schreiben.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import logging
import logging.handlers
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("Das Paket 'requests' fehlt. Bitte installieren: pip install -r requirements.txt")
    sys.exit(2)

VERSION = "1.0.0"
SKRIPT_ORDNER = Path(__file__).resolve().parent

STANDARD_API_URL = "https://api.copra.app/v2"
PLATZHALTER_EMAIL = "test@abc.com"

SPEICHER_CSV = "copra_speicher.csv"
LAEUFE_CSV = "copra_laeufe.csv"
LOG_DATEI = "copra_speicher.log"
TRENNER = ";"

SPEICHER_SPALTEN = [
    "datum",
    "projekt_id",
    "titel",
    "manager",
    "produktion",
    "erstellt_am",
    "groesse_bytes",
    "groesse_gb",
]
LAEUFE_SPALTEN = [
    "datum",
    "uhrzeit",
    "status",
    "anzahl_projekte",
    "summe_bytes",
    "meldung",
]

LIST_PROJECTS_QUERY = """
query ListProjects($after: ID) {
  projects(after: $after) {
    pageInfo { endCursor hasNextPage }
    edges {
      node {
        id
        name
        manager
        production
        createdAt
        folder { totalSize }
      }
    }
  }
}
"""

log = logging.getLogger("copracrawler")


# --------------------------------------------------------------------------- #
# Fehlerklassen
# --------------------------------------------------------------------------- #
class KonfigFehler(Exception):
    """Fehler in der Konfiguration – kein erneuter Versuch sinnvoll."""


class AnmeldeFehler(Exception):
    """Login abgelehnt (falsche Zugangsdaten, 2FA …) – kein erneuter Versuch sinnvoll."""


class AbrufFehler(Exception):
    """Vorübergehender oder unerwarteter Fehler beim Abruf – erneuter Versuch möglich."""


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
@dataclass
class Konfig:
    email: str
    passwort: str
    api_url: str
    csv_ordner: Path
    versuche: int
    wartezeit: int
    timeout: int


def lade_konfig(pfad: Path) -> Konfig:
    if not pfad.exists():
        beispiel = SKRIPT_ORDNER / "config.example.ini"
        if pfad == SKRIPT_ORDNER / "config.ini" and beispiel.exists():
            shutil.copy(beispiel, pfad)
            raise KonfigFehler(
                f"Keine config.ini gefunden – eine Vorlage wurde angelegt: {pfad}\n"
                "Bitte dort E-Mail und Passwort eintragen und das Skript erneut starten."
            )
        raise KonfigFehler(f"Konfigurationsdatei nicht gefunden: {pfad}")

    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(pfad, encoding="utf-8-sig")
    except configparser.Error as e:
        raise KonfigFehler(f"config.ini ist fehlerhaft: {e}") from e

    # Umgebungsvariablen haben Vorrang vor der Datei (optional)
    email = os.environ.get("COPRA_EMAIL") or cp.get("copra", "email", fallback="").strip()
    passwort = os.environ.get("COPRA_PASSWORT") or cp.get("copra", "passwort", fallback="")
    if not email or not passwort:
        raise KonfigFehler("In config.ini fehlen 'email' oder 'passwort' im Abschnitt [copra].")

    api_url = cp.get("copra", "api_url", fallback=STANDARD_API_URL).strip().rstrip("/")

    ordner_roh = cp.get("ablage", "csv_ordner", fallback="").strip()
    csv_ordner = Path(ordner_roh).expanduser() if ordner_roh else SKRIPT_ORDNER
    if not csv_ordner.is_absolute():
        csv_ordner = (pfad.parent / csv_ordner).resolve()

    try:
        versuche = max(1, cp.getint("abruf", "versuche", fallback=3))
        wartezeit = max(0, cp.getint("abruf", "wartezeit_sekunden", fallback=60))
        timeout = max(5, cp.getint("abruf", "timeout_sekunden", fallback=90))
    except ValueError as e:
        raise KonfigFehler(f"Ungültiger Zahlenwert im Abschnitt [abruf]: {e}") from e

    return Konfig(email, passwort, api_url, csv_ordner, versuche, wartezeit, timeout)


# --------------------------------------------------------------------------- #
# COPRA-API
# --------------------------------------------------------------------------- #
class CopraClient:
    def __init__(self, api_url: str, timeout: int):
        self.api_url = api_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": f"Copracrawler/{VERSION}"}
        )

    def _url(self, pfad: str) -> str:
        return f"{self.api_url}/{pfad}"

    def login(self, email: str, passwort: str) -> None:
        try:
            r = self.session.post(
                self._url("login"),
                json={"email": email, "password": passwort},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise AbrufFehler(f"COPRA nicht erreichbar: {e}") from e

        if r.status_code in (400, 401, 403, 404, 422):
            raise AnmeldeFehler(
                f"Anmeldung abgelehnt (HTTP {r.status_code}). E-Mail/Passwort in config.ini prüfen."
            )
        if r.status_code not in (200, 201):
            raise AbrufFehler(f"Unerwartete Antwort beim Login: HTTP {r.status_code}")

        try:
            daten = r.json()
        except ValueError as e:
            raise AbrufFehler("Login-Antwort ist kein JSON – hat COPRA die Schnittstelle geändert?") from e

        token = daten.get("token")
        if not token:
            raise AbrufFehler("Login-Antwort enthält kein Token – hat COPRA die Schnittstelle geändert?")
        if daten.get("mustVerify"):
            raise AnmeldeFehler(
                "Der Account verlangt eine Zwei-Faktor-Bestätigung. "
                "Für den automatischen Abruf einen Account ohne 2FA verwenden."
            )
        self.session.headers["Authorization"] = f"Bearer {token}"
        log.info("Anmeldung erfolgreich.")

    def logout(self) -> None:
        if "Authorization" not in self.session.headers:
            return
        try:
            self.session.post(self._url("logout"), timeout=self.timeout)
            log.info("Abgemeldet.")
        except requests.RequestException:
            log.debug("Abmelden fehlgeschlagen (ignoriert).")
        finally:
            self.session.headers.pop("Authorization", None)
            self.session.close()

    def _query(self, query: str, variablen: dict) -> dict:
        try:
            r = self.session.post(
                self._url("query"),
                json={"query": query, "variables": variablen},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise AbrufFehler(f"Abfrage fehlgeschlagen: {e}") from e

        if r.status_code == 401:
            raise AbrufFehler("Abfrage abgelehnt (HTTP 401) – Token ungültig oder abgelaufen.")
        if r.status_code != 200:
            raise AbrufFehler(f"Abfrage fehlgeschlagen: HTTP {r.status_code}")
        try:
            antwort = r.json()
        except ValueError as e:
            raise AbrufFehler("Antwort ist kein JSON.") from e

        if antwort.get("errors"):
            meldungen = "; ".join(str(f.get("message", f)) for f in antwort["errors"])
            raise AbrufFehler(f"COPRA meldet Fehler: {meldungen}")
        if "data" not in antwort or antwort["data"] is None:
            raise AbrufFehler("Antwort enthält keine Daten.")
        return antwort["data"]

    def projekte(self) -> list[dict]:
        """Alle sichtbaren Projekte, über alle Seiten hinweg."""
        alle: list[dict] = []
        cursor = None
        gesehen: set[str] = set()
        for _ in range(10_000):  # Schutz gegen Endlosschleifen
            daten = self._query(LIST_PROJECTS_QUERY, {"after": cursor})
            try:
                block = daten["projects"]
                seite = block["pageInfo"]
                kanten = block.get("edges") or []
            except (KeyError, TypeError) as e:
                raise AbrufFehler(
                    "Unerwarteter Aufbau der Projektliste – hat COPRA die Schnittstelle geändert?"
                ) from e

            alle.extend(k["node"] for k in kanten if k and k.get("node"))

            cursor = seite.get("endCursor")
            if not seite.get("hasNextPage") or not cursor:
                return alle
            if cursor in gesehen:
                raise AbrufFehler("Blättern hängt (gleicher Cursor doppelt).")
            gesehen.add(cursor)
        raise AbrufFehler("Zu viele Seiten – Abbruch.")


# --------------------------------------------------------------------------- #
# Aufbereitung
# --------------------------------------------------------------------------- #
_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def iso_zu_lokalem_datum(wert: str | None) -> str:
    """'2026-06-28T07:24:11.159023Z' -> '2026-06-28' (in lokaler Zeitzone)."""
    if not wert:
        return ""
    m = _ISO_RE.match(wert.strip())
    if not m:
        return wert[:10]
    basis, bruch, zone = m.groups()
    mikro = (bruch or "0")[:6].ljust(6, "0")
    if zone in (None, "Z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    dt = datetime.fromisoformat(f"{basis}.{mikro}{zone}")
    return dt.astimezone().date().isoformat()


def groesse_gb(bytes_wert: int | None) -> str:
    """Dezimal-GB wie in der COPRA-Oberfläche, mit deutschem Komma."""
    if bytes_wert is None:
        return ""
    return f"{bytes_wert / 1_000_000_000:.3f}".replace(".", ",")


def baue_zeilen(projekte: list[dict], datum: str) -> tuple[list[dict], int]:
    zeilen = []
    summe = 0
    for p in projekte:
        roh = (p.get("folder") or {}).get("totalSize")
        try:
            groesse = int(roh) if roh is not None else None
        except (TypeError, ValueError):
            groesse = None
        if groesse is None:
            log.warning("Projekt '%s' (%s) liefert keine Größe.", p.get("name"), p.get("id"))
        else:
            summe += groesse
        zeilen.append(
            {
                "datum": datum,
                "projekt_id": p.get("id", ""),
                "titel": (p.get("name") or "").strip(),
                "manager": (p.get("manager") or "").strip(),
                "produktion": (p.get("production") or "").strip(),
                "erstellt_am": iso_zu_lokalem_datum(p.get("createdAt")),
                "groesse_bytes": "" if groesse is None else str(groesse),
                "groesse_gb": groesse_gb(groesse),
            }
        )
    zeilen.sort(key=lambda z: z["titel"].lower())
    return zeilen, summe


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def lese_csv(pfad: Path) -> list[dict]:
    if not pfad.exists():
        return []
    with pfad.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=TRENNER))


def datum_vorhanden(pfad: Path, datum: str) -> bool:
    return any(z.get("datum") == datum for z in lese_csv(pfad))


def _mit_wiederholung(aktion, pfad: Path):
    """Excel sperrt geöffnete CSV-Dateien – ein paar Sekunden warten und erneut versuchen."""
    for versuch in range(5):
        try:
            return aktion()
        except PermissionError:
            if versuch == 4:
                raise PermissionError(
                    f"Datei ist gesperrt (in Excel geöffnet?): {pfad}"
                ) from None
            time.sleep(3)


def haenge_an(pfad: Path, spalten: list[str], zeilen: list[dict]) -> None:
    def schreiben():
        neu = not pfad.exists() or pfad.stat().st_size == 0
        # Neue Datei mit BOM, damit Excel Umlaute korrekt anzeigt
        with pfad.open("a", encoding="utf-8-sig" if neu else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=spalten, delimiter=TRENNER, extrasaction="ignore")
            if neu:
                w.writeheader()
            w.writerows(zeilen)

    _mit_wiederholung(schreiben, pfad)


def ersetze_tag(pfad: Path, spalten: list[str], datum: str, zeilen: list[dict]) -> None:
    """Alle Zeilen des Tages entfernen und durch die neuen ersetzen (atomar)."""
    alt = [z for z in lese_csv(pfad) if z.get("datum") != datum]
    tmp = pfad.with_suffix(pfad.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=spalten, delimiter=TRENNER, extrasaction="ignore")
        w.writeheader()
        w.writerows(alt)
        w.writerows(zeilen)
    _mit_wiederholung(lambda: os.replace(tmp, pfad), pfad)


def protokolliere_lauf(ordner: Path, status: str, anzahl: int | str, summe: int | str, meldung: str):
    jetzt = datetime.now()
    try:
        haenge_an(
            ordner / LAEUFE_CSV,
            LAEUFE_SPALTEN,
            [
                {
                    "datum": jetzt.date().isoformat(),
                    "uhrzeit": jetzt.strftime("%H:%M:%S"),
                    "status": status,
                    "anzahl_projekte": anzahl,
                    "summe_bytes": summe,
                    "meldung": meldung.replace("\n", " "),
                }
            ],
        )
    except OSError as e:
        log.error("Lauf konnte nicht protokolliert werden: %s", e)


# --------------------------------------------------------------------------- #
# Ablauf
# --------------------------------------------------------------------------- #
def hole_projekte(k: Konfig) -> list[dict]:
    letzter_fehler: Exception | None = None
    for versuch in range(1, k.versuche + 1):
        client = CopraClient(k.api_url, k.timeout)
        try:
            client.login(k.email, k.passwort)
            projekte = client.projekte()
            log.info("%d Projekte abgerufen.", len(projekte))
            return projekte
        except AnmeldeFehler:
            raise
        except AbrufFehler as e:
            letzter_fehler = e
            log.warning("Versuch %d/%d fehlgeschlagen: %s", versuch, k.versuche, e)
            if versuch < k.versuche:
                time.sleep(k.wartezeit)
        finally:
            client.logout()
    raise AbrufFehler(f"Abruf nach {k.versuche} Versuchen fehlgeschlagen: {letzter_fehler}")


def richte_logging_ein(ordner: Path | None, ausfuehrlich: bool) -> None:
    log.setLevel(logging.DEBUG if ausfuehrlich else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
    konsole = logging.StreamHandler(sys.stdout)
    konsole.setFormatter(fmt)
    log.addHandler(konsole)
    if ordner is not None:
        datei = logging.handlers.RotatingFileHandler(
            ordner / LOG_DATEI, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        datei.setFormatter(fmt)
        log.addHandler(datei)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Erfasst den Speicherverbrauch aller sichtbaren COPRA-Projekte."
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=SKRIPT_ORDNER / "config.ini",
        help="Pfad zur config.ini (Standard: neben dem Skript)",
    )
    ap.add_argument(
        "--ersetzen",
        action="store_true",
        help="Bereits vorhandene Werte von heute überschreiben statt den Lauf zu überspringen",
    )
    ap.add_argument(
        "--probelauf",
        action="store_true",
        help="Nur abrufen und anzeigen, nichts in die CSV schreiben",
    )
    ap.add_argument("-v", "--ausfuehrlich", action="store_true", help="Mehr Details im Log")
    ap.add_argument("--version", action="version", version=f"Copracrawler {VERSION}")
    args = ap.parse_args(argv)

    # Erst nur Konsole – Log-Datei kommt, sobald der Ablageordner bekannt ist
    try:
        k = lade_konfig(args.config.resolve())
    except KonfigFehler as e:
        richte_logging_ein(None, args.ausfuehrlich)
        log.error("%s", e)
        return 2

    try:
        k.csv_ordner.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        richte_logging_ein(None, args.ausfuehrlich)
        log.error("Ablageordner nicht verfügbar: %s (%s)", k.csv_ordner, e)
        return 2
    richte_logging_ein(None if args.probelauf else k.csv_ordner, args.ausfuehrlich)
    log.info("Copracrawler %s – Ablage: %s", VERSION, k.csv_ordner)
    if k.email.lower() == PLATZHALTER_EMAIL:
        log.warning("In config.ini steht noch die Platzhalter-Adresse %s.", k.email)

    heute = date.today().isoformat()
    speicher = k.csv_ordner / SPEICHER_CSV

    if not args.probelauf and not args.ersetzen and datum_vorhanden(speicher, heute):
        log.info("Für %s sind bereits Werte erfasst – Lauf übersprungen (--ersetzen zum Überschreiben).", heute)
        protokolliere_lauf(k.csv_ordner, "UEBERSPRUNGEN", "", "", "Tag bereits erfasst")
        return 0

    try:
        projekte = hole_projekte(k)
    except (AnmeldeFehler, AbrufFehler) as e:
        log.error("%s", e)
        if not args.probelauf:
            protokolliere_lauf(k.csv_ordner, "FEHLER", "", "", str(e))
        return 1

    zeilen, summe = baue_zeilen(projekte, heute)

    if args.probelauf:
        for z in zeilen:
            print(f"{z['titel']:<40} {z['groesse_gb']:>12} GB   erstellt {z['erstellt_am']}")
        print(f"{'Summe':<40} {groesse_gb(summe):>12} GB   ({len(zeilen)} Projekte)")
        return 0

    if not zeilen:
        # Leere Liste ist verdächtig (Rechte entzogen?) – nicht als "alle Projekte gelöscht" werten
        log.warning("COPRA hat keine Projekte geliefert – nichts geschrieben.")
        protokolliere_lauf(k.csv_ordner, "WARNUNG", 0, 0, "Keine Projekte sichtbar")
        return 1

    try:
        if args.ersetzen and datum_vorhanden(speicher, heute):
            ersetze_tag(speicher, SPEICHER_SPALTEN, heute, zeilen)
            log.info("Werte für %s ersetzt.", heute)
        else:
            haenge_an(speicher, SPEICHER_SPALTEN, zeilen)
    except OSError as e:
        log.error("CSV konnte nicht geschrieben werden: %s", e)
        protokolliere_lauf(k.csv_ordner, "FEHLER", len(zeilen), summe, f"CSV nicht schreibbar: {e}")
        return 1

    log.info("%d Projekte mit zusammen %s GB in %s eingetragen.", len(zeilen), groesse_gb(summe), speicher.name)
    protokolliere_lauf(k.csv_ordner, "OK", len(zeilen), summe, "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
