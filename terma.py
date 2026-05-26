"""Client for the Terma Wi-Fi thermostat protocols.

Covers two channels:

* **LAN control** — TCP port 5005, JSON. See :class:`TermaClient` and
  :func:`discover` / :func:`discover_pairing`.
* **User cloud API** — ``https://api.termasmart.com``, HTTPS+JSON with a
  ``BaseRequest`` envelope and ``Authorization: Bearer …`` auth. See
  :class:`TermaCloudClient`.

LAN action IDs (from LocalRequestType in the Android app):
    0  MeasurementsTemperatureSensor   1  MeasurementsWindowSensor
    2  MeasurementsHeater              3  MeasurementsHeatThermostaticHeader
    4  ConfigureTemperatureSensor      5  ConfigureHeater
    6  ConfigureHeatThermostaticHeader 7  ConfigureNetwork
    8  UpdateSchedule                  14 ConfigureApi
    15 fetchTelemetry (with mac)       26 ExtendedControlApi

Telemetry response fields (from DeviceTelemetry):
    serial, fwVersion, timestamp, batteryLevel, errorsFlag,
    temperature, humidity, heaterTemperature, state, isEnabled,
    isDryerOn, isCalibrateOn, isParentalControlOn, isHeating,
    heatingCoefficient, manualMode, powerUsage, schedule.

Heating power (powerCapabilities) is settable but NOT in telemetry response.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterable

log = logging.getLogger("terma")

DEFAULT_PORT = 5005
DEFAULT_TIMEOUT = 5.0
DEFAULT_BROADCAST_PORT = 2349
DEFAULT_DISCOVERY_TIMEOUT = 10.0
MDNS_SERVICE = "_terma._tcp.local"
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353

ACTION_TELEMETRY_HEATER = 2
ACTION_CONFIGURE_HEATER = 5
ACTION_CONFIGURE_NETWORK = 7
ACTION_UPDATE_SCHEDULE = 8
ACTION_CONFIGURE_API = 14
ACTION_TELEMETRY_FETCH = 15
ACTION_EXTENDED_CONTROL = 26

# Cloud channel — APP_API_SERVER from BuildConfig
CLOUD_API_BASE = "https://api.termasmart.com"
CLOUD_DEVICE_API_HOST = "api-devices.termasmart.com"
CLOUD_HTTP_TIMEOUT_SECONDS = 10.0

# Back-compat aliases
ACTION_GET_TELEMETRY = ACTION_TELEMETRY_HEATER
ACTION_SET = ACTION_CONFIGURE_HEATER

# DeviceTemperatureChangeMode values for ChangeTemperatureRequest.manualMode
MODE_MANUAL = 0              # set temperature with no time bound
MODE_MANUAL_WITH_TIMER = 1   # set temperature for a duration (manualTimer seconds)
MODE_MANUAL_SCHEDULE_AWARE = 2  # override schedule but resume at next schedule entry

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "bright_yellow": "\033[93m",
    "bright_cyan": "\033[96m",
}


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERMA_FORCE_COLOR"):
        return True
    return sys.stderr.isatty()


_TOKEN_RE = re.compile(
    r'"(?:\\.|[^"\\])*"(?:\s*:)?'   # strings (possibly followed by colon -> key)
    r'|-?\d+\.?\d*(?:[eE][+-]?\d+)?' # numbers
    r'|\b(?:true|false|null)\b'      # literals
)


def _colorize_json(text: str) -> str:
    color = _ANSI

    def repl(m: re.Match) -> str:
        tok = m.group(0)
        if tok.endswith(":") or tok.rstrip().endswith(":"):
            # key
            return f"{color['cyan']}{tok}{color['reset']}"
        if tok.startswith('"'):
            return f"{color['green']}{tok}{color['reset']}"
        if tok in ("true", "false", "null"):
            return f"{color['magenta']}{tok}{color['reset']}"
        return f"{color['yellow']}{tok}{color['reset']}"

    return _TOKEN_RE.sub(repl, text)


def _hexdump(data: bytes, color: bool, width: int = 16) -> str:
    """xxd-style hex+ASCII dump; safe for arbitrary binary input."""
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        # 2-char hex bytes separated by spaces, padded so ASCII column aligns
        hex_cells = [f"{b:02x}" for b in chunk]
        hex_part = " ".join(hex_cells).ljust(width * 3 - 1)
        ascii_cells = []
        for b in chunk:
            if 32 <= b < 127:
                ch = chr(b)
                ascii_cells.append(_ANSI["green"] + ch + _ANSI["reset"]
                                   if color else ch)
            else:
                ascii_cells.append(_ANSI["dim"] + "." + _ANSI["reset"]
                                   if color else ".")
        if color:
            off_str = f"{_ANSI['cyan']}{off:04x}{_ANSI['reset']}"
            hex_str = f"{_ANSI['yellow']}{hex_part}{_ANSI['reset']}"
        else:
            off_str = f"{off:04x}"
            hex_str = hex_part
        lines.append(f"{off_str}  {hex_str}  {''.join(ascii_cells)}")
    return "\n".join(lines)


def _dump_pretty(direction: str, peer: str, data: bytes, color: bool) -> None:
    body: str
    is_json = False
    try:
        obj = json.loads(data.decode("utf-8"))
        body = json.dumps(obj, indent=2, ensure_ascii=False)
        is_json = True
    except Exception:
        body = _hexdump(data, color)

    if direction == "TX":
        arrow, hue = "→ TX", _ANSI["bright_yellow"]
    else:
        arrow, hue = "← RX", _ANSI["bright_cyan"]
    if color:
        header = (
            f"{_ANSI['bold']}{hue}{arrow}{_ANSI['reset']} "
            f"{_ANSI['dim']}{peer} ({len(data)} bytes){_ANSI['reset']}"
        )
        if is_json:
            body = _colorize_json(body)
    else:
        header = f"{arrow} {peer} ({len(data)} bytes)"
    print(header, file=sys.stderr)
    print(body, file=sys.stderr, flush=True)


def k_to_c(kelvin: float) -> float:
    return round(kelvin - 273.15, 2)


def c_to_k(celsius: float) -> float:
    return round(celsius + 273.15, 2)


def decode_schedule(s: str) -> list[tuple[int, float]]:
    """Parse the telemetry 'schedule' string into [(week_second, celsius), ...]."""
    nums = json.loads(s)
    out = []
    for i in range(0, len(nums), 2):
        sec = int(nums[i])
        setpoint_k = nums[i + 1] / 100.0
        out.append((sec, round(setpoint_k - 273.15, 2)))
    return out


def encode_schedule(entries: list[tuple[int, float]]) -> str:
    """Inverse of decode_schedule. Returns the quoted-list string the device expects."""
    flat = []
    for sec, celsius in entries:
        flat.append(int(sec))
        flat.append(int(round((celsius + 273.15) * 100)))
    return "[ " + ", ".join(str(x) for x in flat) + "]"


class TermaError(RuntimeError):
    pass


def _local_ip(target: str = "255.255.255.255") -> str:
    """Best-effort source IP for the interface that would reach ``target``."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def _mdns_encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if label:
            b = label.encode("utf-8")
            out += bytes([len(b)]) + b
    return out + b"\x00"


