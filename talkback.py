"""Protect AI Horn talkback — 32 kHz ADTS, 32 ms pace, 3-frame pre-roll.

Matches the live-horn settings in UNIFI_HORN_TALKBACK.md (GITS Method B).
24 kHz / 42.67 ms underruns the horn jitter buffer (scatter / PLC echo).
"""
import asyncio
import socket
import time

AAC_RATE = 32000
FRAME_DELAY = 1024.0 / AAC_RATE  # 0.032 s
PRE_ROLL = 3
TAIL_HOLD = 1.5

# Live 24 kHz s16le PCM (browser mic) → 32 kHz ADTS. Flags match the SIP pipe so
# ffmpeg actually emits frames instead of buffering until the mic socket closes.
FFMPEG_PCM24_TO_ADTS = [
    "-hide_banner", "-loglevel", "error",
    "-fflags", "nobuffer", "-flags", "low_delay",
    "-probesize", "32", "-analyzeduration", "0",
    "-use_wallclock_as_timestamps", "1",
    "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
    "-af", "volume=4.0,aresample=24000,asetrate=32000",
    "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "64k", "-ac", "1", "-ar", "32000",
    "-flush_packets", "1", "-f", "adts", "pipe:1",
]

# SIP RTP PCMU 8 kHz → 24 kHz PCM → same 32 kHz tag.
FFMPEG_MULAW8_TO_ADTS = [
    "-hide_banner", "-loglevel", "error",
    "-fflags", "nobuffer", "-flags", "low_delay",
    "-probesize", "32", "-analyzeduration", "0",
    "-f", "mulaw", "-ar", "8000", "-i", "pipe:0",
    "-af", "volume=4.0,aresample=24000,asetrate=32000",
    "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "64k", "-ac", "1", "-ar", "32000",
    "-f", "adts", "pipe:1",
]

# File playback: 1 s silence head/tail, then 32 kHz ADTS.
FFMPEG_WAV_TO_ADTS = [
    "-hide_banner", "-loglevel", "error",
    "-i", "{wav}",
    "-filter:a", "aresample=24000,adelay=1000:all=1,apad=pad_dur=1,asetrate=32000",
    "-c:a", "aac", "-ac", "1", "-ar", "32000",
    "-f", "adts", "pipe:1",
]


def read_adts_frames(aac_data: bytes):
    """Yield one complete ADTS frame at a time from a buffer."""
    idx, total = 0, len(aac_data)
    while idx < total:
        while idx < total - 1:
            if aac_data[idx] == 0xFF and (aac_data[idx + 1] & 0xF0) == 0xF0:
                break
            idx += 1
        if idx >= total - 6:
            break
        header = aac_data[idx:idx + 7]
        frame_len = ((header[3] & 0x03) << 11) | (header[4] << 3) | ((header[5] & 0xE0) >> 5)
        if frame_len < 7 or idx + frame_len > total:
            break
        yield aac_data[idx:idx + frame_len]
        idx += frame_len


# high=0 disables asyncio pause_writing callbacks. Otherwise drain() waits
# until the NVR/horn TCP window opens (~58 ms/frame on the Pi → stutter).
WS_WRITE_LIMIT = 0


async def _no_drain():
    return


def tune_ws_socket(ws):
    sock = None
    transport = getattr(ws, "transport", None)
    if transport and hasattr(transport, "get_extra_info"):
        sock = transport.get_extra_info("socket")
    if sock is None:
        proto = getattr(ws, "protocol", None)
        transport = getattr(proto, "transport", None) if proto else None
        if transport and hasattr(transport, "get_extra_info"):
            sock = transport.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 184)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 18)
    except OSError:
        pass


_TALKBACK_SSL = None


def talkback_ssl_context():
    """One SSL context per process. Building it is most of a talkback connect (~65 ms)."""
    global _TALKBACK_SSL
    ctx = _TALKBACK_SSL
    if ctx is not None:
        return ctx
    import ssl as sslmod

    ctx = sslmod.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = sslmod.CERT_NONE
    _TALKBACK_SSL = ctx
    return ctx


