import asyncio
import logging
import hashlib
import time
import os
import websockets
import json
import base64
import ssl
import re
import socket
import math
from dotenv import load_dotenv
from talkback import FFMPEG_MULAW8_TO_ADTS, FramePacer, TAIL_HOLD, open_talkback
from netinfo import detect_my_ip

load_dotenv(interpolate=False)  # passwords may contain "$"

# --- CONFIGURATION ---
# No site defaults: UNIFI_IP must come from .env. SIP_SERVER defaults to UNIFI_IP.
# MY_IP is auto-detected (the address used to reach the console) when unset.
UNIFI_IP = (os.getenv("UNIFI_IP") or "").strip()
UNIFI_USERNAME = os.getenv("UNIFI_USERNAME")
UNIFI_PASSWORD = os.getenv("UNIFI_PASSWORD")

SIP_SERVER = (os.getenv("SIP_SERVER") or "").strip() or UNIFI_IP
SIP_PORT = int(os.getenv("SIP_PORT", "5060"))
MY_IP = (os.getenv("MY_IP") or "").strip() or detect_my_ip(SIP_SERVER or UNIFI_IP)
RTP_PORT = int(os.getenv("RTP_PORT", "10000"))

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "bridge_status.json")


def _owned_exts():
    """Numbers of the zones this install created (dashboard/state.json).

    "owned" (matched to Talk user IDs by the dashboard), else the previous version's
    "owned_exts" list, else VTH_ADOPT_EXTS. The dashboard writes extensions.json only for zones whose Talk user ID
    and phone match; this is a second guard so an old extensions.json can never register
    another device's zone."""
    path = os.path.join(HERE, "dashboard", "state.json")
    try:
        raw = json.loads(open(path).read())
    except Exception:
        raw = {}
    owned = raw.get("owned")
    if isinstance(owned, dict):
        return {str(e).strip().zfill(4) for e in owned if str(e).strip().isdigit()}
    legacy = raw.get("owned_exts")
    if isinstance(legacy, list):
        return {str(e).strip().zfill(4) for e in legacy if str(e).strip().isdigit()}

    # No record at all (older installs): only an explicit VTH_ADOPT_EXTS list (.env or environment).
    raw_adopt = (os.getenv("VTH_ADOPT_EXTS") or "").strip()
    return {e.strip().zfill(4) for e in raw_adopt.split(",") if e.strip().isdigit()}


def _load_runtime_ext():
    path = os.path.join(HERE, "extensions.json")
    if not os.path.isfile(path):
        return None
    try:
        data = json.loads(open(path).read())
        exts = data.get("extensions") or {}
    except Exception:
        return None
    return _own_only(exts), data.get("delays") or {}


NO_HORNS = set()  # owned zones with no horns: not registered, INVITE gets 480


def _own_only(exts):
    """Never register another device's extension, nor a zone with no horns (it would answer
    calls and play them to nobody)."""
    owned = _owned_exts()
    skipped = sorted(e for e in exts if str(e).zfill(4) not in owned)
    if skipped:
        print("extensions.json: not registering zones not created here: " + ", ".join(skipped), flush=True)
    out = {}
    for e, v in exts.items():
        if str(e).zfill(4) not in owned:
            continue
        if not list((v or {}).get("speakers") or []):
            NO_HORNS.add(e)
            continue
        out[e] = v
    if NO_HORNS:
        print("not registering zones with no horns: " + ", ".join(sorted(NO_HORNS)), flush=True)
    return out


def _console_addrs():
    """IP addresses of the UniFi console (SIP_SERVER and UNIFI_IP). Only these may send SIP."""
    out = set()
    for host in (SIP_SERVER, UNIFI_IP):
        host = (host or "").strip()
        if not host:
            continue
        if host.startswith("["):
            host = host[1:].split("]")[0]
        elif host.count(":") == 1:
            host = host.split(":")[0]  # host:port
        try:
            for info in socket.getaddrinfo(host, None):
                out.add(info[4][0])
        except Exception:
            out.add(host)
    return out


def _write_status(regs):
    """bridge_status.json: per ext, when this Pi last got 200 OK to REGISTER (dashboard reads it)."""
    tmp = "%s.tmp-%d" % (STATUS_PATH, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "updated": time.time(),
                       "exts": {e: {"last_ok": t} for e, t in regs.items()},
                       "no_horns": sorted(NO_HORNS)}, f)
        os.replace(tmp, STATUS_PATH)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print("bridge_status.json write failed: %s" % e, flush=True)


