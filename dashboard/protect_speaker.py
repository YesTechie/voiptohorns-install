"""Put a Protect speaker into talk mode when it is mapped onto a zone.

Protect contract. Same call for AI Speaker, UP-AI-Speaker-Mini,
and any other id from the Protect speakers API. AI Horns are usually already
talk; repeating the PATCH is a success.

  PATCH /proxy/protect/api/speakers/{id}
  {"speakerState": {"mode": "talk"}}
  200 and speakerState.mode == "talk"

Auth stays on the existing console session (Cookie TOKEN, X-CSRF-Token when the
login stored one). This adds X-Token from that same TOKEN cookie, matching
talkback. A non-200 or a body that is not talk is a soft failure: the caller
logs it and keeps going.
"""
import re

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TALK_BODY = {"speakerState": {"mode": "talk"}}
_PATH = "/proxy/protect/api/speakers/%s"


def _safe_id(speaker_id):
    text = str(speaker_id or "").strip()
    if not _SAFE_ID.match(text):
        return None
    return text


def session_token(client):
    """TOKEN cookie already held by the UniFi console session, if any."""
    session = getattr(client, "s", None)
    cookies = getattr(session, "cookies", None) if session is not None else None
    if cookies is None:
        return None
    try:
        return cookies.get("TOKEN") or None
    except Exception:
        return None


def talk_headers(client):
    """Per-request headers. Session cookies and X-CSRF-Token are left in place."""
    headers = {"Content-Type": "application/json"}
    token = session_token(client)
    if token:
        headers["X-Token"] = token
    return headers


def _status(resp):
    try:
        return int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _body(resp):
    try:
        data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _mode(data):
    if not isinstance(data, dict):
        return None
    state = data.get("speakerState")
    if isinstance(state, dict) and state.get("mode"):
        return state.get("mode")
    for key in ("speaker", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = _mode(nested)
            if found:
                return found
    return None


def _snip(resp):
    text = getattr(resp, "text", None)
    if not text:
        return ""
    return str(text).strip().replace("\n", " ")[:180]


def set_talk(client, speaker_id):
    """PATCH one speaker to talk. Never raises. ok is False on non-200 or a bad body."""
    sid = _safe_id(speaker_id)
    if not sid:
        return {
            "id": str(speaker_id or "").strip(),
            "ok": False,
            "status": 0,
            "error": "speaker id is not safe to send",
        }
    try:
        resp = client.req(
            "PATCH",
            _PATH % sid,
            json={"speakerState": {"mode": "talk"}},
            headers=talk_headers(client),
            timeout=15,
        )
    except Exception as e:
        return {"id": sid, "ok": False, "status": 0, "error": str(e)}
    code = _status(resp)
    if code != 200:
        detail = _snip(resp)
        err = "HTTP %s" % code
        if detail:
            err = "%s %s" % (err, detail)
        return {"id": sid, "ok": False, "status": code, "error": err}
    mode = _mode(_body(resp))
    if mode != "talk":
        return {
            "id": sid,
            "ok": False,
            "status": code,
            "mode": mode,
            "error": "response speakerState.mode is %r" % mode,
        }
    return {"id": sid, "ok": True, "status": 200, "mode": "talk"}


def _warn(row):
    return (
        "Speaker %s did not enter talk mode (%s). "
        "Page and talk audio can stay silent while it is in listen."
        % (row.get("id") or "?", row.get("error") or "unknown error")
    )


def set_talk_many(client, speaker_ids, log=None):
    """PATCH each mapped speaker. One failure does not stop the rest."""
    results = []
    warnings = []
    seen = set()
    for raw in speaker_ids or []:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        row = set_talk(client, text)
        results.append(row)
        if row.get("ok"):
            if log:
                log("speaker %s talk mode ok" % row["id"])
        else:
            msg = _warn(row)
            warnings.append(msg)
            if log:
                log(msg)
    return {"ok": not warnings, "results": results, "warnings": warnings}
