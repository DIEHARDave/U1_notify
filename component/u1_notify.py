# U1_notify - Moonraker helper for the Snapmaker U1 (Extended Firmware)
#
# The notifications themselves are sent by Moonraker's own [notifier]
# sections (Apprise), which read the URL and printer name from
# ~/printer_data/moonraker.secrets. That file is hidden from Mainsail and
# Fluidd, so the user edits u1_notify.txt in the config folder instead and
# runs UPDATE_NOTIFY, which calls this component to:
#   - copy the [notify] section into moonraker.secrets, keeping anything
#     else already in that file,
#   - restart Moonraker so the notifiers load the new URL,
#   - send a confirmation message once Moonraker is back.
#
# Zmod sends its other messages from its own print and sensor macros.
# Snapmaker's macros don't, so this component watches Klipper's status
# instead and sends:
#   - "Printer on" when Klipper becomes ready after a boot   (notify_on)
#   - "Filament runout detected" while printing              (notify_filament)
#   - progress every notify_percent percent                  (notify_percent)
# The notify_* switches are Klipper saved variables, the same ones the
# _NOTIFY macro reads.
#
# When the URL is a Discord webhook, U1_notify's notifiers post Discord
# embeds themselves, laid out like FlashForgeUI-Electron's (status, temps,
# progress bar, time, ETA, layer, file, photo), instead of Apprise's plain
# text. If posting fails, the message goes out through Apprise as before.
# [u1_notify] discord_embeds: False turns this off.
#
# Copyright (C) 2026 DIEHARDave
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import configparser
import datetime
import functools
import io
import json
import logging
import pathlib
import re
import uuid

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .klippy_apis import KlippyAPI
    from .notifier import Notifier, NotifierInstance
    from .http_client import HttpClient

SECTION = "notify"
# Klippy becoming ready this soon after the system boots counts as a power on.
BOOT_WINDOW = 300.
ACTIVE_STATES = ("printing", "paused")
# The U1 has four toolheads, each with its own filament motion sensor.
EXTRUDERS = ("extruder", "extruder1", "extruder2", "extruder3")
SENSOR_FMT = "filament_motion_sensor e%d_filament"
# Seconds to wait before each attempt to reach Discord.
RETRY_DELAYS = (0., 10., 20., 30.)

# The notifiers in u1_notify.cfg; users' own notifiers are left alone.
OUR_NOTIFIERS = (
    "print_start", "print_complete", "print_error", "print_cancelled",
    "print_paused", "print_resumed", "notifier", "notifier_photo",
)
DISCORD_RE = re.compile(
    r"^(?:https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/"
    r"|discord://(?:[^@/]*@)?)(?P<id>\d+)/(?P<token>[\w-]+)"
)
# FlashForgeUI-Electron's status labels and embed colors.
STATUS_LABELS = {
    "Ready": "✅ Ready",
    "Printing": "🖨️ Printing",
    "Paused": "⏸️ Paused",
    "Completed": "✅ Completed",
    "Error": "❌ Error",
    "Cancelled": "🚫 Cancelled",
}
STATUS_COLORS = {"Printing": 0x00ff00, "Ready": 0x3498db, "Paused": 0xf39c12}
EVENT_STATUS = {
    "started": "Printing",
    "resumed": "Printing",
    "paused": "Paused",
    "error": "Error",
    "cancelled": "Cancelled",
}
KLIPPER_STATUS = {
    "printing": "Printing",
    "paused": "Paused",
    "error": "Error",
}


