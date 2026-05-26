#!/usr/bin/env python3
"""CLI for the Terma thermostat. See `terma_cli.py --help`."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from typing import Any

from terma import (
    TermaClient,
    TermaCloudClient,
    TermaCloudError,
    TermaError,
    decode_schedule,
    discover,
    discover_pairing,
    k_to_c,
    _ANSI,
    _use_color,
    CLOUD_API_BASE,
    MODE_MANUAL,
    MODE_MANUAL_WITH_TIMER,
    MODE_MANUAL_SCHEDULE_AWARE,
    DEFAULT_BROADCAST_PORT,
    DEFAULT_DISCOVERY_TIMEOUT,
    MDNS_SERVICE,
)


_DEFAULT_TOKEN_FILE = os.path.expanduser("~/.terma_token.json")


def _load_token_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_token_cache(path: str, data: dict) -> None:
    payload = json.dumps(data, indent=2)
    # chmod 600 — credentials.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)


def _clear_token_cache(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


_COLOR = _use_color() and sys.stdout.isatty()
if os.environ.get("NO_COLOR"):
    _COLOR = False
if os.environ.get("TERMA_FORCE_COLOR"):
    _COLOR = True


def _c(color: str, text) -> str:
    if not _COLOR:
        return str(text)
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def _bold(text) -> str:
    return _c("bold", text) if _COLOR else str(text)


def _onoff(v) -> str:
    if v in (1, True):
        return _c("green", "ON")
    if v in (0, False):
        return _c("dim", "off")
    return str(v)


def _row(label: str, value: str, width: int = 18) -> None:
    print(f"  {_c('cyan', label.ljust(width))} {value}")


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_device_time(ts) -> str:
    if not ts:
        return _c("dim", "(unknown)")
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return str(ts)
    dt = _dt.datetime.fromtimestamp(ts).astimezone()
    human = dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    delta = int(time.time()) - ts
    if abs(delta) <= 2:
        skew = _c("green", "in sync")
    elif abs(delta) <= 60:
        skew = _c("yellow", f"{delta:+d}s vs local")
    else:
        skew = _c("red", f"{delta:+d}s vs local")
    return f"{_c('yellow', human)}  ({skew})"


def _fmt_errors_flag(v) -> str:
    if v is None:
        return _c("dim", "—")
    s = str(v)
    if set(s) <= {"0"}:
        return _c("green", "clean") + " " + _c("dim", f"({s})")
    return _c("red", s)


def _fmt_temp(kelvin) -> str:
    if kelvin is None:
        return _c("dim", "—")
    c = k_to_c(kelvin)
    return f"{_c('yellow', f'{c:.2f}')} °C  {_c('dim', f'({kelvin} K)')}"


def _fmt_percent_frac(frac) -> str:
    if frac is None:
        return _c("dim", "—")
    pct = float(frac) * 100
    return f"{_c('yellow', f'{pct:.0f}%')}  {_c('dim', f'(raw {frac})')}"


def _print_status(t: dict, verbose: bool = False) -> None:
    if not t:
        print(_c("red", "(empty telemetry)"))
        return

    print(_bold("Terma thermostat") + "  " +
          _c("dim", f"serial {t.get('serial')}"))
    print(_c("dim",
             f"firmware {t.get('fwVersion')}   wifi {t.get('wfxFwVersion')}"
             f"   hw {t.get('hwVersion')}   class {t.get('devClass')}"))

    print()
    print(_bold("State"))
    _row("enabled", _onoff(t.get("isEnabled")))
    _row("heating", _onoff(t.get("isHeating")))
    _row("dryer", _onoff(t.get("isDryerOn")))
    _row("calibrating", _onoff(t.get("isCalibrateOn")))
    _row("parental lock", _onoff(t.get("isParentalControlOn")))
    _row("manual mode", _c("yellow", t.get("manualMode")))
    if "state" in t and t["state"] is not None:
        _row("state", _c("yellow", t.get("state")))

    print()
    print(_bold("Temperatures"))
    _row("room", _fmt_temp(t.get("temperature")))
    _row("heater", _fmt_temp(t.get("heaterTemperature")))
    _row("humidity", _c("yellow", t.get("humidity")))

    print()
    print(_bold("Power"))
    _row("heating level", _fmt_percent_frac(t.get("heatingCoefficient")))
    _row("power usage", f"{_c('yellow', t.get('powerUsage'))} kW")
    _row("battery", f"{_c('yellow', t.get('batteryLevel'))}%")

    print()
    print(_bold("Time"))
    _row("device time", _fmt_device_time(t.get("timestamp")))
    _row("errors flag", _fmt_errors_flag(t.get("errorsFlag")))

    sched = t.get("schedule")
    if sched:
        decoded = decode_schedule(sched)
        print()
        if verbose:
            print(_bold("Schedule") + _c("dim", f"  ({len(decoded)} entries)"))
            for sec, celsius in decoded:
                day_idx = (sec // 86400) % 7
                hh = (sec % 86400) // 3600
                mm = (sec % 3600) // 60
                day = _c("magenta", _DAYS[day_idx])
                t_str = _c("yellow", f"{hh:02d}:{mm:02d}")
                v_str = _c("yellow", f"{celsius:.2f}") + " °C"
                print(f"  {day} {t_str}  →  {v_str}")
        else:
            print(_bold("Schedule") + _c("dim", f"  ({len(decoded)} entries, use --verbose to expand)"))


_CLOUD_UNSUPPORTED = {
    "zone": "set raw zone weight via LAN; the cloud uses `heat` (coefficient).",
    "power": "powerCapabilities is a LAN-only persisted device config.",
    "zone-sensor": "zone-sensor binding is a LAN-only configuration.",
    "group": "device grouping is a LAN-only configuration.",
    "zone-devices": "zone topology editing is a LAN-only configuration.",
    "schedule": "use `topology` to view; schedules are edited via "
                "cloud house-mutation methods, not exposed in this CLI.",
    "discover": "discovery is a LAN broadcast.",
    "raw": "raw LAN payload doesn't apply to cloud.",
}


def _print_cloud_status(
    device: dict,
    zone: dict | None,
    house: dict | None,
    telemetry: dict | None = None,
    verbose: bool = False,
) -> None:
    """Cloud-mode status: topology context + (optional) live DeviceTelemetry."""
    if not device:
        print(_c("red", "(device not found in topology)"))
        return
    t = telemetry or {}
    print(_bold("Terma thermostat (cloud)") + "  " +
          _c("dim", f"serial {device.get('deviceIdentifier')}"))
    if house or zone:
        bits = []
        if house and house.get("name"):
            bits.append(f"house {house['name']!r}")
        if zone and zone.get("name"):
            bits.append(f"zone {zone['name']!r}")
        if bits:
            print(_c("dim", "  ".join(bits)))
    if t:
        print(_c("dim",
                 f"firmware {t.get('fwVersion')}   wifi {t.get('wfxFwVersion')}"
                 f"   hw {t.get('hwVersion')}   class {t.get('devClass')}"))

    print()
    print(_bold("Device"))
    _row("name", _c("yellow", device.get("name")))
    _row("type", _c("yellow", device.get("type")))
    if device.get("mac"):
        _row("mac", _c("yellow", device.get("mac")))
    if device.get("groupId"):
        _row("group", _c("yellow", device.get("groupId")))
    if device.get("heatingPowerCapabilities") is not None:
        _row("power (W)", _c("yellow", device.get("heatingPowerCapabilities")))

    if zone:
        print()
        print(_bold("Zone"))
        # standBy=True ⇒ heater idle (in standby); standBy=False ⇒ active.
        _row("standby", _onoff(1 if zone.get("standBy") else 0))
        set_t = zone.get("setTemperature")
        if set_t is not None:
            try:
                _row("set temperature",
                     f"{_c('yellow', f'{k_to_c(set_t):.2f}')} °C  "
                     f"{_c('dim', f'({set_t} K)')}")
            except Exception:
                _row("set temperature", str(set_t))
        for k in ("holidayModeTemperature", "smartLocalizationTemperature"):
            v = zone.get(k)
            if v is not None:
                _row(k, _c("yellow", v))

    if not t:
        return

    print()
    print(_bold("State"))
    _row("enabled", _onoff(t.get("isEnabled")))
    _row("heating", _onoff(t.get("isHeating")))
    _row("dryer", _onoff(t.get("isDryerOn")))
    _row("calibrating", _onoff(t.get("isCalibrateOn")))
    _row("parental lock", _onoff(t.get("isParentalControlOn")))
    _row("manual mode", _c("yellow", t.get("manualMode")))
    if "state" in t and t["state"] is not None:
        _row("state", _c("yellow", t.get("state")))

    print()
    print(_bold("Temperatures"))
    _row("room", _fmt_temp(t.get("temperature")))
    _row("heater", _fmt_temp(t.get("heaterTemperature")))
    _row("humidity", _c("yellow", t.get("humidity")))

    print()
    print(_bold("Power"))
    _row("heating level", _fmt_percent_frac(t.get("heatingCoefficient")))
    _row("power usage", f"{_c('yellow', t.get('powerUsage'))} kW")
    _row("battery", f"{_c('yellow', t.get('batteryLevel'))}%")

    print()
    print(_bold("Time"))
    _row("device time", _fmt_device_time(t.get("timestamp")))
    _row("errors flag", _fmt_errors_flag(t.get("errorsFlag")))

    sched = t.get("schedule")
    if sched:
        decoded = decode_schedule(sched)
        print()
        if verbose:
            print(_bold("Schedule") + _c("dim", f"  ({len(decoded)} entries)"))
            for sec, celsius in decoded:
                day_idx = (sec // 86400) % 7
                hh = (sec % 86400) // 3600
                mm = (sec % 3600) // 60
                day = _c("magenta", _DAYS[day_idx])
                t_str = _c("yellow", f"{hh:02d}:{mm:02d}")
                v_str = _c("yellow", f"{celsius:.2f}") + " °C"
                print(f"  {day} {t_str}  →  {v_str}")
        else:
            print(_bold("Schedule") + _c("dim",
                f"  ({len(decoded)} entries, use --verbose to expand)"))


def _ensure_cloud_authed(args) -> TermaCloudClient:
    """Build a TermaCloudClient, loading cached tokens. If no token is
    cached but --email/--password are set, log in fresh. Save the (possibly
    updated) tokens back to the cache when we're done."""
    cache = _load_token_cache(args.token_file)
    c = TermaCloudClient(
        base_url=args.cloud_base,
        access_token=cache.get("access_token"),
        refresh_token=cache.get("refresh_token"),
        user_name=cache.get("user_name"),
        timeout=args.timeout,
        debug=args.debug,
    )
    needs_login = not c.refresh_token
    # If the user passed credentials explicitly, prefer a fresh login over
    # whatever's cached (covers password-rotation and account-switching).
    cli_email = args.email is not None and args.email != cache.get("user_name")
    if needs_login or cli_email:
        if not args.email or not args.password:
            if needs_login:
                raise TermaCloudError(
                    "no cached token; pass --email and --password "
                    "(or set TERMA_EMAIL / TERMA_PASSWORD)")
        else:
            c.login(args.email, args.password)
    return c


