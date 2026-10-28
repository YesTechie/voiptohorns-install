#!/usr/bin/env python3
"""VoIP to Horns dashboard (Flask): Talk zones, Protect horns, Hold To Talk, TTS."""
import asyncio
import json
import multiprocessing as mp
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from functools import wraps

import requests
import urllib3
from dotenv import load_dotenv

import dashboard_auth
import envfile
import protect_speaker
import protect_tts
from flask import Flask, Response, jsonify, redirect, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT_PATH = os.path.join(ROOT, "extensions.json")
BRIDGE_LOG = os.path.join(ROOT, "bridge_tcp.log")
BRIDGE_STATUS = os.path.join(ROOT, "bridge_status.json")  # written by voip_bridge_tcp.py
VERSION_PATH = os.path.join(ROOT, "VERSION")
SIP_REG_TTL = 90  # REGISTER Expires: 60, refresh ~45s
load_dotenv(os.path.join(ROOT, ".env"), interpolate=False)  # passwords may contain "$"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from talkback import (  # noqa: E402
    FFMPEG_PCM24_TO_ADTS,
    FFMPEG_WAV_TO_ADTS,
    FramePacer,
    TAIL_HOLD,
    open_talkback,
    play_file_child,
    read_adts_frames,
)
from netinfo import detect_my_ip  # noqa: E402



def app_version():
    try:
        v = open(VERSION_PATH).read().strip()
        if v:
            return v
    except Exception:
        pass
    return "0.0.0"

