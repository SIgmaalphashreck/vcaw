"""RISEN TWEAKS - Network Lab.

Alle Funktionen messen echte Werte (ping.exe, TCP-Handshake, HTTPS-Transfer,
DNS-Aufloesung). Es werden keine Werte simuliert. Kann etwas nicht gemessen
werden, wird None / eine Fehlermeldung zurueckgegeben.
"""
from __future__ import annotations

import json
import os
import re
import socket
import statistics
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

import psutil

IS_WINDOWS = os.name == "nt"

# Oeffentliche Messendpunkte (Cloudflare Speed Test). Das ist ein allgemeiner
# Internet-Durchsatztest, KEINE Messung der Fortnite-Serverroute.
SPEED_DOWN_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
SPEED_UP_URL = "https://speed.cloudflare.com/__up"

# Epic-Webendpunkte fuer TCP-Handshake-Vergleich (keine Spielserver!).
EPIC_TCP_TARGETS = [("www.epicgames.com", 443), ("www.fortnite.com", 443)]


def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, shell=False, creationflags=flags,
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


# --------------------------------------------------------------------------
# Ping / Jitter / Paketverlust
# --------------------------------------------------------------------------
_PING_TIME = re.compile(r"([=<])\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)


def parse_ping_reply(text: str) -> Optional[float]:
    """Liest die Antwortzeit (ms) aus der Ausgabe EINES Pings.

    Sprachunabhaengig: Eine echte Antwortzeile enthaelt immer 'TTL='
    (auch auf deutschem Windows). '<1ms' wird als 0.5 ms gewertet.
    """
    for line in text.splitlines():
        if "ttl" not in line.lower():
            continue
        m = _PING_TIME.search(line)
        if not m:
            continue
        value = float(m.group(2).replace(",", "."))
        if m.group(1) == "<":
            return 0.5 if value <= 1 else value - 0.5
        return value
    return None


def calc_jitter(samples: list[float]) -> Optional[float]:
    """Jitter = Mittelwert der absoluten Differenzen aufeinanderfolgender
    Antwortzeiten (wie RFC 3550 ohne Glaettung). Braucht >= 2 Antworten."""
    if len(samples) < 2:
        return None
    diffs = [abs(b - a) for a, b in zip(samples, samples[1:])]
    return sum(diffs) / len(diffs)


@dataclass
class PingResult:
    host: str
    sent: int = 0
    samples: list[float] = field(default_factory=list)

    @property
    def received(self) -> int:
        return len(self.samples)

    @property
    def loss_pct(self) -> Optional[float]:
        if self.sent == 0:
            return None
        return 100.0 * (self.sent - self.received) / self.sent

    @property
    def avg(self) -> Optional[float]:
        return statistics.fmean(self.samples) if self.samples else None

    @property
    def min(self) -> Optional[float]:
        return min(self.samples) if self.samples else None

    @property
    def max(self) -> Optional[float]:
        return max(self.samples) if self.samples else None

    @property
    def jitter(self) -> Optional[float]:
        return calc_jitter(self.samples)

    def summary(self) -> str:
        if self.sent == 0:
            return f"{self.host}: kein Test ausgefuehrt"
        if not self.samples:
            return f"{self.host}: keine Antwort ({self.sent} gesendet, 100% Verlust) - ICMP evtl. blockiert"
        return (
            f"{self.host}: Ø {self.avg:.1f} ms · min {self.min:.1f} · max {self.max:.1f} · "
            f"Jitter {self.jitter if self.jitter is not None else float('nan'):.1f} ms · "
            f"Verlust {self.loss_pct:.0f}% ({self.received}/{self.sent})"
        )


def ping_once(host: str, timeout_ms: int = 1000) -> Optional[float]:
    if IS_WINDOWS:
        args = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        args = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
    _, out = _run(args, timeout_ms / 1000 + 3)
    if IS_WINDOWS:
        return parse_ping_reply(out)
    m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out)
    return float(m.group(1)) if m else None