def _persist_cloud_tokens(args, c: TermaCloudClient) -> None:
    _save_token_cache(args.token_file, {
        "base_url": c.base_url,
        "access_token": c.access_token,
        "refresh_token": c.refresh_token,
        "user_name": c.user_name,
    })


def _run_cloud(args, p) -> int:
    # logout doesn't need creds or a network call
    if args.cmd == "logout":
        _clear_token_cache(args.token_file)
        print(f"cleared {args.token_file}")
        return 0

    if args.cmd == "whoami":
        cache = _load_token_cache(args.token_file)
        if not cache:
            print(_c("dim", "(no cached token; run `login`)"))
            return 0
        if args.json:
            redacted = {**cache,
                        "access_token": "***" if cache.get("access_token") else None,
                        "refresh_token": "***" if cache.get("refresh_token") else None}
            print(json.dumps(redacted, indent=2))
        else:
            print(_bold("Cloud session"))
            _row("base url", _c("yellow", cache.get("base_url")))
            _row("user", _c("yellow", cache.get("user_name") or "(unknown)"))
            _row("token", _c("green", "present") if cache.get("access_token")
                          else _c("red", "missing"))
            _row("refresh", _c("green", "present") if cache.get("refresh_token")
                            else _c("red", "missing"))
        return 0

    try:
        c = _ensure_cloud_authed(args)
    except TermaCloudError as e:
        print(f"cloud auth error: {e}", file=sys.stderr)
        return 1

    try:
        result_payload: Any = None

        if args.cmd == "login":
            # _ensure_cloud_authed already did the login when needed; if we
            # got here on a cached token, force a fresh refresh.
            if args.email and args.password:
                c.login(args.email, args.password)
            elif c.refresh_token:
                c.refresh()
            else:
                raise TermaCloudError(
                    "no credentials and no cached refresh_token")
            _persist_cloud_tokens(args, c)
            print(_bold("logged in") +
                  _c("dim", f"  user {c.user_name!r}  base {c.base_url!r}"))
            return 0

        if args.cmd == "houses":
            result_payload = c.get_houses()
            _persist_cloud_tokens(args, c)
            if args.json:
                print(json.dumps(result_payload, indent=2))
                return 0
            houses = (result_payload or {}).get("houses") \
                if isinstance(result_payload, dict) else result_payload
            if not houses:
                print(_c("dim", "(no houses)"))
                return 0
            for h in houses:
                ident = h.get("identifier")
                name = h.get("name")
                tz = h.get("timeZone")
                zones = h.get("zones") or []
                print(f"  {_c('yellow', name)}  "
                      f"{_c('dim', ident)}  "
                      f"{_c('dim', f'tz={tz}  zones={len(zones)}')}")
            return 0

        if args.cmd == "topology":
            result_payload = c.get_topology()
            _persist_cloud_tokens(args, c)
            print(json.dumps(result_payload, indent=2))
            return 0

        # All remaining commands act on a specific device — need --serial
        if not args.serial:
            p.error("--serial is required for device commands "
                    "(or set TERMA_SERIAL)")

        if args.cmd in _CLOUD_UNSUPPORTED:
            print(f"`{args.cmd}` is not supported in --cloud mode: "
                  f"{_CLOUD_UNSUPPORTED[args.cmd]}", file=sys.stderr)
            return 2

        if args.cmd == "status":
            topology = c.get_topology()
            house, zone, device = c.find_device(args.serial, topology=topology)
            if device is None:
                _persist_cloud_tokens(args, c)
                print(_c("red",
                        f"device serial {args.serial!r} not found in topology"),
                      file=sys.stderr)
                return 1
            telemetry = c.device_telemetry(args.serial, topology=topology)
            _persist_cloud_tokens(args, c)
            if args.json:
                print(json.dumps({"house": house, "zone": zone,
                                  "device": device, "telemetry": telemetry},
                                 indent=2))
            else:
                _print_cloud_status(device, zone, house,
                                    telemetry=telemetry,
                                    verbose=args.verbose)
            return 0

        # Device-control commands.
        if args.cmd == "on":
            result_payload = c.device_standby(args.serial, True)
        elif args.cmd == "off":
            result_payload = c.device_standby(args.serial, False)
        elif args.cmd == "dryer":
            result_payload = c.device_dryer(args.serial, args.state == "on",
                                            duration_seconds=args.timer)
        elif args.cmd == "lock":
            result_payload = c.device_parental_control(
                args.serial, args.state == "on")
        elif args.cmd == "calibrate":
            result_payload = c.device_calibrate(
                args.serial, args.state == "on")
        elif args.cmd == "resume":
            result_payload = c.device_exit_manual_mode(args.serial)
        elif args.cmd == "heat":
            if not 0 <= args.percent <= 100:
                p.error("percent must be in [0, 100]")
            result_payload = c.device_change_heating_coefficient(
                args.serial, args.percent / 100.0)
        elif args.cmd == "temp":
            mode_map = {
                "manual": MODE_MANUAL,
                "timer": MODE_MANUAL_WITH_TIMER,
                "schedule-aware": MODE_MANUAL_SCHEDULE_AWARE,
            }
            result_payload = c.device_change_temperature(
                args.serial, args.celsius, mode=mode_map[args.mode],
                timer_seconds=args.timer,
            )
        else:
            p.error(f"unknown command {args.cmd!r} for --cloud")
            return 2

        _persist_cloud_tokens(args, c)
        print(json.dumps(result_payload, indent=2))
        return 0

    except TermaCloudError as e:
        # Make sure any refreshed tokens still get cached even on a later
        # request failure, so the next call doesn't burn a fresh login.
        try:
            _persist_cloud_tokens(args, c)
        except OSError:
            pass
        print(f"cloud error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"network error: {e}", file=sys.stderr)
        return 1


def _hoist_global_flags() -> None:
    """Allow global flags (--debug, --host, etc.) to appear after the subcommand."""
    opts_with_val = {
        "--host", "--serial", "--port", "--timeout",
        "--email", "--password", "--cloud-base", "--token-file",
    }
    opts_bool = {"--debug", "--verbose", "-v", "--json", "--cloud"}
    # don't touch args after `raw`, since its payload may be valid JSON containing
    # similar-looking tokens
    if "raw" in sys.argv[1:]:
        return
    args = sys.argv[1:]
    front, rest = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a in opts_bool:
            front.append(a)
            i += 1
        elif a in opts_with_val and i + 1 < len(args):
            front += [a, args[i + 1]]
            i += 2
        else:
            rest.append(a)
            i += 1
    sys.argv[1:] = front + rest


def main() -> int:
    p = argparse.ArgumentParser(description="Terma thermostat client (LAN + cloud)")
    p.add_argument("--host", default=os.environ.get("TERMA_HOST"),
                   help="thermostat IP (env TERMA_HOST) — LAN mode")
    p.add_argument("--serial", default=os.environ.get("TERMA_SERIAL"),
                   help="device serial (env TERMA_SERIAL)")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--debug", action="store_true", help="log raw TX/RX bytes")
    p.add_argument("--verbose", "-v", action="store_true", help="show full schedule in status")
    p.add_argument("--json", action="store_true", help="print raw JSON response")
    p.add_argument("--cloud", action="store_true",
                   help="talk to api.termasmart.com instead of LAN")
    p.add_argument("--email", default=os.environ.get("TERMA_EMAIL"),
                   help="cloud account email (env TERMA_EMAIL)")
    p.add_argument("--password", default=os.environ.get("TERMA_PASSWORD"),
                   help="cloud account password (env TERMA_PASSWORD)")
    p.add_argument("--cloud-base",
                   default=os.environ.get("TERMA_CLOUD_BASE", CLOUD_API_BASE),
                   help=f"cloud base URL (env TERMA_CLOUD_BASE, default {CLOUD_API_BASE})")
    p.add_argument("--token-file",
                   default=os.environ.get("TERMA_TOKEN_FILE", _DEFAULT_TOKEN_FILE),
                   help=f"token cache (env TERMA_TOKEN_FILE, default {_DEFAULT_TOKEN_FILE})")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="read telemetry (cloud: from topology)")
    sub.add_parser("on", help="enable heater")
    sub.add_parser("off", help="disable heater")

    sub.add_parser("login", help="cloud: log in and cache tokens (--cloud)")
    sub.add_parser("logout", help="cloud: clear cached tokens (--cloud)")
    sub.add_parser("whoami", help="cloud: show cached user (--cloud)")
    sub.add_parser("houses", help="cloud: list houses (--cloud)")
    sub.add_parser("topology", help="cloud: dump full topology (--cloud)")

    sp = sub.add_parser("dryer", help="dryer (towel-rail boost) on/off")
    sp.add_argument("state", choices=["on", "off"])
    sp.add_argument("--timer", type=int, default=3600, help="dryerTimer seconds (default 3600)")

    sp = sub.add_parser("lock", help="parental/child lock on/off")
    sp.add_argument("state", choices=["on", "off"])

    sp = sub.add_parser("zone", help="set zone weight 0.0..1.0 (raw protocol field)")
    sp.add_argument("weight", type=float)

    sp = sub.add_parser("heat", help="set heating level 0..100 percent")
    sp.add_argument("percent", type=float)

    sp = sub.add_parser("power", help="set rated heating element power in watts")
    sp.add_argument("watts", type=int)

    sp = sub.add_parser("temp", help="set manual target temperature (Celsius)")
    sp.add_argument("celsius", type=float)
    sp.add_argument("--mode", choices=["manual", "timer", "schedule-aware"],
                    default="manual",
                    help="manualMode (default: manual = no time bound)")
    sp.add_argument("--timer", type=int, default=None,
                    help="duration in seconds (required for --mode=timer)")

    sub.add_parser("resume", help="exit manual override and resume schedule")

    sp = sub.add_parser("calibrate", help="toggle sensor calibration mode")
    sp.add_argument("state", choices=["on", "off"])

    sub.add_parser("schedule", help="print full decoded schedule")

    sp = sub.add_parser("zone-sensor", help="bind a temperature sensor (MAC) to this zone")
    sp.add_argument("mac")

    sp = sub.add_parser("group", help="set device grouping (comma-separated MACs)")
    sp.add_argument("macs", help='e.g. "AA:BB:CC:DD:EE:FF,11:22:33:44:55:66"')

    sp = sub.add_parser("zone-devices",
                        help="set zone topology (one positional arg per zone, "
                             "each is comma-separated MACs)")
    sp.add_argument("zones", nargs="+",
                    help='e.g. "AA:BB,CC:DD" "EE:FF"')

    sp = sub.add_parser("discover",
                        help="discover thermostats on this LAN (mDNS by default; "
                             "use --pairing for UDP 2349 broadcast probe)")
    sp.add_argument("--timeout", type=float, default=3.0,
                    help="seconds to wait for responses (default 3)")
    sp.add_argument("--service", default=MDNS_SERVICE,
                    help=f"mDNS service (default {MDNS_SERVICE})")
    sp.add_argument("--pairing", action="store_true",
                    help="use UDP 2349 broadcast (pairing mode only)")
    sp.add_argument("--broadcast", default="255.255.255.255",
                    help="[pairing] broadcast address (default 255.255.255.255)")
    sp.add_argument("--broadcast-port", type=int, default=DEFAULT_BROADCAST_PORT,
                    help=f"[pairing] UDP port (default {DEFAULT_BROADCAST_PORT})")

    sp = sub.add_parser("raw", help="send raw JSON command (object body, no serial/timestamp)")
    sp.add_argument("payload", help='e.g. \'{"actionId":5,"enable":1}\'')

    _hoist_global_flags()
    args = p.parse_args()

    if args.cmd == "discover":
        try:
            if args.pairing:
                devices = discover_pairing(
                    timeout=args.timeout,
                    broadcast_address=args.broadcast,
                    port=args.broadcast_port,
                    debug=args.debug,
                )
            else:
                devices = discover(timeout=args.timeout,
                                   service=args.service,
                                   debug=args.debug)
        except OSError as e:
            print(f"network error: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(devices, indent=2))
            return 0
        if not devices:
            print(_c("dim", "(no devices answered)"))
            return 0
        mode = "pairing/UDP" if args.pairing else "mDNS"
        print(_bold(f"Found {len(devices)} device(s) via {mode}:"))
        for d in devices:
            extras = []
            if d.get("port") and d.get("port") != 5005:
                extras.append(f"port {d['port']}")
            if d.get("instance"):
                extras.append(f"({d['instance']})")
            tail = ("  " + _c("dim", "  ".join(extras))) if extras else ""
            print(f"  {_c('cyan', d['ip']):<20}  "
                  f"{_c('yellow', d['serial'])}{tail}")
        return 0

    # ---------------------------------------------------------------- cloud
    if args.cloud or args.cmd in {"login", "logout", "whoami", "houses",
                                  "topology"}:
        if not args.cloud:
            p.error(f"`{args.cmd}` requires --cloud")
        return _run_cloud(args, p)

    if not args.host or not args.serial:
        p.error("--host and --serial are required (or set TERMA_HOST / TERMA_SERIAL)")

    c = TermaClient(args.host, args.serial, port=args.port,
                    timeout=args.timeout, debug=args.debug)

    try:
        if args.cmd == "status":
            t = c.get_telemetry()
            if args.json:
                print(json.dumps(t, indent=2))
            else:
                _print_status(t, verbose=args.verbose)
            return 0

        if args.cmd == "on":
            resp = c.set_enabled(True)
        elif args.cmd == "off":
            resp = c.set_enabled(False)
        elif args.cmd == "dryer":
            resp = c.set_dryer(args.state == "on", timer_seconds=args.timer)
        elif args.cmd == "lock":
            resp = c.set_parental_control(args.state == "on")
        elif args.cmd == "zone":
            resp = c.set_zone_weight(args.weight)
        elif args.cmd == "heat":
            resp = c.set_heating_level(args.percent)
        elif args.cmd == "power":
            resp = c.set_heating_power(args.watts)
        elif args.cmd == "temp":
            mode_map = {
                "manual": MODE_MANUAL,
                "timer": MODE_MANUAL_WITH_TIMER,
                "schedule-aware": MODE_MANUAL_SCHEDULE_AWARE,
            }
            resp = c.set_target_temperature(
                args.celsius, mode=mode_map[args.mode],
                timer_seconds=args.timer,
            )
        elif args.cmd == "resume":
            resp = c.exit_manual_mode()
        elif args.cmd == "calibrate":
            resp = c.set_calibrate(args.state == "on")
        elif args.cmd == "zone-sensor":
            resp = c.set_zone_sensor(args.mac)
        elif args.cmd == "group":
            resp = c.set_device_group(
                [m.strip() for m in args.macs.split(",") if m.strip()]
            )
        elif args.cmd == "zone-devices":
            zones = [
                [m.strip() for m in z.split(",") if m.strip()]
                for z in args.zones
            ]
            resp = c.set_devices_in_zone(zones)
        elif args.cmd == "schedule":
            t = c.get_telemetry()
            for sec, celsius in decode_schedule(t.get("schedule", "[]")):
                day = sec // 86400
                hh = (sec % 86400) // 3600
                mm = (sec % 3600) // 60
                print(f"day {day} {hh:02d}:{mm:02d}  ->  {celsius} C")
            return 0
        elif args.cmd == "raw":
            resp = c.raw(json.loads(args.payload))
        else:
            p.error(f"unknown command {args.cmd!r}")
            return 2

        if args.json:
            print(json.dumps(resp, indent=2))
        else:
            _print_status(resp.get("telemetry", {}), verbose=args.verbose)
        return 0

    except TermaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"network error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