app = Flask(__name__, static_folder=os.path.join(HERE, "web"), static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def _flask_secret():
    path = os.path.join(HERE, ".flask_secret")
    if os.path.isfile(path):
        val = open(path).read().strip()
        if val:
            return val
    val = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(val)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return val


app.secret_key = _flask_secret()

STATE_PATH = os.path.join(HERE, "state.json")
STARTED = time.strftime("%Y-%m-%d %H:%M")
LOGS = ["dashboard start"]

# Fresh install: no zones. Zone maps live in state.json, written by the Zones page.
DEFAULT_SPEAKER_MAP = {}

# No site defaults. UNIFI_IP comes from .env (installer or Config page).
# SIP_SERVER defaults to UNIFI_IP. MY_IP is auto-detected toward the console if unset.
_ENV_UNIFI_IP = (os.getenv("UNIFI_IP") or "").strip()
_ENV_SIP_SERVER = (os.getenv("SIP_SERVER") or "").strip() or _ENV_UNIFI_IP

CONFIG = {
    "site_name": "",
    "my_ip": (os.getenv("MY_IP") or "").strip() or detect_my_ip(_ENV_SIP_SERVER),
    "unifi_host": ("https://" + _ENV_UNIFI_IP) if _ENV_UNIFI_IP else "",
    "unifi_username": os.getenv("UNIFI_USERNAME", ""),
    "unifi_password": os.getenv("UNIFI_PASSWORD", ""),
    "sip_server": _ENV_SIP_SERVER,
    "sip_port": os.getenv("SIP_PORT", "5060"),
    "rtp_port": os.getenv("RTP_PORT", "10000"),
}


def _norm_ext(ext):
    ext = str(ext or "").strip()
    if ext.isdigit():
        return ext.zfill(4)
    return ext


def log(msg):
    LOGS.append(time.strftime("%H:%M:%S") + " " + msg)
    if len(LOGS) > 400:
        del LOGS[: len(LOGS) - 400]


def load_state():
    state = {"maps": dict(DEFAULT_SPEAKER_MAP), "config": {}, "unifi_session": None, "unifi_cooldown_until": 0,
             "owned": None, "owned_exts": None}
    if os.path.exists(STATE_PATH):
        try:
            raw = json.loads(open(STATE_PATH).read())
            if isinstance(raw.get("maps"), dict):
                state["maps"] = raw["maps"]
            if isinstance(raw.get("owned"), dict):
                state["owned"] = {
                    _norm_ext(e): _owned_record(rec) for e, rec in raw["owned"].items() if _norm_ext(e)
                }
            if isinstance(raw.get("owned_exts"), list):
                state["owned_exts"] = sorted({_norm_ext(e) for e in raw["owned_exts"] if _norm_ext(e)})
            state["config"] = raw.get("config") or {}
            state["unifi_session"] = raw.get("unifi_session")
            state["unifi_cooldown_until"] = float(raw.get("unifi_cooldown_until") or 0)
        except Exception:
            pass
    return state


def _owned_record(rec):
    rec = rec if isinstance(rec, dict) else {}
    return {
        "talk_user_id": rec.get("talk_user_id") or None,
        "device_mac": rec.get("device_mac") or None,
        "created_at": rec.get("created_at") or None,
        "source": _neutral_source(rec.get("source")),
    }


def _neutral_source(src):
    """Record origin. Old records may say "3.3.1", "3.2.2" etc. (the list they came from)."""
    src = src or "created"
    return LEGACY_SOURCE if re.fullmatch(r"\d+(\.\d+)+", str(src)) else src


LEGACY_SOURCE = "legacy-list"  # bound from the previous version's owned_exts list


def _write_json_atomic(path, data, mode=0o600):
    """Temp file created with `mode` (never wider), fsync, rename over `path`."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state():
    os.makedirs(HERE, exist_ok=True)
    _write_json_atomic(STATE_PATH, {
        "maps": STATE["maps"],
        "owned": owned_map(),
        # Plain list kept for the bridge and for a rollback to the previous version (which reads only this key).
        "owned_exts": sorted(owned_exts()),
        "config": {k: CONFIG[k] for k in CONFIG if k not in CONSOLE_KEYS},
        "unifi_session": STATE.get("unifi_session"),
        "unifi_cooldown_until": STATE.get("unifi_cooldown_until") or 0,
    }, 0o600)


# Zones this install created, keyed by extension, identified by the Talk user ID and the
# third-party phone MAC recorded when the zone was created (state.json "owned"):
#   {"0025": {"talk_user_id": "...", "device_mac": "...", "created_at": 1761430000, "source": "created"}}
# A Talk user with the same number but another ID is someone else's zone. Other third-party
# phones in Talk (another bridge on the same console) are never listed, registered, changed
# or deleted from here. Records with talk_user_id None come from the previous version's list
# (or VTH_ADOPT_EXTS) and are bound
# once to the Talk user that has that number on the first console read (reconcile_owned).
FOREIGN_EXTS = set()  # owned numbers whose current Talk user/phone is not ours (last console read)


def owned_map():
    if not isinstance(STATE.get("owned"), dict):
        STATE["owned"] = {}
    return STATE["owned"]


def owned_exts():
    return set(owned_map().keys())


def owned_rec(ext):
    return owned_map().get(_norm_ext(ext))


def is_owned(ext):
    """Local check (no console call): recorded as ours and not seen as foreign on the last read."""
    ext = _norm_ext(ext)
    return bool(ext) and ext in owned_map() and ext not in FOREIGN_EXTS


def own_zone(ext, talk_user_id, device_mac=None, source="created"):
    owned_map()[_norm_ext(ext)] = {
        "talk_user_id": talk_user_id,
        "device_mac": device_mac,
        "created_at": int(time.time()),
        "source": source,
    }


def disown_ext(ext):
    owned_map().pop(_norm_ext(ext), None)
    FOREIGN_EXTS.discard(_norm_ext(ext))


def _not_owned(ext):
    return jsonify({
        "ok": False,
        "error": f"Zone {ext} was not created on this dashboard. It belongs to another device, so it cannot be changed here.",
    }), 403


def _third_party(devices):
    return [d for d in devices or () if d.get("model") == "THIRD_PARTY_OTHER"]


def reconcile_owned(users, devices):
    """Match owned records to Talk by user ID (+ phone MAC). Returns (verified, changed).

    verified: ext -> {"user_id", "device_mac"} for zones that are provably ours right now.
    changed: True when a number stopped (or started) being ours, so extensions.json must follow.
    """
    owned = owned_map()
    by_uid = {u.get("unique_id"): u for u in users if u.get("unique_id")}
    users_by_ext = {}
    for u in users:
        users_by_ext.setdefault(_norm_ext(u.get("ext")), []).append(u)
    dev_by_uid = {}
    for d in _third_party(devices):
        if d.get("user_id"):
            dev_by_uid.setdefault(d["user_id"], []).append(d)
    dirty = False
    changed = False
    # 1. Bind records that have no Talk user ID yet (previous version's list or VTH_ADOPT_EXTS), once.
    for ext in sorted(owned):
        rec = owned[ext]
        if rec.get("talk_user_id"):
            continue
        cands = users_by_ext.get(ext) or []
        if len(cands) != 1:
            why = "no Talk user has this number" if not cands else "more than one Talk user has this number"
            msg = "owned zone %s not bound (%s); no longer owned" % (ext, why)
            owned.pop(ext, None)
            dirty = changed = True
            log(msg)
            print(msg, flush=True)
            continue
        u = cands[0]
        devs = dev_by_uid.get(u.get("unique_id")) or []
        if rec.get("source") == "VTH_ADOPT_EXTS" and len(devs) != 1:
            owned.pop(ext, None)
            dirty = changed = True
            msg = "VTH_ADOPT_EXTS: %s is not a third-party phone in Talk; not adopted" % ext
            log(msg)
            print(msg, flush=True)
            continue
        rec["talk_user_id"] = u.get("unique_id")
        rec["device_mac"] = devs[0].get("mac") if len(devs) == 1 else None
        dirty = True
        msg = "owned zone %s bound to its Talk user ID and phone (%s)" % (
            ext, "VTH_ADOPT_EXTS" if rec.get("source") == "VTH_ADOPT_EXTS" else "previous version's list")
        log(msg)
        print(msg, flush=True)
    # 2. Follow each record by its Talk user ID.
    verified = {}
    foreign = set()
    for ext in sorted(owned):
        rec = owned.get(ext)
        if rec is None:
            continue
        u = by_uid.get(rec["talk_user_id"])
        if not u:
            owned.pop(ext, None)
            dirty = changed = True
            msg = "owned zone %s: its Talk user was deleted outside this dashboard; no longer owned" % ext
            log(msg)
            print(msg, flush=True)
            continue
        cur = _norm_ext(u.get("ext"))
        if cur and cur != ext:
            if cur in owned:
                foreign.add(ext)
                log("owned zone %s: Talk user now has %s, which is already recorded here; skipped" % (ext, cur))
                continue
            owned[cur] = owned.pop(ext)
            if ext in STATE["maps"]:
                STATE["maps"][cur] = STATE["maps"].pop(ext)
            log("owned zone %s was renumbered in Talk to %s (same Talk user)" % (ext, cur))
            dirty = changed = True
            ext = cur
            rec = owned[ext]
        devs = dev_by_uid.get(rec["talk_user_id"]) or []
        if rec.get("device_mac"):
            dev = next((d for d in devs if d.get("mac") == rec["device_mac"]), None)
            if dev is None:
                foreign.add(ext)
                log("owned zone %s: its phone %s is no longer in Talk; not used" % (ext, rec["device_mac"]))
                continue
        else:
            dev = devs[0] if len(devs) == 1 else None
            if dev is not None:
                rec["device_mac"] = dev.get("mac")
                dirty = True
        verified[ext] = {"user_id": rec["talk_user_id"], "device_mac": (dev or {}).get("mac")}
    if foreign != FOREIGN_EXTS:
        changed = changed or bool(foreign - FOREIGN_EXTS)
        FOREIGN_EXTS.clear()
        FOREIGN_EXTS.update(foreign)
    if dirty:
        save_state()
    return verified, changed


# The console address and account live only in .env (mode 600), never in state.json.
CONSOLE_KEYS = ("unifi_host", "unifi_username", "unifi_password")

STATE = load_state()
_legacy_secret = "unifi_password" in (STATE.get("config") or {})
if STATE.get("config"):
    for k, v in STATE["config"].items():
        if k in CONFIG and k not in CONSOLE_KEYS and v not in (None, ""):
            CONFIG[k] = v


def _adopt_list():
    """VTH_ADOPT_EXTS from .env or the environment, e.g. "0101,0102"."""
    raw = (os.getenv("VTH_ADOPT_EXTS") or "").strip()
    out = []
    for e in raw.split(","):
        e = e.strip()
        if re.fullmatch(r"\d{3,4}", e):
            out.append(e.zfill(4))
        elif e:
            log("VTH_ADOPT_EXTS: ignored %r (not a 3-4 digit extension)" % e)
    return sorted(set(out))


if STATE.get("owned") is None:
    # Upgrade from a version without "owned". Nothing is adopted that this install cannot prove it created.
    if STATE.get("owned_exts") is not None:
        # The previous version kept the numbers it created; bind each to its current Talk user ID
        # on the first console read (reconcile_owned), once.
        STATE["owned"] = {e: _owned_record({"source": LEGACY_SOURCE}) for e in STATE["owned_exts"]}
        _msg = "owned zones from the previous version's list, to bind on first console read: %s" % (
            ", ".join(sorted(STATE["owned"])) or "none")
    elif _adopt_list():
        # No record at all (older installs). Only an explicit list (VTH_ADOPT_EXTS) is adopted;
        # each number must exist in Talk as a third-party phone, checked on the first console read.
        STATE["owned"] = {e: _owned_record({"source": "VTH_ADOPT_EXTS"}) for e in _adopt_list()}
        _msg = "owned zones from VTH_ADOPT_EXTS, to check in Talk on first console read: %s" % (
            ", ".join(sorted(STATE["owned"])))
    else:
        # No record of what the previous version created and no VTH_ADOPT_EXTS: adopt nothing.
        STATE["owned"] = {}
        _msg = "no ownership record and no VTH_ADOPT_EXTS: no zones adopted"
    log(_msg)
    print(_msg, flush=True)
    save_state()
elif _legacy_secret:
    save_state()  # rewrite state.json without the old copy of the console password
try:
    if os.path.isfile(STATE_PATH):
        os.chmod(STATE_PATH, 0o600)  # it holds the console session cookies
except OSError:
    pass


def _session_ok():
    user = session.get("user")
    if not user or not dashboard_auth.is_configured():
        return False
    if not dashboard_auth.same_user(user, dashboard_auth.configured_user()):
        session.clear()
        return False
    return True


def login_required(f):
    @wraps(f)
    def inner(*a, **k):
        if not _session_ok():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Not signed in"}), 401
            return redirect(url_for("login_page"))
        return f(*a, **k)

    return inner


def host_only(url):
    return re.sub(r"^https?://", "", (url or "").strip()).split("/")[0]


class Console:
    def __init__(self, host, username, password):
        self.host = host_only(host)
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers["Accept"] = "application/json"

    def login(self):
        r = self.s.post(
            f"https://{self.host}/api/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=15,
        )
        if r.status_code == 429:
            raise RuntimeError("UniFi login rate-limited (429) — wait a few seconds and refresh")
        if r.status_code != 200:
            raise RuntimeError(f"UniFi login failed ({r.status_code})")
        csrf = r.headers.get("X-CSRF-Token") or r.headers.get("x-csrf-token")
        if csrf:
            self.s.headers["X-CSRF-Token"] = csrf
        self.s.headers["Origin"] = f"https://{self.host}"
        self.s.headers["Referer"] = f"https://{self.host}/proxy/talk/"
        return True

    def apply_saved(self, blob):
        if not blob:
            return False
        if blob.get("host") != self.host or blob.get("username") != self.username:
            return False
        csrf = blob.get("csrf")
        if csrf:
            self.s.headers["X-CSRF-Token"] = csrf
        self.s.headers["Origin"] = f"https://{self.host}"
        self.s.headers["Referer"] = f"https://{self.host}/proxy/talk/"
        for k, v in (blob.get("cookies") or {}).items():
            self.s.cookies.set(k, v)
        return True

    def export_saved(self):
        return {
            "host": self.host,
            "username": self.username,
            "csrf": self.s.headers.get("X-CSRF-Token"),
            "cookies": requests.utils.dict_from_cookiejar(self.s.cookies),
        }

    def alive(self):
        try:
            r = self.get("/proxy/talk/api/info", timeout=8)
            return r.status_code == 200
        except Exception:
            return False

    def get(self, path, **kw):
        return self.s.get(f"https://{self.host}{path}", timeout=kw.pop("timeout", 15), **kw)

    def req(self, method, path, **kw):
        return self.s.request(method, f"https://{self.host}{path}", timeout=kw.pop("timeout", 20), **kw)

    def speakers(self):
        r = self.get("/proxy/protect/api/speakers")
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("data") or data.get("speakers") or []
        out = []
        for sp in items:
            sid = sp.get("id") or sp.get("_id")
            name = sp.get("name") or sid
            typ = sp.get("type") or sp.get("model") or sp.get("marketName") or ""
            state = sp.get("state") or sp.get("connectionState") or ""
            delay = 220 if "horn" not in typ.lower() and "speaker" in typ.lower() else 0
            out.append({
                "id": sid,
                "name": name,
                "type": typ,
                "state": state,
                "online": str(state).upper() == "CONNECTED",
                "default_delay": delay,
            })
        return out

    def users(self):
        r = self.get("/proxy/talk/api/users")
        r.raise_for_status()
        return r.json()

    def devices(self):
        r = self.get("/proxy/talk/api/devices")
        r.raise_for_status()
        return r.json()

    def create_user(self, name, ext=None):
        body = {"first_name": name, "last_name": ""}
        if ext:
            body["ext"] = ext
        r = self.req("POST", "/proxy/talk/api/user", json=body)
        if r.status_code != 200:
            raise RuntimeError(r.text[:300] or f"create user {r.status_code}")
        uid = (r.json() or {}).get("created_user_id")
        if not uid:
            raise RuntimeError("Talk did not return created_user_id")
        return uid

    def update_user(self, uid, name=None, ext=None):
        u = next((x for x in self.users() if x.get("unique_id") == uid), None)
        if not u:
            raise RuntimeError("Talk user not found")
        if name is not None:
            first, last = name, ""
        else:
            first, last = u.get("first_name") or "", u.get("last_name") or ""
        body = {
            "first_name": first,
            "last_name": last,
            "ext": ext if ext is not None else u.get("ext"),
        }
        r = self.req("PUT", f"/proxy/talk/api/user/{uid}", json=body)
        if r.status_code != 200:
            raise RuntimeError(r.text[:300] or f"update user {r.status_code}")
        return True

    def create_device(self, name, user_uuid):
        r = self.req(
            "PUT",
            "/proxy/talk/api/device/third_party",
            json={"device_name": name, "user_uuid": user_uuid},
        )
        if r.status_code != 200:
            raise RuntimeError(r.text[:300] or f"create device {r.status_code}")
        return (r.json() or {}).get("device_id")

    def delete_device(self, mac):
        r = self.req("DELETE", f"/proxy/talk/api/device/{mac}")
        if r.status_code not in (200, 204):
            raise RuntimeError(r.text[:300] or f"delete device {r.status_code}")

    def delete_user(self, uid):
        r = self.req("DELETE", f"/proxy/talk/api/user/{uid}")
        if r.status_code not in (200, 204):
            raise RuntimeError(r.text[:300] or f"delete user {r.status_code}")


_console = None
_console_key = None
_console_lock = threading.Lock()
LOGIN_COOLDOWN = 900
_cooldown_until = float(STATE.get("unifi_cooldown_until") or 0)


def _remember(c, key):
    global _console, _console_key, _cooldown_until
    _console = c
    _console_key = key
    _cooldown_until = 0
    STATE["unifi_session"] = c.export_saved()
    STATE["unifi_cooldown_until"] = 0
    save_state()


def _cooldown_left():
    return max(0, int(_cooldown_until - time.time()))


def console(allow_login=False):
    global _console, _console_key, _cooldown_until
    if not CONFIG.get("unifi_username") or not CONFIG.get("unifi_password"):
        raise RuntimeError("UniFi username and password are required in Config")
    key = (host_only(CONFIG["unifi_host"]), CONFIG["unifi_username"], CONFIG["unifi_password"])
    with _console_lock:
        if _console is not None and _console_key == key:
            if _console.alive():
                return _console
            _console = None
            STATE["unifi_session"] = None
        c = Console(key[0], key[1], key[2])
        if c.apply_saved(STATE.get("unifi_session")) and c.alive():
            _remember(c, key)
            log("unifi session restored")
            return c
        wait = _cooldown_left()
        if wait > 0:
            raise RuntimeError(
                f"UniFi login locked for {wait}s. Do not click Refresh yet."
            )
        if not allow_login:
            raise RuntimeError("Not connected to UniFi. Click Refresh From Console once.")
        try:
            c.login()
            _remember(c, key)
            log("unifi login ok")
            return c
        except Exception as e:
            if "429" in str(e):
                _cooldown_until = time.time() + LOGIN_COOLDOWN
                STATE["unifi_cooldown_until"] = _cooldown_until
                save_state()
                log(f"unifi 429 — cooling {LOGIN_COOLDOWN}s, no more logins")
                raise RuntimeError(
                    f"UniFi login locked for {LOGIN_COOLDOWN // 60} min. Wait, then click Refresh From Console once."
                )
            raise


def used_exts(users, devices=()):
    """Every extension on the console (all Talk users and devices, owned or not)."""
    out = set()
    for u in list(users) + list(devices or ()):
        e = str(u.get("ext") or "")
        if e.isdigit():
            out.add(e)
    return out


def next_free_ext(users, devices=()):
    used = {int(e) for e in used_exts(users, devices)}
    n = 20
    while n in used:
        n += 1
    return f"{n:04d}"


def zone_name_from_user(u):
    full = (u.get("full_name") or "").strip()
    if full:
        return full
    return ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip() or "Zone"


def load_extensions_file():
    if not os.path.isfile(EXT_PATH):
        return {}
    try:
        data = json.loads(open(EXT_PATH).read())
        return data.get("extensions") or {}
    except Exception:
        return {}


def configured_ext_names():
    names = {}
    for ext, rec in load_extensions_file().items():
        key = str(ext).zfill(4) if str(ext).isdigit() else str(ext)
        if is_owned(key):
            names[key] = (rec or {}).get("name") or key
    for ext in owned_exts():
        names.setdefault(ext, ext)
    return names


def render_ui(filename):
    path = os.path.join(app.static_folder, filename)
    with open(path, encoding="utf-8") as f:
        html = f.read()
    return Response(html, mimetype="text/html")


def local_sip_regs():
    """ext -> True if THIS Pi's bridge registered the ext within SIP_REG_TTL.

    Source: bridge_status.json (written by voip_bridge_tcp.py after each 200 OK to REGISTER).
    Falls back to this Pi's bridge log only when the status file does not exist yet.
    Talk's own registration flag is never used: it is true when any device registered the ext.
    """
    out = {}
    if os.path.isfile(BRIDGE_STATUS):
        try:
            data = json.loads(open(BRIDGE_STATUS).read())
            now = time.time()
            for ext, rec in (data.get("exts") or {}).items():
                try:
                    out[_norm_ext(ext)] = (now - float((rec or {}).get("last_ok") or 0)) <= SIP_REG_TTL
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass
        return out
    if not os.path.isfile(BRIDGE_LOG):
        return out
    try:
        with open(BRIDGE_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 128 * 1024))
            text = f.read().decode("utf-8", "replace")
        last = {}
        for line in text.splitlines():
            m = re.search(
                r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*VoIP \[(\d+)\]: Registration SUCCESSFUL",
                line,
            )
            if not m:
                continue
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            last[m.group(2).zfill(4)] = ts
        now = time.time()
        for ext, ts in last.items():
            out[ext] = (now - ts) <= SIP_REG_TTL
    except Exception:
        pass
    return out


def build_inventory(c):
    speakers = []
    try:
        speakers = c.speakers()
    except Exception as e:
        log(f"protect speakers failed: {e}")
    users = c.users()
    devices = c.devices()
    by_id = {u.get("unique_id"): u for u in users}
    verified, changed = reconcile_owned(users, devices)
    mine = {(v["user_id"], v.get("device_mac")): ext for ext, v in verified.items()}
    local = local_sip_regs()
    zones = []
    for d in _third_party(devices):
        uid = d.get("user_id")
        u = by_id.get(uid) if uid else None
        ext = mine.get((uid, d.get("mac"))) if uid else None
        if not ext:
            continue  # not created here (other user ID or phone): hidden, never touched
        name = zone_name_from_user(u) if u else (d.get("display_name") or "Unassigned")
        mp = STATE["maps"].get(ext) or {}
        has_horns = bool(mp.get("speaker_ids"))
        sip_ok = has_horns and bool(local.get(ext))  # this Pi's bridge only, never Talk's flag
        status = "online" if sip_ok else ("no_horns" if not has_horns else "not_registered")
        zones.append({
            "ext": ext,
            "name": name,
            "user_id": uid,
            "device_mac": d.get("mac"),
            "device_name": d.get("display_name"),
            "status": status,
            "sip_reg": sip_ok,
            "assigned": bool(uid),
            "speaker_ids": list(mp.get("speaker_ids") or []),
            "delays": dict(mp.get("delays") or {}),
            "sip_password_set": bool(u and u.get("sip_password")),
            "has_horns": has_horns,
        })
    zones.sort(key=lambda z: z.get("ext") or "9999")
    return {
        "_owned_changed": changed,
        "ok": True,
        "speakers": speakers,
        "zones": zones,
        "next_ext": next_free_ext(users, devices),
        "used_exts": sorted(used_exts(users, devices)),
    }


def sync_bridge():
    """Write extensions.json from Talk + horn maps and restart the SIP registrar."""
    c = console(allow_login=False)
    by_uid = {u.get("unique_id"): u for u in c.users()}
    inv = build_inventory(c)  # zones matched to this install by Talk user ID + phone MAC
    extensions = {}
    delays = {}
    for z in inv["zones"]:
        ext = z.get("ext")
        if not ext or not z.get("assigned") or not is_owned(ext):
            continue
        speakers = list(z.get("speaker_ids") or [])
        if not speakers:
            log(f"bridge sync skip {ext}: no horns (not registered, so calls are not swallowed)")
            continue
        pw = (by_uid.get(z.get("user_id")) or {}).get("sip_password") or ""
        if not pw:
            log(f"bridge sync skip {ext}: no sip password")
            continue
        extensions[ext] = {
            "name": z.get("name") or ext,
            "password": pw,
            "speakers": speakers,
        }
        for sid, ms in (z.get("delays") or {}).items():
            try:
                sec = float(ms) / 1000.0
            except (TypeError, ValueError):
                continue
            if sec > 0:
                delays[str(sid)] = sec
    _write_json_atomic(EXT_PATH, {"extensions": extensions, "delays": delays}, 0o600)
    log(f"bridge sync wrote {len(extensions)} extensions")
    subprocess.check_call(
        ["sudo", "-n", "systemctl", "restart", "voiptohorns-bridge.service"],
        timeout=20,
    )
    log("bridge restarted")


def inventory_for_api(c):
    """build_inventory for read endpoints. If a number stopped being ours, resync the bridge."""
    inv = build_inventory(c)
    if inv.pop("_owned_changed", False):
        try:
            sync_bridge()
        except Exception as e:
            log(f"bridge resync after ownership change failed: {e}")
    return inv


def prune_extensions_file():
    """Drop extensions this install did not create from extensions.json (startup)."""
    if not os.path.isfile(EXT_PATH):
        return
    try:
        data = json.loads(open(EXT_PATH).read())
    except Exception:
        return
    exts = data.get("extensions") or {}
    foreign = sorted(e for e in exts if not is_owned(e))
    if not foreign:
        return
    data["extensions"] = {e: rec for e, rec in exts.items() if is_owned(e)}
    _write_json_atomic(EXT_PATH, data, 0o600)
    msg = "extensions.json: dropped zones not created here: %s" % ", ".join(foreign)
    log(msg)
    print(msg, flush=True)


prune_extensions_file()
try:
    if os.path.isfile(EXT_PATH):
        os.chmod(EXT_PATH, 0o600)  # holds SIP passwords
except OSError:
    pass


CERT_CRT = "/etc/nginx/ssl/vth.crt"
CERT_MOBILE = "/etc/nginx/ssl/vth.mobileconfig"


@app.route("/ca.crt")
def ca_crt():
    if not os.path.isfile(CERT_CRT):
        return "Certificate not installed on this Pi", 404
    return send_file(
        CERT_CRT,
        mimetype="application/x-x509-ca-cert",
        as_attachment=True,
        download_name="yestechie-vth.crt",
    )


@app.route("/ca.mobileconfig")
def ca_mobileconfig():
    if not os.path.isfile(CERT_MOBILE):
        return "Profile not installed on this Pi", 404
    return send_file(
        CERT_MOBILE,
        mimetype="application/x-apple-aspen-config",
        as_attachment=True,
        download_name="yestechie-vth.mobileconfig",
    )


@app.route("/api/version")
def api_version():
    return jsonify({"ok": True, "version": app_version()})


LOGIN_NOT_CONFIGURED = (
    "Dashboard login is not configured. Set DASHBOARD_USER and DASHBOARD_PASS_HASH in .env "
    "(make the hash with: dashboard/dashboard_auth.py hash-password), then restart voiptohorns-dashboard."
)


@app.route("/login", methods=["GET"])
def login_page():
    if not dashboard_auth.is_configured():
        return Response(LOGIN_NOT_CONFIGURED + "\n", status=503, mimetype="text/plain")
    if _session_ok():
        return redirect(url_for("dashboard"))
    return render_ui("login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    data = request.get_json(silent=True) or {}
    user = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    if not dashboard_auth.is_configured():
        log("login refused: dashboard login not configured")
        return jsonify({"ok": False, "error": LOGIN_NOT_CONFIGURED}), 503
    if dashboard_auth.verify(user, password):
        session["user"] = user
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def dashboard():
    return render_ui("index.html")


def bridge_status():
    """OS PID of voip_bridge_tcp.py, if it is running. Not the UI process."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "voip_bridge_tcp.py"],
            text=True,
            timeout=2,
        ).strip()
        pids = [p for p in out.split() if p.isdigit()]
        if pids:
            pid = int(pids[0])
            since = None
            try:
                st = subprocess.check_output(
                    ["ps", "-o", "lstart=", "-p", str(pid)],
                    text=True,
                    timeout=2,
                ).strip()
                since = st or None
            except Exception:
                pass
            return {"state": "active", "pid": pid, "since": since}
    except subprocess.CalledProcessError:
        pass
    except Exception:
        pass
    return {"state": "stopped", "pid": None, "since": None}


