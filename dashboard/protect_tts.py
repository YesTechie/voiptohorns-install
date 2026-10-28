"""Live Protect TTS via Alarm Manager Custom Message test.

When Global Alarm Manager is enabled (nvr.featureFlags.useExternalAlarmManager),
so classic PATCH/POST /proxy/protect/api/automations/{id}(/run) returns 400:
"This operation is not available when global alarm manager is enabled".

Working call:
  POST /api/v2/alarms/protect/test  -> 204
  body:
    {
      "title": "VTH TTS",
      "triggers_data": [[{"id": "protect:button.buttonPressed",
                          "data": {"pressType": "press"}}]],
      "actions_data": [[{"id": "protect:sound.customMessage",
                         "data": {"sources": ["<MAC>"], "text": "..."}}]]
    }
Horn MACs come from Protect speaker.mac (same values as horn-speakers list).
"""
import re
import threading

TTS_TEXT_MAX = 120
PLAY_LOCK = threading.Lock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_TEST_TRIGGER = {
    "id": "protect:button.buttonPressed",
    "data": {"pressType": "press"},
}
_ACTION = "protect:sound.customMessage"
_TEST_PATH = "/api/v2/alarms/protect/test"

PLAY_TEXT = "PLAY_TEXT_ON_SPEAKER"  # legacy export name
NO_AUTOMATION = (
    "Classic PLAY_TEXT_ON_SPEAKER automations are blocked while Global Alarm "
    "Manager is enabled. VoIPtoHorns uses POST /api/v2/alarms/protect/test with "
    "protect:sound.customMessage instead."
)


class TtsError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def cap_text(text):
    return (text or "")[:TTS_TEXT_MAX]


def normalize_mac(raw):
    if raw is None:
        raise TtsError("speaker has no mac")
    hexchars = "".join(ch for ch in str(raw) if ch in "0123456789abcdefABCDEF")
    if len(hexchars) != 12:
        raise TtsError("speaker mac is not 12 hex digits")
    return hexchars.upper()


def _safe_id(value, label):
    text = str(value or "").strip()
    if not _SAFE_ID.match(text):
        raise TtsError("%s is not a safe id" % label)
    return text


def _json_body(resp, what):
    try:
        return resp.json()
    except Exception as e:
        raise TtsError("%s returned invalid JSON (%s)" % (what, e))


def speaker_mac(client, speaker_id):
    sid = _safe_id(speaker_id, "speaker id")
    resp = client.get("/proxy/protect/api/speakers/%s" % sid)
    code = getattr(resp, "status_code", 0)
    if code != 200:
        raise TtsError("speaker %s lookup failed (%s)" % (sid, code))
    data = _json_body(resp, "speaker %s" % sid)
    if not isinstance(data, dict):
        raise TtsError("speaker %s lookup returned no object" % sid)
    return normalize_mac(data.get("mac"))


def test_body(text, macs):
    spoken = cap_text(text).strip()
    if not spoken:
        raise TtsError("Text is required", status=400)
    if not macs:
        raise TtsError("No speaker MACs", status=400)
    return {
        "title": "VTH TTS",
        "triggers_data": [[dict(_TEST_TRIGGER)]],
        "actions_data": [[{
            "id": _ACTION,
            "data": {
                "sources": list(macs),
                "text": spoken,
            },
        }]],
    }


def play_live(client, text, speaker_ids, preferred_id=None, log=None):
    """Play robotic Custom Message on Protect speaker ids via Alarm Manager test."""
    spoken = cap_text(text)
    if not spoken.strip():
        raise TtsError("Text is required", status=400)

    results = []
    for sid in speaker_ids:
        try:
            mac = speaker_mac(client, sid)
            results.append({"id": sid, "mac": mac, "ok": True})
        except Exception as e:
            results.append({"id": sid, "ok": False, "error": str(e)})
            if log:
                log("live tts speaker=%s failed: %s" % (sid, e))

    good_macs = [row["mac"] for row in results if row.get("ok") and row.get("mac")]
    if not good_macs:
        return {
            "ok": False,
            "mode": "live",
            "path": _TEST_PATH,
            "action": _ACTION,
            "results": results,
        }

    body = test_body(spoken, good_macs)
    if log:
        log(
            "live tts alarm-manager customMessage sources=%s len=%s"
            % (",".join(good_macs), len(spoken))
        )
    resp = client.req("POST", _TEST_PATH, json=body)
    code = getattr(resp, "status_code", 0)
    ok = code == 204 or (200 <= int(code or 0) < 300)
    if log:
        log("live tts alarm-manager test status=%s ok=%s" % (code, ok))
    if not ok:
        err = (getattr(resp, "text", None) or "")[:300]
        for row in results:
            if row.get("ok"):
                row["ok"] = False
                row["status"] = code
                row["error"] = "alarm test failed (%s) %s" % (code, err)
        raise TtsError(
            "Alarm Manager TTS test failed (%s): %s" % (code, err or "no body"),
            status=502,
        )
    for row in results:
        if row.get("ok"):
            row["status"] = code
    return {
        "ok": all(row.get("ok") for row in results) if results else False,
        "mode": "live",
        "path": _TEST_PATH,
        "action": _ACTION,
        "results": results,
    }


def automation_id(auto):
    if not isinstance(auto, dict):
        return ""
    return str(auto.get("id") or auto.get("_id") or "").strip()


def iter_automations(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("automations", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def has_play_text(auto):
    return False


def pick_automation(payload, preferred_id):
    return None


def tts_metadata(text, mac):
    return {"text": cap_text(text), "sources": [mac]}


def patch_body(automation, text, mac):
    raise TtsError(NO_AUTOMATION)


def run_played(status_code):
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return False, None
    if code == 204 or 200 <= code < 300:
        return True, None
    return False, None


def load_automations(client):
    raise TtsError(NO_AUTOMATION)