class U1Notify:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        data_path = pathlib.Path(self.server.get_app_args()["data_path"])
        self.secrets_file = data_path.joinpath("moonraker.secrets")
        self.settings_file = pathlib.Path(config.get(
            "settings_file", str(data_path.joinpath("config/u1_notify.txt"))
        )).expanduser()
        self.discord_embeds = config.getboolean("discord_embeds", True)
        self.progress_message: Optional[str] = None
        self.flag_file = data_path.joinpath(".u1_notify_updated")
        self.boot_checked = False
        self.sensors: List[str] = []
        self.status: Dict[str, Dict[str, Any]] = {}
        self.notified_percent = 0
        self.runout_sent = False

        self._restore_settings()
        self.server.register_remote_method(
            "u1_notify_update", self._handle_update)
        self.server.register_event_handler(
            "server:klippy_ready", self._handle_klippy_ready)
        self.server.register_event_handler(
            "file_manager:filelist_changed", self._handle_file_changed)

    # u1_notify.txt is the user's copy; if it went missing, rebuild it from
    # the secrets.
    def _restore_settings(self) -> None:
        if self.settings_file.exists():
            return
        values = self._read_secrets().get(SECTION)
        if not isinstance(values, dict):
            return
        lines = [f"[{SECTION}]"] + [f"{k}: {v}" for k, v in values.items()]
        try:
            self.settings_file.write_text("\n".join(lines) + "\n")
        except OSError:
            logging.exception("u1_notify: could not restore settings")

    def _read_secrets(self) -> Dict[str, Any]:
        if not self.secrets_file.is_file():
            return {}
        data = self.secrets_file.read_text()
        try:
            vals = json.loads(data)
        except json.JSONDecodeError:
            pass
        else:
            return vals if isinstance(vals, dict) else {}
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(data)
        except configparser.Error:
            return {}
        return {s: dict(parser[s]) for s in parser.sections()}

    def _read_settings(self) -> Dict[str, str]:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(self.settings_file.read_text())
        if not parser.has_section(SECTION):
            raise ValueError(f"no [{SECTION}] section")
        values = dict(parser[SECTION])
        url = values.get("url", "").strip()
        if not url or "://" not in url:
            raise ValueError("'url:' is missing or isn't an Apprise URL")
        if "{" in url:
            raise ValueError(
                "'url:' still contains a placeholder such as {bottoken}")
        values.setdefault("name", "")
        return values

    def _write_secrets(self, values: Dict[str, str]) -> None:
        data = (
            self.secrets_file.read_text() if self.secrets_file.is_file() else ""
        )
        try:
            vals = json.loads(data) if data.strip() else None
        except json.JSONDecodeError:
            vals = None
        if isinstance(vals, dict):
            vals[SECTION] = values
            out = json.dumps(vals, indent=2) + "\n"
        else:
            parser = configparser.ConfigParser(interpolation=None)
            if data.strip():
                parser.read_string(data)
            if parser.has_section(SECTION):
                parser.remove_section(SECTION)
            parser.add_section(SECTION)
            for key, val in values.items():
                parser.set(SECTION, key, val)
            buf = io.StringIO()
            parser.write(buf)
            out = buf.getvalue()
        tmp = self.secrets_file.with_suffix(".tmp")
        tmp.write_text(out)
        tmp.chmod(0o600)
        tmp.replace(self.secrets_file)

    def _respond(self, msg: str) -> None:
        self.server.send_event("server:gcode_response", f"// {msg}")

    # Saved in u1_notify.txt but not copied to the secrets yet, i.e.
    # UPDATE_NOTIFY hasn't been run since the last edit.
    def _settings_pending(self) -> bool:
        try:
            values = self._read_settings()
        except (OSError, ValueError, configparser.Error):
            return False
        current = self._read_secrets().get(SECTION)
        if not isinstance(current, dict):
            return True
        return any(
            str(current.get(k, "")).strip() != str(v).strip()
            for k, v in values.items()
        )

    def _remind_if_pending(self) -> None:
        if self._settings_pending():
            self._respond(
                f"U1_notify: {self.settings_file.name} has changes that "
                "aren't in use yet. Run UPDATE_NOTIFY to apply them.")

    def _handle_file_changed(self, info: Dict[str, Any]) -> None:
        item = info.get("item", {})
        if (
            info.get("action") in ("create_file", "modify_file")
            and item.get("root") == "config"
            and item.get("path") == self.settings_file.name
        ):
            self._remind_if_pending()

    async def _handle_update(self) -> None:
        try:
            values = self._read_settings()
        except FileNotFoundError:
            self._respond(f"U1_notify: {self.settings_file} not found")
            return
        except (ValueError, configparser.Error) as e:
            self._respond(f"U1_notify: {self.settings_file.name}: {e}")
            return
        try:
            self._write_secrets(values)
            self.flag_file.touch()
        except (OSError, configparser.Error) as e:
            logging.exception("u1_notify: could not write secrets")
            self._respond(f"U1_notify: could not save the settings: {e}")
            return
        self._respond(
            "U1_notify: settings saved; restarting Moonraker. A test "
            "message follows in about 20 seconds.")
        # Moonraker restarts itself in-process and reloads the notifiers
        # with the new secrets.
        self.server.restart(1.)

    async def _handle_klippy_ready(self) -> None:
        await self._subscribe()
        if self.flag_file.exists():
            self.flag_file.unlink()
            self.boot_checked = True
            await self._send("Notifications are set up", photo=False)
            return
        self._remind_if_pending()
        if self.boot_checked:
            return
        self.boot_checked = True
        if _uptime() < BOOT_WINDOW:
            await self._send("Printer on", "on", photo=False)

    async def _subscribe(self) -> None:
        kapis: KlippyAPI = self.server.lookup_component("klippy_apis")
        objects: Dict[str, Optional[list]] = {
            "print_stats": ["state", "filename"],
            "virtual_sdcard": ["progress"],
            "toolhead": ["extruder"],
        }
        available = await kapis.get_object_list(default=[])
        if "save_variables" in available:
            objects["save_variables"] = ["variables"]
        self.sensors = [
            SENSOR_FMT % i for i in range(len(EXTRUDERS))
            if SENSOR_FMT % i in available
        ]
        for sensor in self.sensors:
            objects[sensor] = ["enabled", "filament_detected"]
        if not self.sensors:
            logging.info("u1_notify: no filament motion sensors, runout off")
        status = await kapis.subscribe_objects(
            objects, self._handle_status, default=None)
        if status is None:
            logging.info("u1_notify: Klipper status subscription failed")
            return
        self.status = {k: dict(v) for k, v in status.items()}
        # A print already running (Moonraker restarted mid-print) carries on
        # from where it is, without repeating messages already sent.
        self.notified_percent = 0
        self.runout_sent = False
        if self._state() in ACTIVE_STATES:
            self.notified_percent = int(self._progress())
            self.runout_sent = self._runout()

    def _handle_status(self, status: Dict[str, Any], eventtime: float) -> None:
        for name, fields in status.items():
            self.status.setdefault(name, {}).update(fields)
        if not any(k in status for k in (
            "print_stats", "virtual_sdcard", "toolhead", *self.sensors
        )):
            return
        state = self._state()
        if state not in ACTIVE_STATES:
            self.notified_percent = 0
            self.runout_sent = False
            return
        runout = self._runout()
        if runout and not self.runout_sent:
            self.runout_sent = True
            self._spawn(self._send("Filament runout detected", "filament"))
        elif not runout:
            self.runout_sent = False
        step = _as_int(self._variable("notify_percent", 0))
        if step <= 0:
            return
        percent = self._progress()
        threshold = (self.notified_percent // step + 1) * step
        if threshold <= percent < 100 and percent > self.notified_percent:
            self.notified_percent = int(percent)
            self._spawn(self._send_progress(int(percent)))

    def _state(self) -> str:
        return self.status.get("print_stats", {}).get("state", "")

    def _progress(self) -> float:
        progress = self.status.get("virtual_sdcard", {}).get("progress", 0.)
        return round(float(progress or 0.) * 100., 1)

    def _extruder(self) -> str:
        name = self.status.get("toolhead", {}).get("extruder") or "extruder"
        return name if name in EXTRUDERS else "extruder"

    # Only the active toolhead's sensor counts; the idle ones don't move.
    def _runout(self) -> bool:
        sensor = self.status.get(
            SENSOR_FMT % EXTRUDERS.index(self._extruder()))
        if not sensor:
            return False
        return bool(sensor.get("enabled")) and \
            sensor.get("filament_detected") is False

    def _variable(self, name: str, default: Any) -> Any:
        variables = self.status.get("save_variables", {}).get("variables")
        if not isinstance(variables, dict):
            return default
        return variables.get(name, default)

    def _spawn(self, coro) -> None:
        self.server.get_event_loop().register_callback(lambda: coro)

    async def _send_progress(self, percent: int) -> None:
        kapis: KlippyAPI = self.server.lookup_component("klippy_apis")
        extruder = self._extruder()
        temps = await kapis.query_objects(
            {extruder: ["temperature", "target"],
             "heater_bed": ["temperature", "target"]}, default={})
        ext = temps.get(extruder, {})
        bed = temps.get("heater_bed", {})
        filename = self.status.get("print_stats", {}).get("filename") or ""
        basename = filename.replace("\\", "/").split("/")[-1] or "Unknown"
        msg = "%s (%d%%) | T%d %.1f/%.1f°C | %.1f/%.1f°C" % (
            basename, percent, EXTRUDERS.index(extruder),
            ext.get("temperature", 0.), ext.get("target", 0.),
            bed.get("temperature", 0.), bed.get("target", 0.))
        # The Discord embed already shows all of this, so it leaves the
        # text out.
        self.progress_message = msg
        await self._send(msg)

    # Same rules as the _NOTIFY macro: skipped while notify_<kind> is 0,
    # photo unless notify_photo is 0.
    async def _send(
        self, msg: str, kind: str = "ok", photo: bool = True
    ) -> None:
        if _as_int(self._variable(f"notify_{kind}", 1)) != 1:
            return
        if photo and _as_int(self._variable("notify_photo", 1)) == 1:
            name = "notifier_photo"
        else:
            name = "notifier"
        notifier: Optional[Notifier] = self.server.lookup_component(
            "notifier", None)
        if notifier is None:
            logging.info("u1_notify: Moonraker's notifier isn't loaded")
            return
        try:
            await notifier.notify_action(name, msg)
        except Exception:
            logging.exception(f"u1_notify: sending '{msg}' failed")

    # ---- Discord embeds ---------------------------------------------------

    async def component_init(self) -> None:
        if not self.discord_embeds:
            return
        notifier: Optional[Notifier] = self.server.lookup_component(
            "notifier", None)
        if notifier is None:
            return
        for name, inst in notifier.notifiers.items():
            match = DISCORD_RE.match(getattr(inst, "url", "") or "")
            if name not in OUR_NOTIFIERS or match is None:
                continue
            webhook = "https://discord.com/api/webhooks/%s/%s" % (
                match.group("id"), match.group("token"))
            # Moonraker calls inst.notify(event_name, event_args, message)
            # for both print events and _NOTIFY.
            inst.notify = functools.partial(
                self._notify_discord, inst, webhook, inst.notify)
            logging.info(f"u1_notify: [notifier {name}] uses Discord embeds")

    async def _notify_discord(
        self,
        inst: NotifierInstance,
        webhook: str,
        fallback: Callable,
        event_name: str,
        event_args: List,
        message: str = "",
    ) -> None:
        try:
            embed, photo = await self._build_embed(
                inst, event_name, event_args, message)
            await self._post_discord(webhook, embed, photo)
        except Exception:
            logging.exception(
                "u1_notify: Discord embed failed, sending plain text")
            await fallback(event_name, event_args, message)

    async def _build_embed(
        self,
        inst: NotifierInstance,
        event_name: str,
        event_args: List,
        message: str,
    ):
        kapis: KlippyAPI = self.server.lookup_component("klippy_apis")
        extruder = self._extruder()
        status = await kapis.query_objects({
            "print_stats": None,
            "display_status": ["progress"],
            "virtual_sdcard": ["progress"],
            "toolhead": ["extruder"],
            "extruder": ["temperature", "target"],
            "extruder1": ["temperature", "target"],
            "extruder2": ["temperature", "target"],
            "extruder3": ["temperature", "target"],
            "heater_bed": ["temperature", "target"],
            "gcode_move": ["gcode_position"],
        }, default={})
        extruder = status.get("toolhead", {}).get("extruder") or extruder
        stats = dict(status.get("print_stats", {}))
        # Print events carry print_stats as it was when the event fired.
        if len(event_args) > 1 and isinstance(event_args[1], dict):
            stats.update(event_args[1])
        if event_name == "complete":
            embed = self._complete_embed(stats)
        else:
            if message == self.progress_message:
                message = ""
            if event_name == "error" and not message:
                message = stats.get("message", "")
            embed = self._status_embed(
                event_name, stats, status, extruder, message)
        photo = await self._snapshot(inst, event_name, event_args, message)
        if photo is not None:
            embed["image"] = {"url": "attachment://snapshot.jpg"}
        return embed, photo

    # Right after boot the clock can be wrong until it syncs, so HTTPS fails
    # ("certificate is not yet valid"); Discord can also have a bad moment.
    # Both are retried for about a minute. Discord refusing the card (4xx)
    # isn't.
    async def _post_discord(
        self, webhook: str, embed: Dict[str, Any], photo: Optional[bytes]
    ) -> None:
        client: HttpClient = self.server.lookup_component("http_client")
        for delay in RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            # Set per attempt, so a retry after the clock syncs gets the
            # right time.
            embed["timestamp"] = datetime.datetime.now(
                datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if photo is None:
                body: Any = json.dumps({"embeds": [embed]})
                headers = {"Content-Type": "application/json"}
            else:
                body, headers = _multipart({"embeds": [embed]}, photo)
            resp = await client.request(
                "POST", webhook, body, headers, request_timeout=15.)
            if resp.status_code in (200, 204):
                return
            if resp.status_code < 500:
                break
            logging.info(
                f"u1_notify: Discord unreachable ({resp.status_code}), "
                "retrying")
        raise RuntimeError(
            f"Discord returned {resp.status_code}: {resp.text[:200]}")

    def _printer_name(self) -> str:
        name = str(self._read_secrets().get(SECTION, {}).get("name", ""))
        name = name.strip()
        if not name or "{" in name:
            return "Snapmaker U1"
        return name

    def _status_embed(
        self,
        event_name: str,
        stats: Dict[str, Any],
        status: Dict[str, Any],
        extruder: str,
        message: str,
    ) -> Dict[str, Any]:
        state = stats.get("state", "")
        label = EVENT_STATUS.get(event_name) or KLIPPER_STATUS.get(
            state, "Ready")
        fields: List[Dict[str, Any]] = [{
            "name": "Status",
            "value": STATUS_LABELS.get(label, label),
            "inline": True,
        }]
        tool = EXTRUDERS.index(extruder) if extruder in EXTRUDERS else 0
        for key, title in ((extruder, f"Extruder Temp (T{tool})"),
                           ("heater_bed", "Bed Temp")):
            heater = status.get(key)
            if heater:
                fields.append({
                    "name": title,
                    "value": "%.2f°C / %.2f°C" % (
                        heater.get("temperature", 0.),
                        heater.get("target", 0.)),
                    "inline": True,
                })
        if label in ("Printing", "Paused") and stats.get("filename"):
            fields.extend(self._job_fields(stats, status))
        embed: Dict[str, Any] = {
            "title": f"🖨️ {self._printer_name()}",
            "color": STATUS_COLORS.get(label, 0x95a5a6),
            "fields": fields,
        }
        if message:
            embed["description"] = message
        return embed

    def _job_fields(
        self, stats: Dict[str, Any], status: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        filename = stats.get("filename", "")
        meta = self._metadata(filename)
        # M73 progress from the slicer is time based; the file position is
        # the fallback.
        progress = float(
            status.get("display_status", {}).get("progress") or
            status.get("virtual_sdcard", {}).get("progress") or 0.)
        progress = min(max(progress, 0.), 1.)
        filled = int(progress * 100 // 10)
        fields = [{
            "name": "Progress",
            "value": "%s %d%%" % ("█" * filled + "░" * (10 - filled),
                                  round(progress * 100)),
            "inline": False,
        }]
        elapsed = float(stats.get("print_duration") or 0.)
        fields.append({
            "name": "Print Time",
            "value": _duration(elapsed),
            "inline": True,
        })
        estimate = float(meta.get("estimated_time") or 0.)
        remaining = None
        if estimate > 0:
            remaining = estimate * (1. - progress)
        elif progress > .01:
            remaining = elapsed / progress - elapsed
        if remaining is not None:
            # Discord shows <t:...> in each reader's own time zone, so the
            # printer's clock zone doesn't matter.
            eta = int(datetime.datetime.now(
                datetime.timezone.utc).timestamp() + remaining)
            fields.append({
                "name": "ETA",
                "value": f"<t:{eta}:t>",
                "inline": True,
            })
        layer = self._layer(stats, status, meta)
        if layer is not None:
            fields.append({
                "name": "Layer",
                "value": "%d / %d" % layer,
                "inline": True,
            })
        fields.append({"name": "File", "value": filename, "inline": False})
        return fields

    def _complete_embed(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        duration = float(stats.get("total_duration") or 0.)
        return {
            "title": "✅ Print Complete!",
            "color": 0x00ff00,
            "fields": [
                {"name": "File", "value": stats.get("filename") or "Unknown",
                 "inline": False},
                {"name": "Total Time",
                 "value": _duration(duration) if duration else "Unknown",
                 "inline": True},
            ],
            "footer": {"text": self._printer_name()},
        }

    def _metadata(self, filename: str) -> Dict[str, Any]:
        if not filename:
            return {}
        try:
            fm = self.server.lookup_component("file_manager")
            return fm.get_file_metadata(filename) or {}
        except Exception:
            return {}

    # Orca's U1 profile sends SET_PRINT_STATS_INFO, so print_stats.info
    # usually has the layer. Otherwise it's worked out from the nozzle
    # height and the file's layer height.
    def _layer(
        self,
        stats: Dict[str, Any],
        status: Dict[str, Any],
        meta: Dict[str, Any],
    ):
        info = stats.get("info") or {}
        if info.get("current_layer") and info.get("total_layer"):
            return int(info["current_layer"]), int(info["total_layer"])
        total = int(meta.get("layer_count") or 0)
        height = float(meta.get("layer_height") or 0.)
        first = float(meta.get("first_layer_height") or height)
        pos = status.get("gcode_move", {}).get("gcode_position") or []
        if total <= 0 or height <= 0. or len(pos) < 3:
            return None
        layer = int(round((float(pos[2]) - first) / height)) + 1
        return min(max(layer, 1), total), total

    async def _snapshot(
        self,
        inst: NotifierInstance,
        event_name: str,
        event_args: List,
        message: str,
    ) -> Optional[bytes]:
        if inst.attach is None:
            return None
        try:
            rendered = inst.attach.render({
                "event_name": event_name,
                "event_args": event_args,
                "event_message": message,
            })
        except Exception:
            return None
        urls = [u.strip() for u in rendered.splitlines()
                if re.match(r"https?://", u.strip())]
        if not urls:
            return None
        client: HttpClient = self.server.lookup_component("http_client")
        resp = await client.request(
            "GET", urls[0], request_timeout=5., enable_cache=False)
        if resp.has_error() or resp.status_code != 200 or not resp.content:
            logging.info(f"u1_notify: no snapshot from {urls[0]}")
            return None
        return resp.content


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return "%dh %dm" % (seconds // 3600, seconds % 3600 // 60)


def _multipart(payload: Dict[str, Any], photo: bytes):
    boundary = uuid.uuid4().hex
    body = b"".join([
        b"--%s\r\n" % boundary.encode(),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(payload).encode(), b"\r\n",
        b"--%s\r\n" % boundary.encode(),
        b'Content-Disposition: form-data; name="files[0]"; '
        b'filename="snapshot.jpg"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n",
        photo, b"\r\n",
        b"--%s--\r\n" % boundary.encode(),
    ])
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    return body, headers


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _uptime() -> float:
    try:
        return float(pathlib.Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return BOOT_WINDOW


def load_component(config: ConfigHelper) -> U1Notify:
    return U1Notify(config)