@app.route("/api/status")
@login_required
def api_status():
    local = local_sip_regs()
    names = configured_ext_names()
    sip = []
    nvr_ok = False
    nvr_err = None
    try:
        inv = inventory_for_api(console(allow_login=True))
        nvr_ok = True
        for z in inv["zones"]:
            ext = z["ext"]
            sip.append({
                "ext": ext,
                "name": z["name"],
                "ok": bool(z["sip_reg"]),
            })
            if ext:
                names[ext] = z["name"]
    except Exception as e:
        nvr_err = str(e)
    seen = {row["ext"] for row in sip}
    for ext, name in sorted(names.items()):
        if ext not in seen:
            sip.append({"ext": ext, "name": name, "ok": bool(local.get(ext))})
    sip.sort(key=lambda row: row.get("ext") or "9999")
    br = bridge_status()
    return jsonify({
        "state": br["state"],
        "pid": br["pid"],
        "since": br["since"],
        "call": "Idle",
        "site_name": CONFIG["site_name"],
        "sip": sip,
        "nvr_ok": nvr_ok,
        "nvr_error": nvr_err,
        "sys": host_stats(),
        "version": app_version(),
    })


@app.route("/api/health")
@login_required
def api_health():
    try:
        c = console(allow_login=True)
        sp = c.speakers()
        return jsonify({
            "nvr": {"ok": True, "host": CONFIG["unifi_host"], "speakers": len(sp)},
            "talkback": {"ok": True},
        })
    except Exception as e:
        return jsonify({
            "nvr": {"ok": False, "host": CONFIG["unifi_host"], "error": str(e)},
            "talkback": {"ok": False},
        })


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        old = (host_only(CONFIG["unifi_host"]), CONFIG["unifi_username"], CONFIG["unifi_password"])
        for k in CONFIG:
            if k in data:
                if k == "unifi_password" and not data[k]:
                    continue  # blank field = keep the saved password
                CONFIG[k] = data[k]
        new = (host_only(CONFIG["unifi_host"]), CONFIG["unifi_username"], CONFIG["unifi_password"])
        if new != old:
            global _console, _console_key
            _console = None
            _console_key = None
            STATE["unifi_session"] = None
        save_state()
        write_dotenv()
        log("config saved")
        try:
            subprocess.check_call(
                ["sudo", "-n", "systemctl", "restart", "voiptohorns-bridge.service"],
                timeout=20,
            )
        except Exception as e:
            log(f"bridge restart after config: {e}")
        return jsonify({"ok": True})
    out = dict(CONFIG)
    out["unifi_password"] = ""  # never sent to the browser
    out["has_password"] = bool(CONFIG["unifi_password"])
    return jsonify(out)