def ping_series(
    host: str,
    count: int = 20,
    interval: float = 0.25,
    timeout_ms: int = 1000,
    progress: Optional[Callable[[PingResult], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> PingResult:
    """Fuehrt `count` Einzel-Pings aus. Jeder gesendete Ping zaehlt fuer den
    Paketverlust; `progress` wird nach jedem Ping aufgerufen."""
    result = PingResult(host=host)
    for _ in range(count):
        if cancel is not None and cancel.is_set():
            break
        value = ping_once(host, timeout_ms)
        result.sent += 1
        if value is not None:
            result.samples.append(value)
        if progress:
            progress(result)
        time.sleep(interval)
    return result


# --------------------------------------------------------------------------
# TCP-Handshake & DNS
# --------------------------------------------------------------------------
def tcp_connect_ms(host: str, port: int = 443, count: int = 5, timeout: float = 3.0) -> PingResult:
    """Misst die Dauer des TCP-Handshakes (ohne DNS). Ersatz, wenn ICMP
    blockiert ist. Keine Spielserver-Latenz."""
    result = PingResult(host=f"{host}:{port} (TCP)")
    try:
        ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
    except OSError:
        result.sent = count
        return result
    for _ in range(count):
        t0 = time.perf_counter()
        result.sent += 1
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                result.samples.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
        time.sleep(0.15)
    return result


def dns_lookup_ms(host: str) -> tuple[Optional[float], str]:
    t0 = time.perf_counter()
    try:
        info = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ms = (time.perf_counter() - t0) * 1000
        return ms, info[0][4][0]
    except OSError as exc:
        return None, str(exc)


def flush_dns() -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Nur unter Windows verfuegbar."
    code, out = _run(["ipconfig", "/flushdns"], 10)
    return code == 0, out or ("DNS-Cache geleert." if code == 0 else "Fehler")


# --------------------------------------------------------------------------
# Download / Upload
# --------------------------------------------------------------------------
@dataclass
class ThroughputResult:
    direction: str
    mbit_s: Optional[float] = None
    bytes_total: int = 0
    seconds: float = 0.0
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"{self.direction}: Fehler - {self.error}"
        if self.mbit_s is None:
            return f"{self.direction}: keine Daten"
        return (
            f"{self.direction}: {self.mbit_s:.1f} Mbit/s "
            f"({self.bytes_total / 1e6:.1f} MB in {self.seconds:.1f} s, Cloudflare, allgemeiner Test)"
        )


def _mbit(total_bytes: int, seconds: float) -> Optional[float]:
    if seconds <= 0 or total_bytes <= 0:
        return None
    return total_bytes * 8 / seconds / 1e6


def download_test(duration: float = 8.0, streams: int = 4,
                  progress: Optional[Callable[[float], None]] = None) -> ThroughputResult:
    res = ThroughputResult("Download")
    lock = threading.Lock()
    counter = {"bytes": 0}
    errors: list[str] = []
    stop_at = time.perf_counter() + duration
    t_start = time.perf_counter()

    def worker():
        while time.perf_counter() < stop_at:
            try:
                req = urllib.request.Request(
                    SPEED_DOWN_URL.format(bytes=25_000_000),
                    headers={"User-Agent": "RISEN-TWEAKS/2"},
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    while time.perf_counter() < stop_at:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        with lock:
                            counter["bytes"] += len(chunk)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(streams)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(0.4)
        if progress:
            el = time.perf_counter() - t_start
            progress(_mbit(counter["bytes"], el) or 0.0)
    res.seconds = time.perf_counter() - t_start
    res.bytes_total = counter["bytes"]
    res.mbit_s = _mbit(res.bytes_total, res.seconds)
    if res.bytes_total == 0 and errors:
        res.error = errors[0]
    return res


def upload_test(duration: float = 8.0, streams: int = 3, chunk_mb: int = 4) -> ThroughputResult:
    res = ThroughputResult("Upload")
    lock = threading.Lock()
    counter = {"bytes": 0}
    errors: list[str] = []
    payload = os.urandom(chunk_mb * 1024 * 1024)
    stop_at = time.perf_counter() + duration
    t_start = time.perf_counter()

    def worker():
        while time.perf_counter() < stop_at:
            try:
                req = urllib.request.Request(
                    SPEED_UP_URL, data=payload, method="POST",
                    headers={"User-Agent": "RISEN-TWEAKS/2", "Content-Type": "application/octet-stream"},
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
                with lock:
                    counter["bytes"] += len(payload)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(streams)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration + 20)
    res.seconds = time.perf_counter() - t_start
    res.bytes_total = counter["bytes"]
    res.mbit_s = _mbit(res.bytes_total, res.seconds)
    if res.bytes_total == 0 and errors:
        res.error = errors[0]
    return res


# --------------------------------------------------------------------------
# Adapter / Gateway / DNS-Server
# --------------------------------------------------------------------------
def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_netipconfig(text: str) -> list[dict]:
    """Parst die JSON-Ausgabe von Get-NetIPConfiguration | ConvertTo-Json."""
    text = text.strip().lstrip("\ufeff")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for item in _as_list(data):
        if not isinstance(item, dict):
            continue
        gw4 = [g.get("NextHop") for g in _as_list(item.get("IPv4DefaultGateway")) if isinstance(g, dict)]
        dns = []
        for d in _as_list(item.get("DNSServer")):
            if isinstance(d, dict):
                dns.extend(a for a in _as_list(d.get("ServerAddresses")) if a and ":" not in str(a))
        ips = [a.get("IPv4Address") if isinstance(a, dict) else a for a in _as_list(item.get("IPv4Address"))]
        adapter = item.get("NetAdapter") if isinstance(item.get("NetAdapter"), dict) else {}
        out.append({
            "name": item.get("InterfaceAlias") or "?",
            "description": item.get("InterfaceDescription") or "",
            "ipv4": [i for i in ips if i],
            "gateway": [g for g in gw4 if g],
            "dns": dns,
            "status": adapter.get("Status") or "",
            "link_speed": adapter.get("LinkSpeed") or "",
        })
    return out


def get_network_config() -> list[dict]:
    if not IS_WINDOWS:
        return []
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-NetIPConfiguration -Detailed | Where-Object { $_.NetAdapter.Status -eq 'Up' } | "
        "ForEach-Object { [pscustomobject]@{InterfaceAlias=$_.InterfaceAlias;"
        "InterfaceDescription=$_.InterfaceDescription;IPv4Address=@($_.IPv4Address.IPAddress);"
        "IPv4DefaultGateway=@($_.IPv4DefaultGateway);DNSServer=@($_.DNSServer);"
        "NetAdapter=[pscustomobject]@{Status=[string]$_.NetAdapter.Status;LinkSpeed=$_.NetAdapter.LinkSpeed}} } | "
        "ConvertTo-Json -Depth 5 -Compress"
    )
    code, out = _run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        25,
    )
    return parse_netipconfig(out) if code == 0 else []


def psutil_adapters() -> list[dict]:
    """Fallback ohne PowerShell: Adapterstatus ueber psutil."""
    out = []
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    for name, st in stats.items():
        ipv4 = [a.address for a in addrs.get(name, []) if a.family == socket.AF_INET]
        out.append({
            "name": name, "is_up": st.isup,
            "speed_mbit": st.speed or None, "ipv4": ipv4,
            "kind": guess_kind(name),
        })
    return out


def guess_kind(name: str) -> str:
    n = name.lower()
    if any(x in n for x in ("wi-fi", "wifi", "wlan", "wireless")):
        return "WLAN"
    if any(x in n for x in ("ethernet", "eth", "lan", "enp", "eno")):
        return "LAN"
    if any(x in n for x in ("loopback", "lo", "vethernet", "vmware", "virtual", "bluetooth")):
        return "virtuell"
    return "unbekannt"