def _mdns_read_name(buf: bytes, off: int) -> tuple[str, int]:
    """Read a DNS name (handling pointer compression) starting at ``off``.
    Returns the dotted name and the offset just after the name in the buffer
    where the name's bytes ended (not the pointer target)."""
    labels: list[str] = []
    saved = -1
    while True:
        if off >= len(buf):
            break
        length = buf[off]
        if length == 0:
            off += 1
            break
        if length & 0xC0 == 0xC0:  # pointer
            if saved < 0:
                saved = off + 2
            off = ((length & 0x3F) << 8) | buf[off + 1]
            continue
        off += 1
        labels.append(buf[off:off + length].decode("utf-8", "replace"))
        off += length
    return ".".join(labels), (saved if saved >= 0 else off)


def _mdns_parse(buf: bytes) -> dict[str, list[dict[str, Any]]]:
    """Parse a DNS message. Returns dict with 'an'/'ns'/'ar' lists, each entry
    {name, type, class, data}."""
    if len(buf) < 12:
        return {"an": [], "ns": [], "ar": []}
    qd, an, ns, ar = (buf[4] << 8 | buf[5], buf[6] << 8 | buf[7],
                      buf[8] << 8 | buf[9], buf[10] << 8 | buf[11])
    off = 12
    # skip questions
    for _ in range(qd):
        _, off = _mdns_read_name(buf, off)
        off += 4  # type+class
    out: dict[str, list[dict[str, Any]]] = {"an": [], "ns": [], "ar": []}
    for section, count in (("an", an), ("ns", ns), ("ar", ar)):
        for _ in range(count):
            if off >= len(buf):
                break
            name, off = _mdns_read_name(buf, off)
            if off + 10 > len(buf):
                break
            rtype = (buf[off] << 8) | buf[off + 1]
            rclass = (buf[off + 2] << 8) | buf[off + 3]
            rdlen = (buf[off + 8] << 8) | buf[off + 9]
            off += 10
            rdata = buf[off:off + rdlen]
            data: Any
            if rtype == 1 and rdlen == 4:  # A
                data = ".".join(str(b) for b in rdata)
            elif rtype == 12:  # PTR
                data, _ = _mdns_read_name(buf, off)
            elif rtype == 33 and rdlen >= 6:  # SRV
                pri = (rdata[0] << 8) | rdata[1]
                weight = (rdata[2] << 8) | rdata[3]
                port_ = (rdata[4] << 8) | rdata[5]
                target, _ = _mdns_read_name(buf, off + 6)
                data = {"priority": pri, "weight": weight,
                        "port": port_, "target": target}
            elif rtype == 16:  # TXT
                txt = {}
                i = 0
                while i < len(rdata):
                    ln = rdata[i]; i += 1
                    pair = rdata[i:i + ln].decode("utf-8", "replace")
                    i += ln
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        txt[k] = v
                    elif pair:
                        txt[pair] = ""
                data = txt
            else:
                data = rdata.hex()
            out[section].append({"name": name, "type": rtype,
                                 "class": rclass, "data": data})
            off += rdlen
    return out


def _mdns_unescape_serial(label: str) -> str:
    """Reverse the Terma mDNS label encoding (``V`` substituted for ``#``)."""
    return label.replace("V", "#")