@app.route("/api/test-unifi", methods=["POST"])
@login_required
def api_test_unifi():
    data = request.get_json(silent=True) or {}
    host = host_only(data.get("host") or CONFIG["unifi_host"])
    user = data.get("username") or CONFIG["unifi_username"]
    pw = data.get("password") or CONFIG["unifi_password"]
    saved = (host_only(CONFIG["unifi_host"]), CONFIG["unifi_username"], CONFIG["unifi_password"])
    try:
        if (host, user, pw) == saved:
            c = console(allow_login=True)  # saved settings: same path as before (session reuse, 429 cooldown)
        else:
            if not user or not pw:
                raise RuntimeError("UniFi username and password are required in Config")
            c = Console(host, user, pw)  # unsaved values: test only, not kept until Save Configuration
            c.login()
        sp = c.speakers()
        users = c.users()
        log(f"unifi connected host={c.host} speakers={len(sp)} users={len(users)}")
        return jsonify({"ok": True, "speakers": len(sp), "users": len(users)})
    except Exception as e:
        log(f"unifi connect failed: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/unifi/connect", methods=["POST"])
@login_required
def api_unifi_connect():
    try:
        c = console(allow_login=True)
        inv = inventory_for_api(c)
        log(f"unifi connected zones={len(inv['zones'])} speakers={len(inv['speakers'])}")
        return jsonify(inv)
    except Exception as e:
        log(f"unifi connect failed: {e}")
        return jsonify({"ok": False, "error": str(e), "speakers": [], "zones": [], "next_ext": "0020", "used_exts": []})