_runtime = _load_runtime_ext()
if _runtime:
    EXTENSIONS, SPEAKER_DELAYS = _runtime
else:
    # No extensions.json yet (fresh install): zero zones. The dashboard writes it.
    EXTENSIONS, SPEAKER_DELAYS = {}, {}

# Talkback uses 32 kHz ADTS / 32 ms (see talkback.py) 

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("VoIPBridge")

def get_header(msg, long_name, short_name):
    pattern = rf'^(?:{long_name}|{short_name}):\s*(.*)$'
    match = re.search(pattern, msg, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None

# --- PROTECT STREAMER (ON-DEMAND + DELAY LINE) ---
class ProtectStreamer:
    def __init__(self, nvr_ip, speaker_delays):
        self.nvr_ip = nvr_ip
        self.speaker_delays = speaker_delays
        self.speaker_ids = []
        self.websockets = {}
        self.queues = {}
        self.queue_tasks = []
        self.active = False

    async def connect_all(self):
        import protect_token
        token = await protect_token.get_valid_token()
        self.active = True
        for sid in self.speaker_ids:
            try:
                ws = await open_talkback(
                    self.nvr_ip,
                    sid,
                    token,
                    extra_headers={"Authorization": f"Bearer {token}"},
                )
                self.websockets[sid] = ws
                logger.info(f"Streamer: Connected to {sid}")
                
                self.queues[sid] = asyncio.Queue()
                self.queue_tasks.append(asyncio.create_task(self._speaker_queue_loop(sid)))
            except Exception as e: 
                logger.error(f"Streamer: Failed to connect to {sid}: {e}")

    async def disconnect_all(self):
        self.active = False
        for q in self.queues.values():
            await q.put(None)
        
        for t in self.queue_tasks:
            t.cancel()
        self.queue_tasks = []
        self.queues = {}

        for ws in self.websockets.values():
            try: await ws.close()
            except: pass
        self.websockets = {}
        logger.info("Streamer: Disconnected all speakers.")

    async def _speaker_queue_loop(self, sid):
        delay = self.speaker_delays.get(sid, 0.0)
        if delay > 0:
            logger.info(f"Streamer: Delay Line active for {sid} ({int(delay*1000)}ms)")
        pacer = FramePacer(extra_delay=delay)

        while self.active:
            try:
                item = await self.queues[sid].get()
                if item is None:
                    break
                data = item[1] if isinstance(item, tuple) else item
                ws = self.websockets.get(sid)
                if ws:
                    await pacer.send(ws, data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Streamer: Send failed for {sid}: {e}")

    async def broadcast(self, aac_data):
        if not self.active:
            return
        for sid in self.speaker_ids:
            if sid in self.queues:
                self.queues[sid].put_nowait(aac_data)

# --- TCP VOIP BRIDGE ---
class TCPVoIPBridge:
    def __init__(self):
        self.regs = {}
        self.allowed = _console_addrs()
        self.streamer = None
        self.call_active = False
        self.active_call_id = None
        self.rtp_task = None
        self.audio_task = None
        self.ffmpeg_proc = None
        self.udp_sock = None

    def setup_udp(self):
        if self.udp_sock:
            try: self.udp_sock.close()
            except: pass
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_sock.bind((MY_IP, RTP_PORT))
        self.udp_sock.setblocking(False)
        logger.info(f"VoIP: UDP Socket bound to {MY_IP}:{RTP_PORT}")

    async def register_extension(self, extension, password):
        while True:
            try:
                logger.info(f"VoIP [{extension}]: Connecting to {SIP_SERVER}...")
                reader, writer = await asyncio.open_connection(SIP_SERVER, SIP_PORT)
                
                branch = f"z9hG4bK-{int(time.time())}-{extension}"
                call_id = f"call-{int(time.time())}-{extension}@{MY_IP}"
                
                msg = (
                    f"REGISTER sip:{SIP_SERVER} SIP/2.0\r\n"
                    f"Via: SIP/2.0/TCP {MY_IP}:5060;branch={branch}\r\n"
                    f"From: <sip:{extension}@{SIP_SERVER}>;tag=ag-{extension}-tag\r\n"
                    f"To: <sip:{extension}@{SIP_SERVER}>\r\n"
                    f"Call-ID: {call_id}\r\n"
                    f"CSeq: 1 REGISTER\r\n"
                    f"Contact: <sip:{extension}@{MY_IP}:5060;transport=tcp>\r\n"
                    f"Max-Forwards: 70\r\n"
                    f"Expires: 60\r\n"
                    f"Content-Length: 0\r\n\r\n"
                )
                writer.write(msg.encode())
                await writer.drain()
                
                resp_text = (await reader.read(4096)).decode()
                if "401 Unauthorized" in resp_text:
                    auth_match = re.search(r'WWW-Authenticate:\s*Digest (.*)', resp_text, re.I)
                    if auth_match:
                        auth_params = auth_match.group(1)
                        realm = re.search(r'realm="(.*?)"', auth_params).group(1)
                        nonce = re.search(r'nonce="(.*?)"', auth_params).group(1)
                        qop_match = re.search(r'qop="(.*?)"', auth_params)
                        qop = qop_match.group(1) if qop_match else None
                        
                        cnonce = f"agcnonce{extension}"
                        nc = "00000001"
                        uri = f"sip:{SIP_SERVER}"
                        
                        HA1 = hashlib.md5(f"{extension}:{realm}:{password}".encode()).hexdigest()
                        HA2 = hashlib.md5(f"REGISTER:{uri}".encode()).hexdigest()
                        if qop:
                            response = hashlib.md5(f"{HA1}:{nonce}:{nc}:{cnonce}:{qop}:{HA2}".encode()).hexdigest()
                        else:
                            response = hashlib.md5(f"{HA1}:{nonce}:{HA2}".encode()).hexdigest()
                        
                        auth_header = (
                            f'Digest username="{extension}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
                            f'response="{response}", algorithm=MD5'
                        )
                        if qop:
                            auth_header += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
                        
                        msg2 = (
                            f"REGISTER sip:{SIP_SERVER} SIP/2.0\r\n"
                            f"Via: SIP/2.0/TCP {MY_IP}:5060;branch={branch}\r\n"
                            f"From: <sip:{extension}@{SIP_SERVER}>;tag=ag-{extension}-tag\r\n"
                            f"To: <sip:{extension}@{SIP_SERVER}>\r\n"
                            f"Call-ID: {call_id}\r\n"
                            f"CSeq: 2 REGISTER\r\n"
                            f"Contact: <sip:{extension}@{MY_IP}:5060;transport=tcp>\r\n"
                            f"Authorization: {auth_header}\r\n"
                            f"Max-Forwards: 70\r\n"
                            f"Expires: 60\r\n"
                            f"Content-Length: 0\r\n\r\n"
                        )
                        writer.write(msg2.encode())
                        await writer.drain()
                        
                        resp2 = await reader.read(4096)
                        if b"200 OK" in resp2:
                            logger.info(f"VoIP [{extension}]: Registration SUCCESSFUL.")
                            self.regs[extension] = time.time()
                            _write_status(self.regs)
                            # Maintain registration line listen loop
                            asyncio.create_task(self.listen_to_conn(reader, writer))
                            await asyncio.sleep(45)
                            continue
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"VoIP [{extension}]: Error in registration: {e}")
                await asyncio.sleep(10)

    async def start_server(self):
        logger.info(f"VoIP: Starting local TCP SIP server on {MY_IP}:5060...")
        server = await asyncio.start_server(self.handle_incoming_conn, MY_IP, 5060)
        async with server: await server.serve_forever()

    async def handle_incoming_conn(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("",))[0]
        if peer.startswith("::ffff:"):
            peer = peer[7:]
        if peer not in self.allowed:
            await self.reject_conn(reader, writer, peer)
            return
        await self.listen_to_conn(reader, writer)

    async def reject_conn(self, reader, writer, peer):
        """SIP from anything but the console: 403 Forbidden, then close."""
        try:
            data = await asyncio.wait_for(reader.read(4096), 5)
            msg = data.decode(errors="replace")
            first = msg.split("\r\n", 1)[0]
            logger.warning(f"VoIP: refused SIP from {peer} (not the console): {first[:60]}")
            if msg and not msg.startswith("SIP/2.0"):
                hdrs = "".join(
                    f"{name}: {val}\r\n"
                    for name, val in (("Via", get_header(msg, "Via", "v")),
                                      ("From", get_header(msg, "From", "f")),
                                      ("To", get_header(msg, "To", "t")),
                                      ("Call-ID", get_header(msg, "Call-ID", "i")),
                                      ("CSeq", get_header(msg, "CSeq", "")))
                    if val
                )
                writer.write(f"SIP/2.0 403 Forbidden\r\n{hdrs}Content-Length: 0\r\n\r\n".encode())
                await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass

    async def listen_to_conn(self, reader, writer):
        while True:
            try:
                data = await reader.read(4096)
                if not data: break
                msg = data.decode()
                
                if msg.startswith("INVITE"): 
                    asyncio.create_task(self.handle_invite(msg, writer))
                elif msg.startswith("ACK"): 
                    logger.info(f"VoIP: Call Confirmed (ID: {self.active_call_id}).")
                elif msg.startswith("BYE"):
                    logger.info("VoIP: Call ended by remote.")
                    writer.write(b"SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    await self.stop_audio_bridge()
                elif msg.startswith("OPTIONS"):
                    writer.write(b"SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
            except: break

    async def handle_invite(self, msg, writer):
        call_id = get_header(msg, "Call-ID", "i")
        via_h = get_header(msg, "Via", "v")
        from_h = get_header(msg, "From", "f")
        to_h = get_header(msg, "To", "t")
        cseq = get_header(msg, "CSeq", "")

        # 1. Busy-State Check (If system is currently in use by another call)
        if self.call_active:
            if call_id == self.active_call_id:
                # Re-INVITE support (e.g. Hold/Unhold)
                logger.info("VoIP: Re-INVITE handled.")
                sdp = (f"v=0\r\no=- {int(time.time())} {int(time.time())} IN IP4 {MY_IP}\r\ns=VoIPtoHorns\r\nc=IN IP4 {MY_IP}\r\nt=0 0\r\nm=audio {RTP_PORT} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=recvonly\r\n")
                ok = (f"SIP/2.0 200 OK\r\nVia: {via_h}\r\nFrom: {from_h}\r\nTo: {to_h}\r\n"
                      f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}")
                writer.write(ok.encode())
                await writer.drain()
                return
            else:
                logger.info(f"VoIP: System is busy. Rejecting incoming call (Call-ID: {call_id})")
                busy = (f"SIP/2.0 486 Busy Here\r\nVia: {via_h}\r\nFrom: {from_h}\r\nTo: {to_h};tag=busy-tag\r\n"
                        f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n")
                writer.write(busy.encode())
                await writer.drain()
                return

        # 2. Extract which extension number was dialed (the zone extension)
        dialed_number = None
        match = re.search(r'sip:(.*?)@', to_h)
        if match:
            dialed_number = match.group(1)
        
        if dialed_number in NO_HORNS:
            logger.warning(f"VoIP: Zone '{dialed_number}' has no horns. Rejecting with 480.")
            unavail = (f"SIP/2.0 480 Temporarily Unavailable\r\nVia: {via_h}\r\nFrom: {from_h}\r\nTo: {to_h};tag=err-tag\r\n"
                       f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n")
            writer.write(unavail.encode())
            await writer.drain()
            return

        # Fallback to verify against config mapping
        if dialed_number not in EXTENSIONS:
            logger.warning(f"VoIP: Dialed number '{dialed_number}' not recognized in config. Rejecting.")
            not_found = (f"SIP/2.0 404 Not Found\r\nVia: {via_h}\r\nFrom: {from_h}\r\nTo: {to_h};tag=err-tag\r\n"
                         f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n")
            writer.write(not_found.encode())
            await writer.drain()
            return

        self.call_active = True
        self.active_call_id = call_id
        
        # 3. Route to the correct speaker zone
        target_speakers = EXTENSIONS[dialed_number]["speakers"]
        logger.info(f"VoIP: Incoming call to '{EXTENSIONS[dialed_number]['name']}' ({dialed_number})")
        logger.info(f"VoIP: Connecting speakers for this group: {target_speakers}")
        
        self.streamer.speaker_ids = target_speakers
        await self.streamer.connect_all()

        sdp = (f"v=0\r\no=- {int(time.time())} {int(time.time())} IN IP4 {MY_IP}\r\ns=VoIPtoHorns\r\nc=IN IP4 {MY_IP}\r\nt=0 0\r\nm=audio {RTP_PORT} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=recvonly\r\n")
        to_tag = ";tag=ag-inv-tag" if ";tag=" not in to_h else ""
        ok = (f"SIP/2.0 200 OK\r\nVia: {via_h}\r\nFrom: {from_h}\r\nTo: {to_h}{to_tag}\r\n"
              f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\nContact: <sip:{dialed_number}@{MY_IP}:5060;transport=tcp>\r\n"
              f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}")
        writer.write(ok.encode())
        await writer.drain()

        # Start the audio pipeline
        asyncio.create_task(self.start_audio_bridge())

    async def start_audio_bridge(self):
        ffmpeg_cmd = ["ffmpeg"] + list(FFMPEG_MULAW8_TO_ADTS)
        self.ffmpeg_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        loop = asyncio.get_event_loop()
        
        async def rtp_receiver():
            while self.call_active:
                try:
                    data = await loop.sock_recv(self.udp_sock, 2048)
                    if not data: break
                    if self.ffmpeg_proc and self.ffmpeg_proc.stdin:
                        self.ffmpeg_proc.stdin.write(data[12:])
                        await self.ffmpeg_proc.stdin.drain()
                except asyncio.CancelledError:
                    break
                except: 
                    break
            if self.ffmpeg_proc and self.ffmpeg_proc.stdin: self.ffmpeg_proc.stdin.close()

        async def audio_broadcaster():
            logger.info("VoIP: Real-Time Broadcaster Active.")
            frames = 0
            while self.call_active:
                try:
                    header = await self.ffmpeg_proc.stdout.readexactly(7)
                    frame_length = ((header[3] & 3) << 11) | (header[4] << 3) | ((header[5] & 0xe0) >> 5)
                    payload = await self.ffmpeg_proc.stdout.readexactly(frame_length - 7)
                    await self.streamer.broadcast(header + payload)
                    frames += 1
                    if frames % 100 == 0: logger.info(f"Streamer: Sent {frames} frames...")
                except asyncio.CancelledError:
                    break
                except: 
                    break
            if self.ffmpeg_proc: self.ffmpeg_proc.terminate()

        self.rtp_task = asyncio.create_task(rtp_receiver())
        self.audio_task = asyncio.create_task(audio_broadcaster())

    async def stop_audio_bridge(self):
        logger.info("VoIP: Stopping audio bridge and cleaning tasks...")
        self.call_active = False
        self.active_call_id = None
        
        if self.rtp_task:
            self.rtp_task.cancel()
            self.rtp_task = None
        if self.audio_task:
            self.audio_task.cancel()
            self.audio_task = None
            
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.stdin.close()
                self.ffmpeg_proc.terminate()
            except: pass
            self.ffmpeg_proc = None

        await asyncio.sleep(TAIL_HOLD)
        await self.streamer.disconnect_all()
        logger.info("VoIP: Audio bridge stopped cleanly.")

async def _idle_forever():
    while True:
        await asyncio.sleep(3600)


async def main():
    if not UNIFI_IP:
        logger.error("UNIFI_IP is not set in .env. Set the UniFi console address "
                     "(dashboard Config or .env), then restart voiptohorns-bridge. Idle until then.")
        await _idle_forever()
    if not MY_IP:
        logger.error(f"MY_IP is not set and could not be detected toward {SIP_SERVER}. "
                     "Set MY_IP in .env to this Pi's LAN address. Idle until then.")
        await _idle_forever()
    if not EXTENSIONS:
        logger.info("No zones configured yet (extensions.json missing or empty). "
                    "Create a zone in the dashboard. SIP server stays up, nothing registers.")
    logger.info(f"Config: UNIFI_IP={UNIFI_IP} SIP_SERVER={SIP_SERVER}:{SIP_PORT} MY_IP={MY_IP} zones={len(EXTENSIONS)}")
    bridge = TCPVoIPBridge()
    _write_status({})  # nothing registered yet by this process
    logger.info(f"SIP accepted only from the console: {', '.join(sorted(bridge.allowed)) or 'none'}")
    bridge.setup_udp()
    bridge.streamer = ProtectStreamer(UNIFI_IP, SPEAKER_DELAYS)
    
    # Register all extensions concurrently
    registration_tasks = [
        bridge.register_extension(ext, info["password"]) 
        for ext, info in EXTENSIONS.items()
    ]
    
    await asyncio.gather(
        bridge.start_server(),
        *registration_tasks
    )

if __name__ == "__main__":
    asyncio.run(main())