def discover(
    timeout: float = 3.0,
    service: str = MDNS_SERVICE,
    debug: bool = False,
) -> list[dict[str, Any]]:
    """Discover Terma thermostats on the LAN via mDNS (``_terma._tcp.local``).

    Sends a PTR query for the service and parses replies for SRV/A/TXT
    records. Returns ``[{ip, serial, host, port, txt, ...}, ...]``. The
    ``serial`` field is the mDNS instance with ``V`` mapped back to ``#`` so
    it can be used directly in the TCP 5005 protocol.

    This is the path the device actually uses in normal operation. For the
    pairing-mode UDP-broadcast probe (port 2349), see :func:`discover_pairing`.
    """
    color = _use_color() and debug
    # Build query
    qid = 0x1234
    header = bytes([qid >> 8, qid & 0xFF, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0])
    name = _mdns_encode_name(service)
    # PTR (12), IN class (1) with QU bit (0x8000) → 0x8001 (request unicast reply)
    qclass = 0x8001
    question = name + bytes([0, 12, (qclass >> 8) & 0xFF, qclass & 0xFF])
    query = header + question

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))

    try:
        if debug:
            _dump_pretty("TX", f"{MDNS_GROUP}:{MDNS_PORT} (mDNS PTR {service})",
                         query, color)
        sock.sendto(query, (MDNS_GROUP, MDNS_PORT))

        deadline = time.monotonic() + timeout
        devices: dict[str, dict[str, Any]] = {}
        # Per-peer parsed records (so we can stitch SRV/A/TXT into one entry)
        per_ip: dict[str, dict[str, Any]] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                raw, (peer_ip, _peer_port) = sock.recvfrom(4096)
            except socket.timeout:
                break
            if debug:
                _dump_pretty("RX", peer_ip, raw, color)
            try:
                parsed = _mdns_parse(raw)
            except Exception as e:
                log.debug("discover: mDNS parse failed from %s: %s", peer_ip, e)
                continue
            entry = per_ip.setdefault(peer_ip, {})
            for section in ("an", "ar", "ns"):
                for rec in parsed[section]:
                    if rec["type"] == 12 and rec["name"].endswith(service):
                        entry["ptr"] = rec["data"]
                    elif rec["type"] == 33:
                        entry["srv"] = rec["data"]
                        entry["srv_name"] = rec["name"]
                    elif rec["type"] == 1:
                        entry.setdefault("a", []).append(
                            {"name": rec["name"], "ip": rec["data"]})
                    elif rec["type"] == 16:
                        entry["txt"] = rec["data"]

        for peer_ip, entry in per_ip.items():
            ptr = entry.get("ptr") or entry.get("srv_name")
            if not ptr or not ptr.endswith(service):
                continue
            instance = ptr[: -(len(service) + 1)]  # strip ".<service>"
            serial = _mdns_unescape_serial(instance)
            ip = peer_ip
            for a in entry.get("a", []):
                # Prefer A records that name the device instance
                if a["name"].startswith(instance) or a["name"].startswith(
                        instance + ".local"):
                    ip = a["ip"]; break
            devices.setdefault(serial, {
                "ip": ip,
                "serial": serial,
                "instance": instance,
                "host": entry.get("srv", {}).get("target")
                        if isinstance(entry.get("srv"), dict) else None,
                "port": entry.get("srv", {}).get("port")
                        if isinstance(entry.get("srv"), dict) else None,
                "txt": entry.get("txt", {}),
            })
        return list(devices.values())
    finally:
        sock.close()


def discover_pairing(
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
    broadcast_address: str = "255.255.255.255",
    port: int = DEFAULT_BROADCAST_PORT,
    sender_ip: str | None = None,
    serials: Iterable[str] | None = None,
    debug: bool = False,
) -> list[dict[str, Any]]:
    """Broadcast a ``DeviceConfigurationRequest`` and collect responses.

    Only useful while the device is in **pairing mode** (its own AP, before
    Wi-Fi credentials are written). After pairing, the device stops answering
    these and announces itself via mDNS instead — see :func:`discover`.

    Returns a list of dicts:
        {"ip": <str>, "serial": <str>, "identifier": <uuid>, "raw": <dict>}

    Implements the protocol described in PROTOCOL.md §3 — UDP broadcast on
    port 2349, frame keys prefixed ``_terma_message_*``. The device replies
    on the port given in ``_terma_message_channel``, so we use one socket
    bound to an ephemeral port for receiving and a separate unbound socket
    to send the broadcast — matching ``HouseNetworkService`` in the app.
    """
    color = _use_color() and debug
    sender = sender_ip or _local_ip(broadcast_address)

    # Receiver socket — bound to an ephemeral port. Its local port is what
    # the device will reply to (the protocol field `_terma_message_channel`).
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", 0))
    reply_port = rx.getsockname()[1]

    # Sender socket — unbound, broadcast enabled.
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    frame = {
        "_terma_message_identifier": str(uuid.uuid4()),
        "_terma_message_type": "DeviceConfigurationRequest",
        "_terma_message_sender": sender,
        "_terma_message_recipients": list(serials) if serials else None,
        "_terma_message_channel": reply_port,
        "_terma_message_payload": None,
    }
    data = json.dumps(frame, separators=(",", ":")).encode("utf-8")

    try:
        if debug:
            _dump_pretty("TX", f"{broadcast_address}:{port} (reply→{reply_port})",
                         data, color)
        tx.sendto(data, (broadcast_address, port))

        deadline = time.monotonic() + timeout
        found: dict[str, dict[str, Any]] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            rx.settimeout(remaining)
            try:
                raw, (peer_ip, _peer_port) = rx.recvfrom(2048)
            except socket.timeout:
                break
            # device pads to ~1024 bytes with NULs/spaces — strip both
            body = raw.rstrip(b"\x00 \t\r\n")
            if debug:
                _dump_pretty("RX", peer_ip, body, color)
            try:
                obj = json.loads(body.decode("utf-8"))
            except Exception as e:
                log.debug("discover: skipping non-JSON datagram from %s: %s",
                          peer_ip, e)
                continue
            if obj.get("_terma_message_type") != "DeviceConfigurationResponse":
                continue
            serial = obj.get("_terma_message_device_identifier")
            if not serial or serial in found:
                continue
            found[serial] = {
                "ip": peer_ip,
                "serial": serial,
                "identifier": obj.get("_terma_message_identifier"),
                "raw": obj,
            }
        return list(found.values())
    finally:
        tx.close()
        rx.close()