@app.route("/api/inventory")
@login_required
def api_inventory():
    try:
        inv = inventory_for_api(console(allow_login=True))
        return jsonify(inv)
    except Exception as e:
        return jsonify({
            "ok": False,
            "need_connect": True,
            "error": str(e),
            "speakers": [],
            "zones": [],
            "next_ext": "0020",
            "used_exts": [],
        })


def apply_mapped_speaker_talk(client, speaker_ids):
    """Talk mode for speakers just saved onto a zone. Never raises."""
    try:
        return protect_speaker.set_talk_many(client, speaker_ids, log=log)
    except Exception as e:
        msg = "Could not set Protect speaker talk mode: %s" % e
        log(msg)
        return {"ok": False, "results": [], "warnings": [msg]}


def _zone_saved(payload, talk):
    if talk is None:
        return jsonify(payload)
    body = dict(payload)
    body["speaker_talk"] = list(talk.get("results") or [])
    warnings = list(talk.get("warnings") or [])
    if warnings:
        body["warnings"] = warnings
    return jsonify(body)


@app.route("/api/zones", methods=["POST"])
@login_required
def api_zones_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    ext = (data.get("ext") or "").strip()
    speaker_ids = data.get("speaker_ids") or []
    delays = data.get("delays") or {}
    if not name:
        return jsonify({"ok": False, "error": "Zone name is required"}), 400
    if ext and not re.fullmatch(r"\d{3,4}", ext):
        return jsonify({"ok": False, "error": "Extension must be 3–4 digits"}), 400
    if ext:
        ext = ext.zfill(4)
    try:
        c = console(allow_login=False)
        users = c.users()
        taken = used_exts(users, c.devices())
        if ext and ext in taken:
            return jsonify({"ok": False, "error": f"Extension {ext} is already in use"}), 400
        uid = c.create_user(name, ext)
        try:
            u = next((x for x in c.users() if x.get("unique_id") == uid), None)
            auto_ext = str((u or {}).get("ext") or "")
            if ext and auto_ext != ext:
                c.update_user(uid, name=name, ext=ext)
            else:
                ext = auto_ext
            if not ext:
                raise RuntimeError("Talk did not assign an extension")
            own_zone(ext, uid)  # recorded by Talk user ID right away
            save_state()
            mac = c.create_device(name, uid)
            dev = next((d for d in _third_party(c.devices()) if d.get("user_id") == uid), None)
            mac = (dev or {}).get("mac") or mac
            if not mac:
                raise RuntimeError("Talk did not return the new phone")
            owned_map()[ext]["device_mac"] = mac
        except Exception as e:
            # Undo only what this request created: the Talk user with this exact ID.
            log(f"zone create failed after the Talk user was made: {e}; removing that user")
            if ext and (owned_rec(ext) or {}).get("talk_user_id") == uid:
                disown_ext(ext)
                save_state()
            try:
                c.delete_user(uid)
                log("zone create rolled back (Talk user removed)")
            except Exception as e2:
                log(f"zone create rollback could not remove the Talk user: {e2}")
            raise RuntimeError(f"Zone create failed and was undone: {e}")
        STATE["maps"][ext] = {"speaker_ids": list(speaker_ids), "delays": dict(delays)}
        talk = apply_mapped_speaker_talk(c, speaker_ids)
        save_state()
        log(f"zone created {ext} {name} device={mac}")
        sync_bridge()
        return _zone_saved(
            {"ok": True, "ext": ext, "user_id": uid, "device_mac": mac},
            talk,
        )
    except Exception as e:
        log(f"zone create failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/zones/<ext>", methods=["PATCH"])
@login_required
def api_zones_update(ext):
    ext = (ext or "").zfill(4)
    data = request.get_json(silent=True) or {}
    if not is_owned(ext):
        log(f"zone update refused {ext}: not created here")
        return _not_owned(ext)
    try:
        c = console(allow_login=False)
        inv = build_inventory(c)
        zone = next((z for z in inv["zones"] if z["ext"] == ext), None)
        if not zone and (not is_owned(ext) or ext in inv["used_exts"]):
            log(f"zone update refused {ext}: Talk user/phone is not the one created here")
            return _not_owned(ext)
        if not zone or not zone.get("user_id"):
            return jsonify({"ok": False, "error": "Zone not found"}), 404
        uid = zone["user_id"]
        new_name = data.get("name")
        new_ext = data.get("ext")
        if new_ext:
            new_ext = str(new_ext).zfill(4)
            if not re.fullmatch(r"\d{4}", new_ext):
                return jsonify({"ok": False, "error": "Extension must be 4 digits"}), 400
        if new_ext and new_ext != ext:
            if new_ext in set(inv["used_exts"]) - {ext}:
                return jsonify({"ok": False, "error": f"Extension {new_ext} is already in use"}), 400
            c.update_user(uid, name=new_name if new_name else zone["name"], ext=new_ext)
            if ext in STATE["maps"]:
                STATE["maps"][new_ext] = STATE["maps"].pop(ext)
            owned_map()[new_ext] = owned_map().pop(ext)  # same Talk user ID and phone
            log(f"zone {ext} ext -> {new_ext}")
            ext = new_ext
        elif new_name and new_name != zone["name"]:
            c.update_user(uid, name=new_name, ext=ext)
            log(f"zone {ext} renamed {new_name}")
        talk = None
        if "speaker_ids" in data:
            mp = STATE["maps"].setdefault(ext, {"speaker_ids": [], "delays": {}})
            mp["speaker_ids"] = list(data.get("speaker_ids") or [])
            if "delays" in data:
                mp["delays"] = dict(data.get("delays") or {})
            log(f"zone {ext} speakers saved")
            talk = apply_mapped_speaker_talk(c, mp["speaker_ids"])
        elif "delays" in data:
            mp = STATE["maps"].setdefault(ext, {"speaker_ids": [], "delays": {}})
            mp["delays"] = dict(data.get("delays") or {})
        save_state()
        sync_bridge()
        return _zone_saved({"ok": True, "ext": ext}, talk)
    except Exception as e:
        log(f"zone update failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/zones/<ext>", methods=["DELETE"])
@login_required
def api_zones_delete(ext):
    ext = (ext or "").zfill(4)
    if not is_owned(ext):
        log(f"zone delete refused {ext}: not created here")
        return _not_owned(ext)
    try:
        c = console(allow_login=False)
        inv = build_inventory(c)
        zone = next((z for z in inv["zones"] if z["ext"] == ext), None)
        if not zone and (not is_owned(ext) or ext in inv["used_exts"]):
            log(f"zone delete refused {ext}: Talk user/phone is not the one created here")
            return _not_owned(ext)
        if not zone:
            return jsonify({"ok": False, "error": "Zone not found"}), 404
        if zone.get("device_mac"):
            c.delete_device(zone["device_mac"])
        if zone.get("user_id"):
            try:
                c.delete_user(zone["user_id"])
            except Exception as e:
                log(f"zone {ext} user delete: {e}")
        STATE["maps"].pop(ext, None)
        disown_ext(ext)
        save_state()
        log(f"zone deleted {ext}")
        sync_bridge()
        return jsonify({"ok": True})
    except Exception as e:
        log(f"zone delete failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 400


def host_stats():
    sysd = {
        "cpu_temp": None,
        "cpu_load": None,
        "mem_used_mb": None,
        "mem_total_mb": None,
        "mem_pct": 0,
        "disk_used_gb": None,
        "disk_total_gb": None,
        "disk_pct": 0,
        "uptime": None,
    }
    try:
        sysd["cpu_temp"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000.0, 1)
    except Exception:
        pass
    try:
        sysd["cpu_load"] = round(os.getloadavg()[0], 2)
    except Exception:
        pass
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])
        total = info.get("MemTotal") or 0
        avail = info.get("MemAvailable") or info.get("MemFree") or 0
        used = max(total - avail, 0)
        sysd["mem_total_mb"] = round(total / 1024)
        sysd["mem_used_mb"] = round(used / 1024)
        sysd["mem_pct"] = int(round(100.0 * used / total)) if total else 0
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        sysd["disk_total_gb"] = round(total / 1e9, 1)
        sysd["disk_used_gb"] = round(used / 1e9, 1)
        sysd["disk_pct"] = int(round(100.0 * used / total)) if total else 0
    except Exception:
        pass
    try:
        secs = int(float(open("/proc/uptime").read().split()[0]))
        sysd["uptime"] = f"{secs // 86400}d {(secs % 86400) // 3600}h"
    except Exception:
        pass
    return sysd


