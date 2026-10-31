# RISEN TWEAKS V3.4.0

Windows-Gaming-Optimizer (Python + CustomTkinter). Design nach `reference_ui.png`.

## Portable EXE ohne Python bauen (GitHub Actions)
1. Alle Dateien dieses ZIPs so ins Repository legen, dass **`main.py` und `requirements.txt` im selben Ordner** liegen
   (Root empfohlen). Der Workflow `.github/workflows/build-windows.yml` muss exakt diesen Pfad haben.
2. Commit auf `main` oder unter **Actions → Build RISEN TWEAKS for Windows → Run workflow**.
3. Der Lauf prüft Syntax, führt Unit-Tests aus, baut die EXE, startet sie headless (`--selftest`) und lädt sie hoch.
4. Artifact **RISEN_TWEAKS-Windows-EXE** herunterladen, entpacken, doppelklicken. Kein Python nötig, kein Konsolenfenster.

Schlägt `requirements.txt not found` weiter fehl, zeigt der Schritt *Locate project* im Log die tatsächliche Dateiliste.
Dann liegen die Dateien nicht im selben Ordner oder heißen anders (z. B. mit Präfix `RISEN_TWEAKS_V2_`).

SmartScreen kann bei einer selbst gebauten, unsignierten EXE warnen. Das ist normal.

## Tweaks-Tab (Startseite)
**AUTO OPTIMIZE** macht alles selbst (System erkennen, Tweaks wählen, anwenden, aufräumen). Profile: SAFE, COMPETITIVE, MAX FPS.
Jeder Schalter wirkt sofort, ist gesichert, verifiziert und per Schalter umkehrbar.

## Seiten
Home · **Tweaks** · Windows Tweaks · **Control Center** · Performance · Cleanup · GPU · **Fortnite** · **Network Lab** · Processes · Hardware · Tools

## Control Center
Schalter bei den Tweaks setzen, dann **APPLY** oder **APPLY & RESTART**. **BEST TWEAKS FOR MY PC** wählt automatisch
sichere Tweaks für dein System (Desktop/Notebook, Fortnite-Config vorhanden, schon Aktives wird übersprungen), zeigt die
Auswahl mit Begründung und führt sie nach Bestätigung aus. Kosmetische Tweaks (Transparenz, Animationen) werden nie automatisch gewählt.

## Tweak Library (Details)
14 Kategorien. Jeder Registry-/Energieplan-Tweak hat Beschreibung, Auswirkung, Status, Anwenden, Undo,
Backup (`%LOCALAPPDATA%\RisenTweaks\tweak_state.json`) und Rücklese-Verifikation (bei Abweichung automatisch Rollback).
Die meisten Registry-Tweaks betreffen `HKEY_CURRENT_USER`. Tweaks mit 🔒 ADMIN (HKLM: Scheduler, Netzwerk-Drosselung, Telemetrie, MSI-Modus u. a.) brauchen Adminrechte; sie sind jederzeit per Undo umkehrbar.
Kategorien ohne sicheren Tweak (NVIDIA, AMD, CPU, RAM, Netzwerk, Advanced) enthalten Prüf-/Hinweiskarten statt Fake-Tweaks.

## Fortnite-Messung
FPS, Frametime, 1 % Low: nur aus echten Daten von **PresentMon** (Intel, MIT). `PresentMon.exe` neben `RISEN_TWEAKS.exe`
legen, Fortnite starten, ggf. RISEN als Administrator starten, dann „30 s Messung starten“.
Eingabelatenz und Fortnite-Serverlatenz sind per Software nicht ehrlich messbar und werden nicht geschätzt.

## Was die Tweaks leisten (ehrlich)
Es gibt keinen Tweak, der "0 Delay" oder große FPS-Sprünge garantiert. Realistisch sind kleinere Verbesserungen bei
Frametime-Spitzen und weniger Störfaktoren (Overlays, Popups, Energiespar-Aufwachen). Wirkung messen: PresentMon-Messung
vor und nach dem Apply auf der Fortnite-Seite. Verkaufe keine FPS-Zahlen, die du nicht selbst gemessen hast.

## Bewusst nicht enthalten
Defender/Firewall/Windows Update deaktivieren (Tamper Protection blockiert es meist, echtes Sicherheitsrisiko, kaum FPS-Gewinn), Anti-Cheat-Eingriffe,
HPET/bcdedit, TCP/Nagle-Registry (Fortnite nutzt UDP), Treiberinstallation/DDU, BIOS-Einstellungen (per Software nicht ehrlich möglich).

## Tests
`python -m unittest discover -s tests` (Ping-Parser DE/EN, Jitter, 1 %-Low-Rechnung, PresentMon-CSV, Apply/Undo/Backup/Rollback,
Powerplan-Ablauf, Autostart, Sicherheits-Blacklist). Zusätzlich `tests/smoke_ui.py` (UI-Aufbau mit Mocks, Linux-tauglich).
Nicht automatisch getestet: echte Registry-/powercfg-Aufrufe, echtes PresentMon, das echte Fenster.