class TermaClient:
    def __init__(
        self,
        host: str,
        serial: str,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        debug: bool = False,
    ):
        self.host = host
        self.serial = serial
        self.port = port
        self.timeout = timeout
        self.debug = debug
        self._color = _use_color()
        if debug:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
            )
            log.setLevel(logging.DEBUG)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"serial": self.serial, "timestamp": int(time.time()), **payload}
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        peer = f"{self.host}:{self.port}"
        log.debug("connect %s", peer)
        with socket.create_connection((self.host, self.port), self.timeout) as s:
            s.settimeout(self.timeout)
            if self.debug:
                _dump_pretty("TX", peer, data, self._color)
            s.sendall(data)
            buf = b""
            depth = 0
            in_str = False
            esc = False
            started = False
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
                for c in chunk:
                    ch = chr(c)
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    else:
                        if ch == '"':
                            in_str = True
                        elif ch == "{":
                            depth += 1
                            started = True
                        elif ch == "}":
                            depth -= 1
                if started and depth == 0:
                    break
        if self.debug:
            _dump_pretty("RX", peer, buf, self._color)
        if not buf:
            raise TermaError("empty response")
        try:
            resp = json.loads(buf.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise TermaError(f"invalid JSON response: {e}; raw={buf!r}") from e
        if resp.get("errorCode") not in (None, "None"):
            raise TermaError(f"device error: {resp['errorCode']}")
        return resp

    def get_telemetry(self) -> dict[str, Any]:
        resp = self._request({"actionId": ACTION_GET_TELEMETRY})
        return resp.get("telemetry", {})

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        return self._request({"actionId": ACTION_SET, "enable": 1 if enabled else 0})

    def set_dryer(self, on: bool, timer_seconds: int = 3600) -> dict[str, Any]:
        return self._request(
            {"actionId": ACTION_SET, "dryer": 1 if on else 0, "dryerTimer": int(timer_seconds)}
        )

    def set_parental_control(self, on: bool) -> dict[str, Any]:
        return self._request(
            {"actionId": ACTION_SET, "parentalControl": 1 if on else 0}
        )

    def set_zone_weight(self, weight: float) -> dict[str, Any]:
        """Heating level as a fraction 0.0..1.0 (what the app shows as 0..100%).
        Mirrored back in telemetry as `heatingCoefficient`."""
        if not 0.0 <= weight <= 1.0:
            raise ValueError("zone_weight must be in [0.0, 1.0]")
        return self._request({"actionId": ACTION_SET, "zoneWeight": float(weight)})

    def set_heating_level(self, percent: float) -> dict[str, Any]:
        """Heating level as a percentage 0..100 (UI semantics)."""
        if not 0 <= percent <= 100:
            raise ValueError("percent must be in [0, 100]")
        return self.set_zone_weight(percent / 100.0)

    def set_heating_power(self, watts: int) -> dict[str, Any]:
        """Configure rated heating element power in watts (PowerCapabilitiesCommand).
        Persists across reboots. Not echoed back in telemetry."""
        return self._request(
            {"actionId": ACTION_CONFIGURE_HEATER, "powerCapabilities": int(watts)}
        )

    def set_target_temperature(
        self,
        celsius: float,
        mode: int = MODE_MANUAL,
        timer_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Set manual target temperature.

        mode: MODE_MANUAL (0), MODE_MANUAL_WITH_TIMER (1, requires timer_seconds),
              or MODE_MANUAL_SCHEDULE_AWARE (2).
        Temperature is encoded as Kelvin × 100 (e.g. 21.0°C → 29415)."""
        payload: dict[str, Any] = {
            "actionId": ACTION_CONFIGURE_HEATER,
            "manualMode": int(mode),
            "setTemperature": int(round((celsius + 273.15) * 100)),
        }
        if mode == MODE_MANUAL_WITH_TIMER:
            if timer_seconds is None:
                raise ValueError("MODE_MANUAL_WITH_TIMER requires timer_seconds")
            payload["manualTimer"] = int(timer_seconds)
        else:
            payload["manualTimer"] = None
        return self._request(payload)

    def exit_manual_mode(self) -> dict[str, Any]:
        """Resume schedule, dropping any manual override."""
        return self._request(
            {"actionId": ACTION_CONFIGURE_HEATER, "exitManualMode": 1}
        )

    def set_calibrate(self, enable: bool) -> dict[str, Any]:
        """Toggle calibration mode (sensor adjustment)."""
        return self._request(
            {"actionId": ACTION_CONFIGURE_HEATER, "calibrate": 1 if enable else 0}
        )

    def set_zone_sensor(self, mac: str) -> dict[str, Any]:
        """Bind a remote temperature sensor to this heater's zone (MAC)."""
        return self._request(
            {"actionId": ACTION_CONFIGURE_HEATER, "zoneSensor": str(mac)}
        )

    def set_device_group(self, macs: Iterable[str]) -> dict[str, Any]:
        """Group sibling heaters that should heat together (list of MACs)."""
        return self._request({
            "actionId": ACTION_CONFIGURE_HEATER,
            "groupedDevicesMac": [str(m) for m in macs],
        })

    def set_devices_in_zone(
        self, zone_devices: Iterable[Iterable[str]]
    ) -> dict[str, Any]:
        """Zone-to-device topology: list-of-lists of MAC addresses, one inner
        list per zone."""
        return self._request({
            "actionId": ACTION_CONFIGURE_HEATER,
            "zoneDevicesMac": [[str(m) for m in zone] for zone in zone_devices],
        })

    def update_schedule(
        self,
        entries: list[tuple[int, float]],
        exit_manual_mode: bool = True,
    ) -> dict[str, Any]:
        """Replace the weekly schedule. Each entry is (week_second, celsius).
        Schedule is sent as a flat List<long> alternating (sec, K*100)."""
        flat: list[int] = []
        for sec, celsius in entries:
            flat.append(int(sec))
            flat.append(int(round((celsius + 273.15) * 100)))
        return self._request({
            "actionId": ACTION_UPDATE_SCHEDULE,
            "schedule": flat,
            "exitManualMode": 1 if exit_manual_mode else 0,
        })

    # Back-compat: the string-encoded variant the telemetry uses
    def set_schedule(self, entries: list[tuple[int, float]]) -> dict[str, Any]:
        """Deprecated: prefer update_schedule(). Sends schedule as the legacy
        bracketed string form."""
        return self._request(
            {"actionId": ACTION_CONFIGURE_HEATER, "schedule": encode_schedule(entries)}
        )

    def fetch_telemetry(self, mac: str) -> dict[str, Any]:
        """actionId=15 variant. Requires the device's MAC address."""
        resp = self._request({"actionId": ACTION_TELEMETRY_FETCH, "mac": mac})
        return resp.get("telemetry", {})

    def configure_network(self, ssid: str, password: str) -> dict[str, Any]:
        """Reconfigure Wi-Fi credentials. Device will likely reconnect/restart."""
        return self._request({
            "actionId": ACTION_CONFIGURE_NETWORK,
            "ssid": ssid,
            "password": password,
        })

    def set_secure_http(self, enable: bool) -> dict[str, Any]:
        """Toggle the cloud HTTPS channel (ExtendedControlApi, actionId=26)."""
        return self._request({
            "actionId": ACTION_EXTENDED_CONTROL,
            "setSecuredHttp": 1 if enable else 0,
        })

    def set_api_endpoint(self, url: str, port: int) -> dict[str, Any]:
        """Repoint the device's cloud endpoint (ConfigureApi, actionId=14).
        DANGEROUS: incorrect values may detach the device from the cloud."""
        return self._request({
            "actionId": ACTION_CONFIGURE_API,
            "apiUrl": url,
            "apiPort": int(port),
        })

    def raw(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(payload)


class TermaCloudError(RuntimeError):
    """Raised for any cloud error: HTTP non-2xx, BaseResponse error envelopes,
    or transport failures. ``status`` is the HTTP status (``None`` for
    transport errors), ``body`` is the parsed JSON if available, ``raw`` is
    the bytes the server sent."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: Any = None,
        raw: bytes | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.raw = raw


# Cloud BaseResponse `type` discriminator values (BaseResponseType.kt).
CLOUD_RESPONSE_OK = "CommandsResponse"
CLOUD_RESPONSE_AUTH_FAILURE = "AuthenticationFailure"
CLOUD_RESPONSE_VALIDATION_ERROR = "ModelValidationError"
CLOUD_RESPONSE_UNEXPECTED = "UnexpectedException"


class TermaCloudClient:
    """Client for ``https://api.termasmart.com``.

    Wraps every request body in the ``BaseRequest`` envelope
    (``{"identifier": <uuid>, "payload": …}``) and parses the
    ``IBaseResponse`` envelope on the way back. ``Authorization: Bearer
    <access_token>`` is injected automatically. On HTTP 401 the client calls
    :meth:`refresh` once and replays the original request — matching the
    Android app's ``ResponseInterceptor`` / ``tryRefreshToken`` flow.

    Authentication state is held on the instance: ``access_token``,
    ``refresh_token``, ``user_name``. After :meth:`login` these are populated;
    they're also persisted in the response payload that :meth:`login` returns,
    so callers can stash them however they like.

    Path methods return the response *payload* (the inner object), not the
    envelope. Use :meth:`post` / :meth:`get` / :meth:`put` for raw access.
    """

    def __init__(
        self,
        base_url: str = CLOUD_API_BASE,
        access_token: str | None = None,
        refresh_token: str | None = None,
        user_name: str | None = None,
        timeout: float = CLOUD_HTTP_TIMEOUT_SECONDS,
        debug: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_name = user_name
        self.timeout = timeout
        self.debug = debug
        self._color = _use_color()
        if debug:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
            )
            log.setLevel(logging.DEBUG)

    # ------------------------------------------------------------------ HTTP

    def _full_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _build_headers(self, with_auth: bool) -> dict[str, str]:
        h = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        }
        if with_auth and self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _read_body(self, resp: Any) -> bytes:
        data = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
        return data

    def _http(
        self,
        method: str,
        path: str,
        body: bytes | None,
        with_auth: bool,
    ) -> tuple[int, bytes, dict[str, str]]:
        url = self._full_url(path)
        req = urllib.request.Request(
            url, data=body, method=method,
            headers=self._build_headers(with_auth),
        )
        if self.debug:
            label = f"{method} {url}"
            _dump_pretty("TX", label, body or b"", self._color)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = self._read_body(resp)
                headers = dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = self._read_body(e)
            except Exception:
                raw = e.read() if hasattr(e, "read") else b""
            headers = dict(e.headers.items()) if e.headers else {}
        if self.debug:
            _dump_pretty("RX", f"{status} {url}", raw, self._color)
        return status, raw, headers

    def _parse_envelope(
        self, status: int, raw: bytes
    ) -> tuple[dict[str, Any] | None, Any]:
        """Return ``(envelope_dict, payload)``. ``payload`` is unwrapped from
        the ``BaseResponse`` envelope when the server returned one."""
        if not raw:
            return None, None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise TermaCloudError(
                f"invalid JSON response (status {status}): {e}",
                status=status, raw=raw,
            ) from e
        if isinstance(obj, dict) and "payload" in obj:
            return obj, obj.get("payload")
        return obj if isinstance(obj, dict) else None, obj

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        with_envelope: bool = True,
        with_auth: bool = True,
        _allow_refresh: bool = True,
    ) -> Any:
        if with_envelope:
            body_obj: Any = {
                "identifier": str(uuid.uuid4()),
                "payload": payload if payload is not None else {},
            }
        else:
            body_obj = payload
        if body_obj is None:
            body_bytes: bytes | None = None
        else:
            body_bytes = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        status, raw, _headers = self._http(method, path, body_bytes, with_auth)

        # Auto-refresh on 401, once.
        if (
            status == 401
            and _allow_refresh
            and with_auth
            and self.refresh_token
            and not path.endswith("auth/refresh")
        ):
            log.debug("401 from %s — refreshing token and retrying", path)
            self.refresh()
            return self._request(
                method, path, payload,
                with_envelope=with_envelope,
                with_auth=with_auth,
                _allow_refresh=False,
            )

        envelope, payload_out = self._parse_envelope(status, raw)
        env_type = envelope.get("type") if isinstance(envelope, dict) else None

        if status >= 400 or env_type in (
            CLOUD_RESPONSE_AUTH_FAILURE,
            CLOUD_RESPONSE_VALIDATION_ERROR,
            CLOUD_RESPONSE_UNEXPECTED,
        ):
            msg = self._error_message(status, env_type, payload_out, raw)
            raise TermaCloudError(msg, status=status, body=envelope, raw=raw)

        return payload_out

    @staticmethod
    def _error_message(
        status: int, env_type: str | None, payload: Any, raw: bytes
    ) -> str:
        if isinstance(payload, dict):
            for k in ("message", "errorMessage", "reason", "detail"):
                if payload.get(k):
                    return f"HTTP {status} {env_type or ''}: {payload[k]}".strip()
            if "errors" in payload:
                return f"HTTP {status} {env_type or ''}: {payload['errors']}".strip()
        snippet = raw[:200].decode("utf-8", "replace") if raw else ""
        return f"HTTP {status} {env_type or ''}: {snippet}".strip()

    # Public raw escape hatches.
    def post(self, path: str, payload: Any = None) -> Any:
        """POST <path> with a BaseRequest envelope. Returns the response
        payload (unwrapped) or raises :class:`TermaCloudError`."""
        return self._request("POST", path, payload)

    def get(self, path: str) -> Any:
        """GET <path>. Used by endpoints like ``users-settings`` that don't
        take a body. Returns the response payload (unwrapped)."""
        return self._request("GET", path, None, with_envelope=False)

    def put(self, path: str, payload: Any = None) -> Any:
        """PUT <path> with a BaseRequest envelope."""
        return self._request("PUT", path, payload)

    # ------------------------------------------------------------------ auth

    def login(self, email: str, password: str) -> dict[str, Any]:
        """``POST /auth/signIn`` — email/password login. Body shape matches
        ``LoginUserRequestPayload`` (``{email, password}``); path is the
        ``"auth/signIn"`` literal from ``TermaUserAuthService.java``. Stores
        ``token``, ``refreshToken``, ``userName`` from the response on this
        instance for subsequent calls."""
        payload = self._request(
            "POST", "auth/signIn",
            {"email": email, "password": password},
            with_auth=False,
        )
        self._absorb_tokens(payload)
        return payload

    def sign_in_external(
        self,
        provider: str,
        token: str,
        **extra: Any,
    ) -> dict[str, Any]:
        """``POST /auth/external/signIn`` — external (Facebook/Apple/Google)
        sign-in. Pass the provider's identity token; extra kwargs are merged
        into the request payload."""
        body = {"provider": provider, "token": token}
        body.update(extra)
        payload = self._request(
            "POST", "auth/external/signIn", body, with_auth=False,
        )
        self._absorb_tokens(payload)
        return payload

    def refresh(self) -> dict[str, Any]:
        """``POST /auth/refresh`` — exchange the refresh token for a new
        access+refresh pair. Updates ``self.access_token`` /
        ``self.refresh_token``."""
        if not self.refresh_token:
            raise TermaCloudError("refresh() requires a refresh_token")
        body = {
            "userName": self.user_name or "",
            "token": self.access_token or "",
            "refreshToken": self.refresh_token,
        }
        payload = self._request(
            "POST", "auth/refresh", body,
            with_auth=False, _allow_refresh=False,
        )
        self._absorb_tokens(payload)
        return payload

    def register(
        self, email: str, password: str, confirm_password: str | None = None
    ) -> dict[str, Any]:
        """``POST /auth/register`` — create a local account. If
        ``confirm_password`` is omitted it defaults to ``password``."""
        return self._request("POST", "auth/register", {
            "email": email,
            "password": password,
            "confirmPassword": confirm_password if confirm_password is not None
                else password,
        }, with_auth=False)

    def register_external(self, **payload: Any) -> dict[str, Any]:
        """``POST /auth/external/register`` — register via an external
        provider. Pass the provider-specific fields as kwargs."""
        return self._request(
            "POST", "auth/external/register", payload, with_auth=False,
        )

    def confirm_email(self, email: str, token: str) -> dict[str, Any]:
        """``POST /auth/confirmEmail`` — complete email verification."""
        return self._request(
            "POST", "auth/confirmEmail",
            {"email": email, "token": token}, with_auth=False,
        )

    def reset_password(self, email: str) -> dict[str, Any]:
        """``POST /auth/resetPassword`` — start the password-reset flow."""
        return self._request(
            "POST", "auth/resetPassword", {"email": email}, with_auth=False,
        )

    def confirm_reset_password(
        self,
        email: str,
        token: str,
        new_password: str,
        confirm_password: str | None = None,
    ) -> dict[str, Any]:
        """``POST /auth/confirmChangePassword`` — finish password reset.
        Server reuses the change-password confirmation endpoint for both
        flows (only ``auth/confirmChangePassword`` is present in the dex)."""
        return self._request("POST", "auth/confirmChangePassword", {
            "email": email,
            "token": token,
            "password": new_password,
            "confirmPassword": confirm_password if confirm_password is not None
                else new_password,
        }, with_auth=False)

    def change_password(
        self,
        current_password: str,
        new_password: str,
        confirm_new_password: str | None = None,
    ) -> dict[str, Any]:
        """``POST /auth/changePassword`` — change the current account's
        password."""
        return self._request("POST", "auth/changePassword", {
            "currentPassword": current_password,
            "newPassword": new_password,
            "confirmNewPassword": confirm_new_password if confirm_new_password
                is not None else new_password,
        })

    def _absorb_tokens(self, payload: Any) -> None:
        """Pull token/refreshToken/userName out of an auth response payload
        and stash them on the instance."""
        if not isinstance(payload, dict):
            return
        if payload.get("token"):
            self.access_token = payload["token"]
        if payload.get("refreshToken"):
            self.refresh_token = payload["refreshToken"]
        if payload.get("userName"):
            self.user_name = payload["userName"]

    # ---------------------------------------------------------------- houses

    def get_houses(self) -> Any:
        """``POST /house`` — the all-houses collection request (the Kotlin
        method is ``getAllHouses``). Empty payload."""
        return self._request("POST", "house", {})

    def create_house(self, **payload: Any) -> Any:
        """``POST /createHouse``."""
        return self._request("POST", "createHouse", payload)

    def delete_house(self, house_identifier: str) -> Any:
        """``POST /deleteHouse``."""
        return self._request(
            "POST", "deleteHouse",
            {"houseIdentifier": house_identifier},
        )

    def get_topology(self, house_identifier: str | None = None) -> Any:
        """``POST /house/topology`` — full house structure (zones, devices,
        schedules, last-known telemetry). The Kotlin payload has no required
        fields (``HouseTopologyRequestPayload`` only has the ``none`` field);
        passing ``house_identifier`` filters to a single house when supported
        by the server."""
        body: dict[str, Any] = {}
        if house_identifier is not None:
            body["houseIdentifier"] = house_identifier
        return self._request("POST", "house/topology", body)

    def add_house_device(self, **payload: Any) -> Any:
        """``POST /addHouseDevice``."""
        return self._request("POST", "addHouseDevice", payload)

    def update_house_device(self, **payload: Any) -> Any:
        """``POST /updateHouseDevice``."""
        return self._request("POST", "updateHouseDevice", payload)

    def delete_house_device(self, **payload: Any) -> Any:
        """``POST /deleteHouseDevice``."""
        return self._request("POST", "deleteHouseDevice", payload)

    def add_house_user(self, **payload: Any) -> Any:
        """``POST /addHouseUser``."""
        return self._request("POST", "addHouseUser", payload)

    def delete_house_user(self, **payload: Any) -> Any:
        """``POST /deleteHouseUser``."""
        return self._request("POST", "deleteHouseUser", payload)

    def create_house_zone(self, **payload: Any) -> Any:
        """``POST /createHouseZone``."""
        return self._request("POST", "createHouseZone", payload)

    def update_house_zone(self, **payload: Any) -> Any:
        """``POST /updateHouseZone``."""
        return self._request("POST", "updateHouseZone", payload)

    def delete_house_zone(self, **payload: Any) -> Any:
        """``POST /deleteHouseZone``."""
        return self._request("POST", "deleteHouseZone", payload)

    def update_house_info(self, **payload: Any) -> Any:
        """``POST /updateHouseInfo``."""
        return self._request("POST", "updateHouseInfo", payload)

    def update_house_configuration(self, **payload: Any) -> Any:
        """``POST /updateHouseConfiguration``."""
        return self._request("POST", "updateHouseConfiguration", payload)

    def create_house_schedule(self, **payload: Any) -> Any:
        """``POST /createHouseSchedule``."""
        return self._request("POST", "createHouseSchedule", payload)

    def update_house_schedule(self, **payload: Any) -> Any:
        """``POST /updateHouseSchedule``."""
        return self._request("POST", "updateHouseSchedule", payload)

    def delete_house_schedule(self, **payload: Any) -> Any:
        """``POST /deleteHouseSchedule``."""
        return self._request("POST", "deleteHouseSchedule", payload)

    def add_house_zone_schedule(self, **payload: Any) -> Any:
        """``POST /addHouseZoneSchedule``."""
        return self._request("POST", "addHouseZoneSchedule", payload)

    def delete_house_zone_schedule(self, **payload: Any) -> Any:
        """``POST /deleteHouseZoneSchedule``."""
        return self._request("POST", "deleteHouseZoneSchedule", payload)

    # --------------------------------------------------------- notifications

    def get_house_notifications(self, **payload: Any) -> Any:
        """``POST /house/notifications`` — list per-house notifications."""
        return self._request("POST", "house/notifications", payload)

    def register_notification_device(self, registration_token: str) -> Any:
        """``POST /house/notifications/device/register`` — register an FCM
        device token."""
        return self._request(
            "POST", "house/notifications/device/register",
            {"registrationToken": registration_token},
        )

    # ---------------------------------------------------- location & weather

    def forward_geocoding(self, **payload: Any) -> Any:
        """``POST /house-location`` (``performForwardGeoCoding``)."""
        return self._request("POST", "house-location", {
            "method": "performForwardGeoCoding", **payload,
        })

    def reverse_geocoding(self, **payload: Any) -> Any:
        """``POST /house-location`` (``performReverseGeoCoding``)."""
        return self._request("POST", "house-location", {
            "method": "performReverseGeoCoding", **payload,
        })

    def get_current_weather(self, **payload: Any) -> Any:
        """``POST /house-location`` (``getCurrentWeather``)."""
        return self._request("POST", "house-location", {
            "method": "getCurrentWeather", **payload,
        })

    def update_user_location(self, **payload: Any) -> Any:
        """``POST /user-location`` (``updateUserLocation``)."""
        return self._request("POST", "user-location", {
            "method": "updateUserLocation", **payload,
        })

    def allow_location_tracking(self, allow: bool) -> Any:
        """``POST /user-location`` (``allowLocationTracking``)."""
        return self._request("POST", "user-location", {
            "method": "allowLocationTracking",
            "allow": bool(allow),
        })

    # -------------------------------------------------------- user settings

    def get_user_settings(self) -> Any:
        """``GET /users-settings`` — per-user preferences blob."""
        return self.get("users-settings")

    def update_user_settings(self, settings: dict[str, Any]) -> Any:
        """``PUT /users-settings``."""
        return self.put("users-settings", settings)

    def delete_user_account(self) -> Any:
        """``POST /users-settings/user-delete``."""
        return self._request("POST", "users-settings/user-delete", {})

    # ----------------------------------------------------- device control
    #
    # Cloud-side device control. Paths are observed in the APK's dex strings
    # (the decompiled Kotlin marks the relevant onRemote() bodies as "Method
    # not decompiled"); payload field names come from the
    # ``*RequestPayload`` classes under
    # ``core/api/internal/requests/device/remote/control/``.
    #
    # Note: these endpoints are **not** documented in PROTOCOL.md §4.2.
    # They were reverse-engineered for completeness; treat them as best-effort.
    # The simple ops take ``serialNumber`` (same string as the LAN ``serial``,
    # e.g. ``"HBKB693DAP#1#D1"``); ``device_change_temperature`` also needs
    # ``houseIdentifier`` and ``zone`` (zone *name* from the topology) — pass
    # them explicitly or let the helper look them up.

    def find_device(
        self,
        serial: str,
        topology: Any | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Locate a device in the topology by ``deviceIdentifier``.

        Returns ``(house, zone, device)`` dicts. Any missing element is
        ``None``. When ``topology`` is omitted, calls :meth:`get_topology`."""
        if topology is None:
            topology = self.get_topology()
        houses: list[Any] = []
        if isinstance(topology, dict):
            houses = topology.get("houses") or []
        elif isinstance(topology, list):
            houses = topology
        for house in houses:
            if not isinstance(house, dict):
                continue
            for zone in house.get("zones") or []:
                if not isinstance(zone, dict):
                    continue
                for dev in zone.get("devices") or []:
                    if isinstance(dev, dict) and dev.get("deviceIdentifier") == serial:
                        return house, zone, dev
        return None, None, None

    def device_standby(self, serial: str, enable: bool) -> Any:
        """``POST /house-device-control/common/standby`` — heater on/off."""
        return self._request("POST", "house-device-control/common/standby", {
            "serialNumber": serial, "enable": bool(enable),
        })

    def device_dryer(
        self, serial: str, enable: bool, duration_seconds: int = 3600,
    ) -> Any:
        """``POST /house-device-control/common/dryer`` — towel-rail boost."""
        return self._request("POST", "house-device-control/common/dryer", {
            "serialNumber": serial,
            "enable": bool(enable),
            "durationSeconds": int(duration_seconds),
        })

    def device_parental_control(self, serial: str, enable: bool) -> Any:
        """``POST /house-device-control/common/parental-control``."""
        return self._request(
            "POST", "house-device-control/common/parental-control",
            {"serialNumber": serial, "enable": bool(enable)},
        )

    def device_calibrate(self, serial: str, enable: bool) -> Any:
        """``POST /house-device-control/common/calibrate``."""
        return self._request(
            "POST", "house-device-control/common/calibrate",
            {"serialNumber": serial, "enable": bool(enable)},
        )

    def device_exit_manual_mode(self, serial: str) -> Any:
        """``POST /house-device-control/common/exit-manual-mode``."""
        return self._request(
            "POST", "house-device-control/common/exit-manual-mode",
            {"serialNumber": serial},
        )

    def device_check_awaiting_commands(self, serial: str) -> Any:
        """``POST /house-device-control/common/check-awaiting-commands``."""
        return self._request(
            "POST", "house-device-control/common/check-awaiting-commands",
            {"serialNumber": serial},
        )

    def device_check_for_updates(self, serial: str) -> Any:
        """``POST /house-device-control/common/check-4-updates``."""
        return self._request(
            "POST", "house-device-control/common/check-4-updates",
            {"serialNumber": serial},
        )

    def device_change_heating_coefficient(
        self, serial: str, coefficient: float,
    ) -> Any:
        """``POST /house-device-control/temperature/coefficient`` — heating
        level (0.0..1.0)."""
        return self._request(
            "POST", "house-device-control/temperature/coefficient",
            {"serialNumber": serial, "coefficient": float(coefficient)},
        )

    def device_change_temperature(
        self,
        serial: str,
        celsius: float,
        mode: int = MODE_MANUAL,
        timer_seconds: int | None = None,
        house_identifier: str | None = None,
        zone: str | None = None,
        topology: Any | None = None,
    ) -> Any:
        """``POST /house-device-control/temperature`` — manual setpoint.

        Cloud payload requires a ``destination`` (``houseIdentifier`` + zone
        name); when not supplied, :meth:`find_device` is called to look them
        up from the topology. ``celsius`` is converted to Kelvin (the
        cloud's ``TemperatureConfiguration.temperature`` is a ``double``).
        """
        if house_identifier is None or zone is None:
            h, z, _d = self.find_device(serial, topology)
            if h is None:
                raise TermaCloudError(
                    f"device serial {serial!r} not found in topology — "
                    "pass house_identifier/zone explicitly")
            house_identifier = house_identifier or h.get("identifier")
            zone = zone or (z.get("name") if z else None)
            if not house_identifier or zone is None:
                raise TermaCloudError(
                    f"topology entry for {serial!r} missing identifier/zone")
        config: dict[str, Any] = {
            "changeMode": int(mode),
            "temperature": float(celsius + 273.15),
            "durationSeconds": (
                int(timer_seconds) if timer_seconds is not None else None
            ),
        }
        return self._request("POST", "house-device-control/temperature", {
            "configuration": config,
            "destination": {"houseIdentifier": house_identifier, "zone": zone},
        })

    def device_telemetry(
        self,
        serial: str,
        topology: Any | None = None,
    ) -> dict[str, Any]:
        """``POST /house-device/status/telemetry/batch`` — live telemetry
        for every device in the matching house, filtered down to the one
        whose ``serialNumber`` matches ``serial``.

        Returns the inner ``DeviceTelemetry`` dict (same shape as the LAN
        response in §2.5 of PROTOCOL.md). Raises :class:`TermaCloudError`
        if the device isn't in topology or no telemetry was returned for it.
        ``topology`` is reused if supplied, otherwise :meth:`get_topology`
        is called.
        """
        house, _zone, _dev = self.find_device(serial, topology)
        if house is None:
            raise TermaCloudError(
                f"device serial {serial!r} not found in topology")
        house_identifier = house.get("identifier")
        if not house_identifier:
            raise TermaCloudError(
                f"topology entry for {serial!r} missing houseIdentifier")
        payload = self._request(
            "POST", "house-device/status/telemetry/batch",
            {"houseIdentifier": house_identifier},
        )
        entries: list[Any] = []
        if isinstance(payload, dict):
            entries = payload.get("telemetry") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("serialNumber") == serial:
                t = entry.get("telemetry")
                if isinstance(t, dict):
                    return t
        raise TermaCloudError(
            f"no telemetry returned for {serial!r} in batch response")