def write_dotenv():
    envfile.update(os.path.join(ROOT, ".env"), {
        "UNIFI_IP": host_only(CONFIG["unifi_host"]),
        "UNIFI_USERNAME": CONFIG["unifi_username"],
        "UNIFI_PASSWORD": CONFIG["unifi_password"],
        "SIP_SERVER": CONFIG["sip_server"],
        "SIP_PORT": str(CONFIG["sip_port"]),
        "MY_IP": CONFIG["my_ip"],
        "RTP_PORT": str(CONFIG["rtp_port"]),
    })


def restart_bridge():
    subprocess.check_call(
        ["sudo", "-n", "systemctl", "restart", "voiptohorns-bridge.service"],
        timeout=20,
    )
    return True


@app.route("/api/bridge/restart", methods=["POST"])
@login_required
def api_bridge_restart():
    try:
        restart_bridge()
        log("bridge restarted from UI")
        return jsonify({"ok": True})
    except Exception as e:
        log(f"bridge restart failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/announce", methods=["POST"])
@login_required
def api_announce():
    data = request.get_json(silent=True) or {}
    ext = str(data.get("ext") or "").zfill(4)
    if not is_owned(ext):
        return _not_owned(ext)
    try:
        inv = build_inventory(console(allow_login=False))
        zone = next((z for z in inv["zones"] if z["ext"] == ext), None)
        if not zone and (not is_owned(ext) or ext in inv["used_exts"]):
            return _not_owned(ext)
        ids = (zone or {}).get("speaker_ids") or []
        if not ids:
            return jsonify({"ok": False, "error": "No horns in this zone"}), 400
        wav = os.path.join(ROOT, "test_announcement_24k.wav")
        if not os.path.isfile(wav):
            return jsonify({"ok": False, "error": "Test clip missing on the Pi"}), 400
        _ANNOUNCE_STOP.set()  # ask any in-flight play to bail
        time.sleep(0.05)
        _ANNOUNCE_STOP.clear()
        threading.Thread(target=_play_wav, args=(ids, wav, zone.get("delays") or {}), daemon=True).start()
        log(f"announce {ext} -> {len(ids)} speakers")
        return jsonify({"ok": True, "speakers": len(ids)})
    except Exception as e:
        log(f"announce failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 400


_ANNOUNCE_LOCK = threading.Lock()
_ANNOUNCE_STOP = threading.Event()


def _play_wav(speaker_ids, wav, delays=None):
    import ssl
    import websockets

    delays = delays or {}
    if not _ANNOUNCE_LOCK.acquire(blocking=False):
        log("announce: skipped overlapping play")
        return
    try:
        c = console(allow_login=False)
        token = c.s.cookies.get("TOKEN")
        nvr = c.host
        if not token:
            log("announce: no UniFi token")
            return

        cmd = ["ffmpeg"] + [x.replace("{wav}", wav) for x in FFMPEG_WAV_TO_ADTS]
        t0 = time.perf_counter()
        encoded = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        frames = list(read_adts_frames(encoded.stdout or b""))
        log(f"announce encoded frames={len(frames)} bytes={len(encoded.stdout or b'')} in {int((time.perf_counter()-t0)*1000)}ms")
        if not frames:
            log("announce: ffmpeg produced no ADTS frames")
            return

        extras = {}
        for sid in speaker_ids:
            try:
                extras[sid] = float(delays.get(sid) or 0) / 1000.0
            except (TypeError, ValueError):
                extras[sid] = 0.0
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        proc = ctx.Process(
            target=play_file_child,
            args=(q, nvr, token, list(speaker_ids), extras, frames),
            daemon=True,
        )
        proc.start()
        proc.join()
        msg = q.get() if not q.empty() else ("err", "play process produced no result")
        if msg[0] != "ok":
            log(f"announce failed: {msg[1] if len(msg) > 1 else msg}")
            return
        sent, wall, avg, d0 = msg[1], msg[2], msg[3], msg[4]
        log(f"announce paced frames={sent} in {wall:.2f}s send_avg={avg:.1f}ms delay0={d0}")
        log("announce done")
    except Exception as e:
        log(f"announce failed: {e}")
    finally:
        try:
            _ANNOUNCE_LOCK.release()
        except Exception:
            pass


PTT_LOOP = None
PTT_ENGINE = None


# Bound the NVR handshake so a hung Protect socket cannot sit in Connecting forever.
# The browser waits a little longer than this for {"state":"live"}.
PTT_PROTECT_OPEN_TIMEOUT = 15.0
# Warm talkback with nobody holding is closed so the NVR does not keep a ghost session.
PTT_WARM_IDLE_S = 45.0



def _delay_ms_to_seconds(raw, ids):
    delays = {}
    for sid in ids:
        try:
            delays[sid] = float((raw or {}).get(sid) or 0) / 1000.0
        except (TypeError, ValueError):
            delays[sid] = 0.0
    return delays


def resolve_ptt_target(ext):
    """Horns for one zone from the saved map. Does not list Talk users or Protect speakers."""
    ext = _norm_ext(ext)
    maps = STATE.get("maps") or {}
    if ext in maps:
        mp = maps.get(ext) or {}
        ids = [sid for sid in (mp.get("speaker_ids") or []) if sid]
        return ext, ids, _delay_ms_to_seconds(mp.get("delays") or {}, ids)
    delays = {}
    try:
        data = json.loads(open(EXT_PATH).read()) if os.path.isfile(EXT_PATH) else {}
    except Exception:
        data = {}
    rec = (data.get("extensions") or {}).get(ext) or {}
    ids = [sid for sid in (rec.get("speakers") or []) if sid]
    file_delays = data.get("delays") or {}
    for sid in ids:
        try:
            delays[sid] = float(file_delays.get(sid) or 0)
        except (TypeError, ValueError):
            delays[sid] = 0.0
    return ext, ids, delays


def console_for_ptt():
    """Cached UniFi session for talkback. Skips the Talk /api/info liveness GET.

    That probe (8s timeout) plus a full inventory listing sat on the only Flask
    thread in front of Hold. Talkback open itself is the check that the token works.
    """
    if not CONFIG.get("unifi_username") or not CONFIG.get("unifi_password"):
        raise RuntimeError("UniFi username and password are required in Config")
    key = (host_only(CONFIG["unifi_host"]), CONFIG["unifi_username"], CONFIG["unifi_password"])
    with _console_lock:
        if _console is not None and _console_key == key and _console.s.cookies.get("TOKEN"):
            return _console
    return console(allow_login=False)


class PttEngine:
    def __init__(self):
        self.active = False
        self.ticket = None
        self.proc = None
        self.speaker_ws = {}
        self.queues = {}
        self.tasks = []
        self.pump_task = None
        self.pcm_bytes = 0
        self.adts_frames = 0
        self.ready = None
        self.failed = None
        self.open_task = None
        self.lock = asyncio.Lock()
        self.phase = "idle"  # idle | warming | ready | live | failed
        self.ext = None
        self.claimed = False
        self.idle_task = None
        self.warm_idle = PTT_WARM_IDLE_S
        self.error = None

    def _reusable_unlocked(self, ext):
        if not ext or self.ext != ext or not self.ticket:
            return False
        if self.claimed or self.phase == "live":
            return False
        return self.phase in ("warming", "ready")

    def _arm_idle_unlocked(self, ticket):
        idle = self.idle_task
        self.idle_task = None
        if idle and idle is not asyncio.current_task() and not idle.done():
            idle.cancel()
        self.idle_task = asyncio.create_task(self._idle_wait(ticket))

    async def _idle_wait(self, ticket):
        try:
            await asyncio.sleep(self.warm_idle)
        except asyncio.CancelledError:
            return
        async with self.lock:
            if self.ticket != ticket or self.claimed or self.phase in ("live", "idle"):
                return
            self.idle_task = None
            log("ptt warm idle teardown %s" % (self.ext or ""))
            await self._stop_unlocked(hold=False)

    async def snapshot(self, ext=None):
        want = _norm_ext(ext) if ext else ""
        if want and self.ext and want != self.ext:
            return {"ok": True, "state": "idle", "warm": False, "ext": want, "ticket": None, "error": None}
        state = self.phase or "idle"
        return {
            "ok": True,
            "state": state,
            "warm": state == "ready",
            "ext": self.ext or "",
            "ticket": self.ticket,
            "error": self.error if state == "failed" else None,
        }

    async def start(self, nvr, token, speaker_ids, delays, ticket, ext=None):
        """Accept a PTT ticket and open Protect talkback in the background.

        Returns as soon as /ws/ptt may use the ticket. Horns are not up yet —
        the socket sends {"state":"live"} when talkback is actually open.
        A warm session for this same zone is reused instead of reopened.
        """
        async with self.lock:
            ext = _norm_ext(ext) if ext else self.ext
            if self._reusable_unlocked(ext):
                return self.ticket, self.phase
            await self._stop_unlocked(hold=False)
            self._begin_unlocked(nvr, token, speaker_ids, delays, ticket, ext, arm_idle=False)
            return ticket, "warming"

    async def warm(self, nvr, token, speaker_ids, delays, ticket, ext):
        """Open Protect talkback before Hold. Stays ready until claimed, switched, or idle."""
        async with self.lock:
            ext = _norm_ext(ext)
            if self._reusable_unlocked(ext):
                self._arm_idle_unlocked(self.ticket)
                log("ptt warm reuse %s state=%s" % (ext, self.phase))
                return self.ticket, self.phase
            await self._stop_unlocked(hold=False)
            self._begin_unlocked(nvr, token, speaker_ids, delays, ticket, ext, arm_idle=True)
            return ticket, "warming"

    def _begin_unlocked(self, nvr, token, speaker_ids, delays, ticket, ext, arm_idle):
        self.ticket = ticket
        self.ext = ext or None
        self.active = True
        self.claimed = False
        self.failed = None
        self.error = None
        self.phase = "warming"
        self.ready = asyncio.Event()
        self.pcm_bytes = 0
        self.adts_frames = 0
        self.speaker_ws = {}
        self.queues = {}
        self.tasks = []
        self.proc = None
        self.pump_task = None
        if arm_idle:
            self._arm_idle_unlocked(ticket)
        self.open_task = asyncio.create_task(
            self._open_protect(nvr, token, list(speaker_ids), dict(delays), ticket)
        )

    async def claim(self, ticket):
        """Hold attached. Idle teardown must not close the session under the mic."""
        async with self.lock:
            if self.ticket != ticket:
                return False
            self.claimed = True
            idle = self.idle_task
            self.idle_task = None
            if idle and not idle.done():
                idle.cancel()
            return True

    async def mark_live(self, ticket):
        """Live only after this ticket's Protect sockets are actually up."""
        async with self.lock:
            if self.ticket != ticket or not self.active or not self.speaker_ws:
                raise RuntimeError("Protect talkback is not ready")
            if self.failed:
                raise RuntimeError(str(self.failed))
            self.phase = "live"
            self.claimed = True

    async def _open_protect(self, nvr, token, speaker_ids, delays, ticket):
        try:
            deadline = time.perf_counter() + PTT_PROTECT_OPEN_TIMEOUT
            for sid in speaker_ids:
                if self.ticket != ticket or not self.active:
                    return
                remaining = deadline - time.perf_counter()
                if remaining <= 0.05:
                    log(f"ptt connect {sid}: timed out")
                    break
                try:
                    ws = await asyncio.wait_for(open_talkback(nvr, sid, token), timeout=remaining)
                except asyncio.TimeoutError:
                    log(f"ptt connect {sid}: timed out")
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log(f"ptt connect {sid}: {e}")
                    continue
                if self.ticket != ticket or not self.active:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                self.speaker_ws[sid] = ws
                self.queues[sid] = asyncio.Queue()
                delay = float(delays.get(sid) or 0)
                self.tasks.append(asyncio.create_task(self._speaker_loop(sid, delay)))
                log(f"ptt connected {sid} delay={int(delay * 1000)}ms")
            if self.ticket != ticket or not self.active:
                return
            if not self.speaker_ws:
                self.failed = RuntimeError("Could not open Protect talkback to any horn")
                self.error = str(self.failed)
                self.phase = "failed"
                self.active = False
                return
            proc = await asyncio.create_subprocess_exec(
                *(["ffmpeg"] + list(FFMPEG_PCM24_TO_ADTS)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if self.ticket != ticket or not self.active:
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            self.proc = proc
            self.pump_task = asyncio.create_task(self._pump_adts())
            if self.ticket == ticket and self.active:
                self.phase = "ready"
                self.error = None
            log(f"ptt protect ready speakers={len(self.speaker_ws)}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"ptt protect open failed: {e}")
            if self.ticket == ticket:
                self.failed = e
                self.error = str(e)
                self.phase = "failed"
                self.active = False
                for ws in list(self.speaker_ws.values()):
                    try:
                        await ws.close()
                    except Exception:
                        pass
                self.speaker_ws = {}
        finally:
            if self.ticket == ticket and self.ready is not None and not self.ready.is_set():
                self.ready.set()

    async def wait_until_ready(self, ticket, timeout=20):
        """Block until this ticket's Protect talkback is up. Raises if it will not be."""
        ready = self.ready
        if ready is None or self.ticket != ticket:
            raise RuntimeError("PTT session closed")
        try:
            await asyncio.wait_for(ready.wait(), timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Protect talkback timed out") from None
        if self.failed and self.ticket == ticket:
            raise RuntimeError(str(self.failed)) from None
        if self.ticket != ticket or not self.active or not self.speaker_ws:
            raise RuntimeError("PTT session closed")

    async def _speaker_loop(self, sid, delay):
        q = self.queues[sid]
        pacer = FramePacer(extra_delay=delay)
        while self.active:
            try:
                item = await q.get()
                if item is None:
                    break
                data = item[1] if isinstance(item, tuple) else item
                ws = self.speaker_ws.get(sid)
                if ws:
                    await pacer.send(ws, data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"ptt send {sid}: {e}")
                break

    async def _pump_adts(self):
        try:
            while self.active and self.proc and self.proc.stdout:
                header = await self.proc.stdout.read(7)
                if not header or len(header) < 7:
                    break
                frame_length = ((header[3] & 3) << 11) | (header[4] << 3) | ((header[5] & 0xE0) >> 5)
                rest = max(frame_length - 7, 0)
                payload = await self.proc.stdout.read(rest) if rest else b""
                frame = header + payload
                self.adts_frames += 1
                if self.adts_frames == 1 or self.adts_frames == 25:
                    log(f"ptt adts frames={self.adts_frames} pcm={self.pcm_bytes}b")
                for q in self.queues.values():
                    await q.put(frame)
        except Exception as e:
            log(f"ptt ffmpeg pump: {e}")

    async def feed_pcm(self, data):
        if self.phase != "live" or not self.active or not self.proc or not self.proc.stdin:
            return
        self.pcm_bytes += len(data)
        if self.pcm_bytes == len(data):
            log(f"ptt pcm first {len(data)}b")
        self.proc.stdin.write(data)
        buf = 0
        try:
            buf = self.proc.stdin.transport.get_write_buffer_size()
        except Exception:
            pass
        if buf > 32768:
            await self.proc.stdin.drain()

    async def stop(self, hold=True):
        async with self.lock:
            await self._stop_unlocked(hold=hold)

    async def _stop_unlocked(self, hold=True):
        had_frames = self.adts_frames if hasattr(self, "adts_frames") else 0
        open_task = self.open_task
        self.open_task = None
        self.active = False
        self.ticket = None
        self.phase = "idle"
        self.ext = None
        self.claimed = False
        self.failed = None
        self.error = None
        idle = self.idle_task
        self.idle_task = None
        if idle and idle is not asyncio.current_task() and not idle.done():
            idle.cancel()
        ready = self.ready
        if ready is not None and not ready.is_set():
            ready.set()
        if open_task and open_task is not asyncio.current_task() and not open_task.done():
            open_task.cancel()
            try:
                await open_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self.pump_task:
            self.pump_task.cancel()
            self.pump_task = None
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        for q in self.queues.values():
            try:
                q.put_nowait(None)
            except Exception:
                pass
        self.queues = {}
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
        if hold and had_frames:
            await asyncio.sleep(TAIL_HOLD)
        for ws in list(self.speaker_ws.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self.speaker_ws = {}


async def ptt_client_handler(ws):
    """Browser /ws/ptt. Ticket is accepted while Protect is still opening; live comes later."""
    ticket = None
    owned = False
    try:
        raw = await asyncio.wait_for(ws.recv(), 10)
        if isinstance(raw, bytes):
            raw = raw.decode()
        msg = json.loads(raw)
        ticket = msg.get("ticket")
        engine = PTT_ENGINE
        # Ticket match is enough to own the socket. Protect may already have
        # failed (active cleared) and the client still needs that error.
        if not engine or not ticket or ticket != engine.ticket:
            await ws.close(code=4401, reason="no ptt session")
            return
        owned = True
        await engine.claim(ticket)
        try:
            await engine.wait_until_ready(ticket, timeout=20)
            await engine.mark_live(ticket)
        except Exception as e:
            try:
                await ws.send(json.dumps({"ok": False, "error": str(e)}))
            except Exception:
                pass
            try:
                await ws.close(code=4408, reason="protect not ready")
            except Exception:
                pass
            return
        await ws.send(json.dumps({"ok": True, "state": "live"}))
        async for message in ws:
            if isinstance(message, bytes) and engine.phase == "live" and engine.ticket == ticket:
                await engine.feed_pcm(message)
    except Exception:
        pass
    finally:
        # Only the socket that owns the current ticket may tear it down.
        # A rejected or replaced client must not stop the session that replaced it.
        if owned and ticket and PTT_ENGINE and PTT_ENGINE.ticket == ticket:
            await PTT_ENGINE.stop()
        log("ptt client closed")


def _start_ptt_loop():
    global PTT_LOOP, PTT_ENGINE
    import websockets

    PTT_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(PTT_LOOP)
    PTT_ENGINE = PttEngine()

    async def runner():
        async with websockets.serve(ptt_client_handler, "127.0.0.1", 5051):
            log("ptt websocket on 127.0.0.1:5051")
            await asyncio.Future()

    try:
        PTT_LOOP.run_until_complete(runner())
    except Exception as e:
        log(f"ptt loop died: {e}")


def ptt_call(coro, timeout=20):
    if not PTT_LOOP:
        raise RuntimeError("PTT engine not started")
    fut = asyncio.run_coroutine_threadsafe(coro, PTT_LOOP)
    return fut.result(timeout=timeout)


def _warm_body(snap):
    state = snap.get("state") or "idle"
    body = {
        "ok": True,
        "state": state,
        "warm": bool(snap.get("warm") or state == "ready"),
        "ext": snap.get("ext") or "",
        "ticket": snap.get("ticket"),
    }
    if state == "failed" and snap.get("error"):
        body["error"] = snap["error"]
    return body


@app.route("/api/ptt/warm", methods=["GET", "POST"])
@login_required
def api_ptt_warm():
    """Pre-open Protect talkback for a zone. Hold stays off until state is ready."""
    if request.method == "GET":
        ext = _norm_ext(request.args.get("ext"))
        if not PTT_ENGINE:
            return jsonify({"ok": False, "error": "PTT engine not started"}), 400
        return jsonify(_warm_body(ptt_call(PTT_ENGINE.snapshot(ext))))
    data = request.get_json(silent=True) or {}
    ext, ids, delays = resolve_ptt_target(data.get("ext"))
    if not is_owned(ext):
        return _not_owned(ext)
    if not ids:
        return jsonify({"ok": False, "error": "No horns in this zone"}), 400
    try:
        c = console_for_ptt()
        token = c.s.cookies.get("TOKEN")
        if not token:
            return jsonify({"ok": False, "error": "Not connected to UniFi. Refresh From Console."}), 400
        if not PTT_ENGINE:
            return jsonify({"ok": False, "error": "PTT engine not started"}), 400
        ticket = secrets.token_hex(16)
        got, phase = ptt_call(PTT_ENGINE.warm(c.host, token, ids, delays, ticket, ext))
        err = PTT_ENGINE.error if phase == "failed" else None
        log("ptt warm %s speakers=%s state=%s" % (ext, len(ids), phase))
        return jsonify(_warm_body({
            "state": phase,
            "warm": phase == "ready",
            "ext": ext,
            "ticket": got,
            "error": err,
        }))
    except Exception as e:
        log("ptt warm failed: %s" % e)
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/ptt/start", methods=["POST"])
@login_required
def api_ptt_start():
    data = request.get_json(silent=True) or {}
    ext, ids, delays = resolve_ptt_target(data.get("ext"))
    if not is_owned(ext):
        return _not_owned(ext)
    if not ids:
        return jsonify({"ok": False, "error": "No horns in this zone"}), 400
    try:
        c = console_for_ptt()
        token = c.s.cookies.get("TOKEN")
        if not token:
            return jsonify({"ok": False, "error": "Not connected to UniFi. Refresh From Console."}), 400
        if not PTT_ENGINE:
            return jsonify({"ok": False, "error": "PTT engine not started"}), 400
        ticket = secrets.token_hex(16)
        # Returns when the ticket is registered, not when the NVR sockets are open.
        got, phase = ptt_call(PTT_ENGINE.start(c.host, token, ids, delays, ticket, ext))
        state = "ready" if phase == "ready" else "connecting"
        log(f"ptt start {ext} speakers={len(ids)} state={state}")
        return jsonify({"ok": True, "ticket": got, "state": state})
    except Exception as e:
        log(f"ptt start failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/ptt/stop", methods=["POST"])
@login_required
def api_ptt_stop():
    try:
        if PTT_ENGINE:
            ptt_call(PTT_ENGINE.stop(), timeout=8)
        log("ptt stop")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


TTS_TEXT_MAX = 120


@app.route("/api/tts", methods=["POST"])
@login_required
def api_tts():
    """Talk-page robotic TTS via Protect (protect_tts.play_live)."""
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"ok": False, "error": "Text is required"}), 400
    if len(text) > TTS_TEXT_MAX:
        return jsonify({"ok": False, "error": "Text must be 120 characters or fewer"}), 400
    raw_ids = data.get("speaker_ids")
    ids = []
    if isinstance(raw_ids, list):
        for sid in raw_ids:
            speaker_id = str(sid).strip() if sid is not None else ""
            if speaker_id:
                ids.append(speaker_id)
    if not ids:
        return jsonify({"ok": False, "error": "speaker_ids is required"}), 400
    ext = data.get("ext")
    ext_s = str(ext).strip() if ext not in (None, "") else "-"
    # Only horns mapped on this install's own zones (the named zone when one is given).
    if ext_s != "-":
        ext_n = _norm_ext(ext_s)
        if not is_owned(ext_n):
            log("live tts refused ext=%s: not created here" % ext_s)
            return _not_owned(ext_n)
        allowed = set((STATE["maps"].get(ext_n) or {}).get("speaker_ids") or [])
    else:
        allowed = set()
        for e in owned_exts():
            if is_owned(e):
                allowed |= set((STATE["maps"].get(e) or {}).get("speaker_ids") or [])
    outside = [sid for sid in ids if sid not in allowed]
    if outside:
        log("live tts refused: %s speaker(s) not in this install's zones" % len(outside))
        return jsonify({
            "ok": False,
            "error": "Only horns in zones created on this dashboard can be used here.",
        }), 403
    preferred = (os.getenv("VTH_TTS_AUTOMATION_ID") or "").strip()
    log("live tts ext=%s speakers=%s len=%s" % (ext_s, len(ids), len(text)))
    try:
        # One automation is rewritten per speaker. Hold the lock across the fan-out
        # so two PLAY clicks cannot interleave PATCH and /run on the same horns.
        with protect_tts.PLAY_LOCK:
            c = console(allow_login=True)
            body = protect_tts.play_live(c, text.strip(), ids, preferred, log=log)
    except protect_tts.TtsError as e:
        log("live tts failed: %s" % e)
        return jsonify({"ok": False, "mode": "live", "error": str(e)}), e.status
    except Exception as e:
        log("live tts failed: %s" % e)
        return jsonify({"ok": False, "mode": "live", "error": str(e)}), 502
    if not body.get("ok"):
        failed = len([row for row in body.get("results") or [] if not row.get("ok")])
        total = len(body.get("results") or [])
        body["error"] = "%s of %s speakers failed" % (failed, total)
    return jsonify(body)


def tail_lines(path, max_lines=70, max_bytes=262144):
    """Last lines of a log. Seek to the end; never read a multi-megabyte bridge log whole.

    A full read of bridge_tcp.log (it can grow to tens of MB) on unthreaded Flask
    head-of-line blocked /api/ptt/start for about 1.6s, ~3.2s when queued.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        window = min(size, max_bytes)
        f.seek(size - window)
        raw = f.read(window)
    if size > max_bytes:
        cut = raw.find(b"\n")
        if cut != -1:
            raw = raw[cut + 1 :]
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


@app.route("/api/logs")
@login_required
def api_logs():
    lines = []
    blog = os.path.join(ROOT, "bridge_tcp.log")
    if os.path.isfile(blog):
        try:
            lines.extend(tail_lines(blog, max_lines=70))
        except Exception:
            pass
    lines.extend(LOGS[-30:])
    return jsonify({"lines": lines[-90:]})


if __name__ == "__main__":
    host = os.getenv("VTH_UI_HOST", "127.0.0.1")
    port = int(os.getenv("VTH_UI_PORT", "5050"))
    threading.Thread(target=_start_ptt_loop, name="ptt-ws", daemon=True).start()
    time.sleep(0.4)
    print(f"Dashboard: http://{host}:{port}")
    # threaded: a slow request (logs, inventory) must not HOL-block PTT.
    app.run(host=host, port=port, debug=False, threaded=True)