async def open_talkback(nvr, speaker_id, token, ssl_context=None, extra_headers=None):
    """Protect talkback WS with TCP_NODELAY and a write buffer that does not block pacing."""
    import websockets

    if ssl_context is None:
        ssl_context = talkback_ssl_context()
    url = f"wss://{nvr}/proxy/protect/ws/talkback?speaker={speaker_id}&token={token}"
    headers = {"Cookie": f"TOKEN={token}", "X-Token": token}
    if extra_headers:
        headers.update(extra_headers)
    kw = dict(
        ssl=ssl_context,
        ping_interval=None,
        ping_timeout=None,
        write_limit=WS_WRITE_LIMIT,
    )
    try:
        ws = await websockets.connect(url, additional_headers=headers, **kw)
    except TypeError:
        ws = await websockets.connect(url, extra_headers=headers, **kw)
    tune_ws_socket(ws)
    ws.drain = _no_drain
    return ws


def emit_adts(ws, frame: bytes):
    """Write one ADTS frame onto an open talkback WS without waiting for drain."""
    ws.protocol.send_binary(frame)
    ws.send_data()


def pace_frames(sockets, frames, extra_delay=0.0, should_stop=None):
    """Pre-roll 3, then 32 ms clock using time.sleep (dedicated play thread).

    Returns (sent, wall_s, send_avg_ms, first_delays_ms).
    """
    if extra_delay > 0:
        time.sleep(extra_delay)
    t0 = time.perf_counter()
    send_s = 0.0
    start = None
    first_delays = []
    sent = 0
    for frame in frames:
        if should_stop and should_stop():
            break
        t_send = time.perf_counter()
        for ws in sockets:
            emit_adts(ws, frame)
        send_s += time.perf_counter() - t_send
        sent += 1
        if sent < PRE_ROLL:
            continue
        if start is None:
            start = time.perf_counter()
            continue
        delay = start + (sent - PRE_ROLL) * FRAME_DELAY - time.perf_counter()
        if len(first_delays) < 6:
            first_delays.append(round(delay * 1000, 1))
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.100:
            start = time.perf_counter() - (sent - PRE_ROLL) * FRAME_DELAY
    wall = time.perf_counter() - t0
    avg = (send_s / sent) * 1000 if sent else 0.0
    return sent, wall, avg, first_delays


def play_file_sync(nvr, token, speaker_ids, extra_delays, frames):
    """Open talkback and pace in this process. Used from a child so Flask's GIL cannot stall writes."""

    async def go():
        sockets = []
        extras = []
        try:
            for sid in speaker_ids:
                sockets.append(await open_talkback(nvr, sid, token))
                try:
                    extras.append(float((extra_delays or {}).get(sid) or 0))
                except (TypeError, ValueError):
                    extras.append(0.0)
            if not sockets:
                return 0, 0.0, 0.0, []
            result = pace_frames(sockets, frames, extra_delay=max(extras) if extras else 0.0)
            # Horn still has the tail in its buffer. Closing now cuts the last words.
            await asyncio.sleep(TAIL_HOLD)
            return result
        finally:
            for ws in sockets:
                try:
                    await ws.close()
                except Exception:
                    pass

    return asyncio.run(go())


def play_file_child(q, nvr, token, speaker_ids, extra_delays, frames):
    try:
        q.put(("ok",) + play_file_sync(nvr, token, speaker_ids, extra_delays, frames))
    except Exception as e:
        q.put(("err", repr(e)))


class FramePacer:
    """Burst PRE_ROLL frames, then absolute 32 ms pacing with a 100 ms drift valve."""

    def __init__(self, extra_delay=0.0, pre_roll=PRE_ROLL, frame_delay=FRAME_DELAY):
        self.extra_delay = float(extra_delay or 0)
        self.pre_roll = pre_roll
        self.frame_delay = frame_delay
        self.sent = 0
        self.start = None
        self.send_s = 0.0

    def send_avg_ms(self):
        return (self.send_s / self.sent) * 1000 if self.sent else 0.0

    async def flush(self):
        return

    async def send(self, ws, frame):
        if self.sent == 0 and self.extra_delay > 0:
            await asyncio.sleep(self.extra_delay)
        t0 = time.perf_counter()
        emit_adts(ws, frame)
        self.send_s += time.perf_counter() - t0
        self.sent += 1
        if self.sent < self.pre_roll:
            if self.sent == self.pre_roll:
                self.start = time.perf_counter()
            return
        if self.start is None:
            self.start = time.perf_counter()
            return
        delay = self.start + (self.sent - self.pre_roll) * self.frame_delay - time.perf_counter()
        if delay < -0.100:
            self.start = time.perf_counter() - (self.sent - self.pre_roll) * self.frame_delay
            return
        if delay > 0:
            await asyncio.sleep(delay)
