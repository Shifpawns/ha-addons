#!/usr/bin/env python3
import json
import urllib.request
import os
import select
import socket
import struct
import subprocess
import threading
import time

import paho.mqtt.client as mqtt

from wvenc import Encoder

HERE = os.path.dirname(os.path.abspath(__file__))

SPEAKER = os.environ.get("SPEAKER_IP", "192.168.1.9")
NAME = os.environ.get("SPEAKER_NAME", "Omni 10")
NODE = os.environ.get("NODE_ID", "omni10")
MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
LMS_SERVER = os.environ.get("LMS_SERVER", "")
PLAYER_MAC = os.environ.get("PLAYER_MAC", "02:38:29:1e:0b:0d")
BITS = float(os.environ.get("BITS_PER_SAMPLE", "2.5"))

SPEAKER_PORT = 6060
LOCAL_PORT = 6080
MCAST = "224.0.1.186"
TICKS_PER_SEC = 12_288_000
FRAME = 6000
FRAME_SEC = FRAME / 48000
FRAME_BYTES = FRAME * 4
LEAD = 1.5
DATA_BYTES = 1316
IDLE_STOP = 2.0
GAP = bytes(256)
START_DELAY = 1.5
RESYNC_AFTER = 1.0
OFFLINE_AFTER = 90

AUDIO_CHECK = {
    1422: 0xF7DAA589, 1424: 0x870FE01A, 1426: 0x15152F00, 1428: 0xA7FB6399,
    1432: 0xC6E6E71C, 1434: 0x54FC2806, 1438: 0x7408AB85, 1440: 0x07B8EFB9,
    1442: 0x95A220A3, 1444: 0x274C6C3A, 1446: 0xB556A320, 1448: 0x4651E8BF,
    1450: 0xD44B27A5, 1452: 0x66A56B3C,
}

OLD_IP = socket.inet_aton("192.168.1.205")
OLD_SID = 0x6A94A863

VOLUME_TEMPLATE = bytes.fromhex(
    "a4000000050000000400000017c00000c0a801cd17ac0000c0a80109c0a801cd"
    "4c26ba6a00000000000000000000000000000000a254c4500100000000000000"
    "0f00000000000000000000000c6a00000000000063a8946a00000000ffffffff"
    "02000000ff000000000000001e001e001e001e001e001e001e001e0000000000"
    "00000000000000000000000000000000000000000000000000000000c0a801cd"
    "00000000"
)

BASE = f"omni/{NODE}"
T_BRIDGE = f"{BASE}/bridge"
T_AVAIL = f"{BASE}/available"
T_VOLUME = f"{BASE}/volume"
T_VOLUME_SET = f"{BASE}/volume/set"
T_PLAYING = f"{BASE}/playing"

KNOWN = {(1, 17), (1, 44), (1, 46), (3, 12), (3, 39), (4, 9), (6, 1), (6, 3), (6, 23), (6, 34), (6, 40), (6, 47)}
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
MA_PLAYER = os.environ.get("MA_PLAYER", "")


def log(*args):
    print(*args, flush=True)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((SPEAKER, SPEAKER_PORT))
    ip = s.getsockname()[0]
    s.close()
    return ip


def now_us():
    return int(time.monotonic() * 1_000_000) & 0xFFFFFFFF


def word(buf, i):
    return struct.unpack_from("<I", buf, i * 4)[0]


def put(buf, i, value):
    struct.pack_into("<I", buf, i * 4, value & 0xFFFFFFFF)


def multicast_socket(bind_host, port, ip):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, port))
    mreq = struct.pack("4s4s", socket.inet_aton(MCAST), socket.inet_aton(ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return sock


class Speaker:
    def __init__(self):
        self.ip = local_ip()
        self.my_ip = socket.inet_aton(self.ip)
        self.seq = int(time.time()) & 0xFFFF
        self.last_seen = 0.0
        self.volume = None
        self.session = None
        self.on_volume = None
        self.flushed = threading.Event()
        self.seen = set()
        self.ma_volume = None
        self.ignore_ma_until = 0.0
        self.on_button = None
        self.paused_at = 0.0
        self.played_at = 0.0
        self.on_ma_volume = None
        self.sock = multicast_socket("", LOCAL_PORT, self.ip)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, self.my_ip)
        self.heartbeat = multicast_socket(MCAST, SPEAKER_PORT, self.ip)
        threading.Thread(target=self.listen, daemon=True).start()
        threading.Thread(target=self.watch_heartbeat, daemon=True).start()

    def send(self, buf, multicast=False):
        self.sock.sendto(bytes(buf), (MCAST if multicast else SPEAKER, SPEAKER_PORT))

    def set_volume(self, volume):
        volume = max(0, min(50, int(volume)))
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        buf = bytearray(VOLUME_TEMPLATE)
        for i in (4, 7, 39):
            buf[i * 4 : i * 4 + 4] = self.my_ip
        put(buf, 8, now_us())
        put(buf, 19, self.seq)
        for i in range(27, 31):
            put(buf, i, volume | (volume << 16))
        self.send(buf)

    def watch_heartbeat(self):
        while True:
            pkt, addr = self.heartbeat.recvfrom(4096)
            if addr[0] == SPEAKER:
                self.last_seen = time.time()

    def listen(self):
        while True:
            pkt, addr = self.sock.recvfrom(4096)
            if addr[0] != SPEAKER or len(pkt) < 72:
                continue
            self.last_seen = time.time()
            key = (word(pkt, 14), word(pkt, 16))
            if key in ((1, 17), (1, 46)) and len(pkt) >= 112:
                volume = word(pkt, 27) & 0xFFFF
                if volume != self.volume:
                    self.volume = volume
                    if self.on_volume:
                        self.on_volume(volume)
            session = self.session
            if session is not None:
                session.handle(key, pkt)
            if key in ((6, 23), (6, 40), (1, 44)):
                log(f"speaker meldt {key}, sessie {'actief' if session and session.running else 'uit'}")
            if key == (6, 23) and session is not None and session.running and not session.stopped_by_speaker:
                session.stopped_by_speaker = True
                self.paused_at = time.monotonic()
                if self.on_button:
                    self.on_button("media_pause")
            elif key == (6, 40) and session is None and time.monotonic() - self.paused_at > 3:
                if time.monotonic() - self.played_at > 3:
                    self.played_at = time.monotonic()
                    if self.on_button:
                        self.on_button("media_play")
            if key not in KNOWN and key not in self.seen:
                self.seen.add(key)
                log(f"onbekend pakket {key} van speaker, {len(pkt)} bytes: {pkt[72:160].hex()}")


class Session:
    def __init__(self, speaker, templates):
        self.speaker = speaker
        self.templates = dict(templates)
        self.templates["audio_head"] = self.templates["audio"][:132]
        self.sid = now_us()
        self.seq = self.sid & 0xFFFF
        self.lock = threading.Lock()
        self.clock = None
        self.clock_at = 0.0
        self.events = {k: threading.Event() for k in ((6, 3), (6, 34), (3, 39))}
        self.sent_audio = {}
        self.resent = {}
        self.encoder = Encoder(bits_per_sample=BITS)
        self.running = False
        self.frame_no = 0
        self.counter = 0
        self.data = bytearray()
        self.frame_off = None
        self.last_off = None
        self.packet_pos = 1
        self.frame_pos = 1
        self.marked = None
        self.late = 0
        self.stopped_by_speaker = False
        self.behind = 0.0
        self.resend_count = 0
        self.last_sent = 0.0
        self.play_at = 0.0
        self.stream_start = 0.0

    def next_seq(self):
        with self.lock:
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            return self.seq

    def speaker_now(self):
        if self.clock is None:
            return None
        return (self.clock + int((time.monotonic() - self.clock_at) * TICKS_PER_SEC)) & 0xFFFFFFFF

    def handle(self, key, pkt):
        if key == (3, 12) and len(pkt) >= 128:
            self.clock = struct.unpack_from("<I", pkt, 124)[0]
            self.clock_at = time.monotonic()
        elif key == (4, 9) and len(pkt) >= 156:
            self.resend(pkt)
        if key in self.events:
            self.events[key].set()

    def resend(self, pkt):
        missing = min(word(pkt, 36), (len(pkt) // 4 - 38) // 2)
        now = time.monotonic()
        for i in range(missing):
            counter = word(pkt, 38 + 2 * i)
            buf = self.sent_audio.get(counter)
            if buf is None or now - self.resent.get(counter, 0) < 0.15:
                continue
            self.resent[counter] = now
            fresh = bytearray(buf)
            put(fresh, 19, self.next_seq())
            put(fresh, 8, now_us())
            self.speaker.send(fresh)
            self.resend_count += 1

    def build(self, name):
        buf = bytearray(self.templates[name])
        if buf[16:20] == OLD_IP:
            buf[16:20] = self.speaker.my_ip
        if buf[28:32] == OLD_IP:
            buf[28:32] = self.speaker.my_ip
        for i in range(18, min(len(buf) // 4, 300)):
            w = word(buf, i)
            if w == OLD_SID:
                put(buf, i, self.sid)
            elif w == OLD_SID + 3:
                put(buf, i, self.sid + 3)
        put(buf, 8, now_us())
        return buf

    def message(self, name, copies=1):
        buf = self.build(name)
        put(buf, 19, self.next_seq())
        for _ in range(copies):
            self.speaker.send(buf)

    def sync_burst(self, round_no, count=11):
        for _ in range(count):
            buf = self.build("sync")
            put(buf, 19, self.next_seq())
            buf[100] = round_no & 0xFF
            struct.pack_into("<I", buf, 101, now_us())
            self.speaker.send(buf, multicast=True)
            self.speaker.send(buf)
            time.sleep(0.012)

    def open(self):
        for _ in range(3):
            self.message("disc", copies=2)
            if self.events[(6, 3)].wait(1.0):
                break
        else:
            log("speaker reageert niet op de sessie-aanvraag")
            return False
        self.message("disc2", copies=2)
        self.events[(6, 34)].wait(1.0)
        self.message("cfg0")
        time.sleep(0.07)
        self.sync_burst(0, 1)
        self.sync_burst(1, 12)
        self.message("keep")
        for _ in range(4):
            time.sleep(0.25)
            self.message("cfg0")
        self.sync_burst(2, 8)
        if self.speaker_now() is None:
            self.sync_burst(2, 8)
        if self.speaker_now() is None:
            log("geen klok van de speaker ontvangen")
            return False
        for _ in range(4):
            check = self.build("check")
            put(check, 19, self.next_seq())
            put(check, 8, self.speaker_now())
            self.speaker.send(check)
            if self.events[(3, 39)].wait(0.15):
                break
        self.message("rate", copies=2)
        self.sync_burst(2, 3)
        self.start_clock()
        self.running = True
        threading.Thread(target=self.housekeeping, daemon=True).start()
        return True

    def start_clock(self):
        spk = self.speaker_now()
        cfg = self.build("cfg1")
        put(cfg, 19, self.next_seq())
        put(cfg, 27, spk)
        put(cfg, 29, spk + int(START_DELAY * TICKS_PER_SEC))
        self.speaker.send(cfg)
        self.speaker.send(cfg)
        self.play_at = time.monotonic() + START_DELAY
        self.stream_start = time.monotonic()

    def housekeeping(self):
        round_no = 3
        while self.running:
            time.sleep(4.1)
            if not self.running:
                break
            self.sync_burst(round_no)
            round_no += 1
            self.message("keep")

    def flush(self):
        self.counter += 1
        marker = (self.frame_off << 8) | 1 if self.frame_off is not None else 0
        data = self.data
        if 136 + len(data) not in AUDIO_CHECK:
            data = data.ljust(DATA_BYTES, b"\0")
        buf = self.build("audio_head") + struct.pack("<I", marker) + data
        put(buf, 0, len(buf))
        put(buf, 13, AUDIO_CHECK[len(buf)])
        put(buf, 19, self.next_seq())
        second_mark = (self.packet_pos - 1) % 48000 == 0 and self.packet_pos != self.marked
        if second_mark:
            self.marked = self.packet_pos
        put(buf, 24, 0x0A | (0x04000000 if second_mark else 0))
        put(buf, 25, 2)
        put(buf, 26, self.counter)
        put(buf, 27, 0)
        put(buf, 28, self.packet_pos)
        put(buf, 29, self.packet_pos >> 32)
        wait = self.last_sent + 0.002 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.speaker.send(buf)
        self.last_sent = time.monotonic()
        self.sent_audio[self.counter] = bytes(buf)
        self.sent_audio.pop(self.counter - 3000, None)
        self.resent.pop(self.counter - 3000, None)
        self.data = bytearray()
        self.frame_off = None
        self.last_off = None
        self.packet_pos = self.frame_pos

    def write(self, data):
        while data:
            space = DATA_BYTES - len(self.data)
            self.data += data[:space]
            data = data[space:]
            if len(self.data) == DATA_BYTES:
                self.flush()

    def start_frame(self, pos):
        room = DATA_BYTES - len(self.data)
        if self.data and room <= 31 and (136 + len(self.data)) in AUDIO_CHECK:
            self.flush()
        if not self.data:
            self.packet_pos = pos
        offset = len(self.data)
        if self.frame_off is None:
            self.frame_off = offset
        else:
            struct.pack_into("<I", self.data, self.last_off, offset)
        self.last_off = offset
        self.frame_pos = pos
        self.write(struct.pack("<IQ", 0, pos))

    def push(self, pcm, last=False):
        due = max(
            self.play_at + self.frame_no * FRAME_SEC - LEAD,
            self.stream_start + self.frame_no * FRAME_SEC / 4,
        )
        delay = due - time.monotonic()
        if delay > 0 and self.speaker.flushed.wait(delay):
            return
        late = time.monotonic() - (self.play_at + self.frame_no * FRAME_SEC - 0.2)
        self.behind = late
        if late > 0 and not last:
            self.late += 1
            if self.late % 20 == 1:
                log(f"audio komt te laat binnen ({self.late}x, {late:.2f} s)")
        self.start_frame(self.frame_no * FRAME + 1)
        for block in self.encoder.encode(pcm):
            self.write(block)
        self.frame_no += 1

    def close(self, wait=True):
        if self.data:
            self.flush()
        end = self.play_at + self.frame_no * FRAME_SEC + 0.3
        while wait and time.monotonic() < end:
            time.sleep(0.05)
        self.running = False
        self.message("stop", copies=2)


def session_stats(session):
    return f"{session.frame_no} frames, {session.late}x te laat, {session.resend_count} pakketten opnieuw"


def squeezelite_command():
    cmd = ["squeezelite", "-d", "slimproto=debug", "-n", NAME, "-m", PLAYER_MAC, "-o", "-", "-a", "16", "-r", "48000-48000", "-R", "hLE"]
    if LMS_SERVER:
        cmd += ["-s", LMS_SERVER]
    return cmd


def strip_silence(data):
    out = bytearray()
    i = 0
    while True:
        p = data.find(GAP, i)
        if p < 0:
            out += data[i:]
            return bytes(out)
        start = (p + 3) & ~3
        rest = data[start:]
        end = (start + len(rest) - len(rest.lstrip(b"\0"))) & ~3
        out += data[i:start]
        i = end


def volume_to_gain(volume):
    if volume <= 0:
        return 0
    if volume > 25:
        db = (volume - 100) * 37 / 75
    else:
        db = volume * 37 / 25 - 74
    mult = 10 ** (db / 20)
    if db >= -30:
        return int(mult * 256 + 0.5) * 256
    return int(mult * 65536 + 0.5)


GAINS = [volume_to_gain(v) for v in range(101)]


def gain_to_volume(gain):
    return min(range(101), key=lambda v: abs(GAINS[v] - gain))


def watch_squeezelite(stream, speaker):
    for line in stream:
        line = line.decode(errors="replace").strip()
        if "audg gainL:" in line:
            parts = line.split()
            gain = int(parts[parts.index("gainL:") + 1])
            adjust = parts[parts.index("adjust:") + 1] != "0"
            if adjust and speaker.on_ma_volume:
                speaker.on_ma_volume(gain_to_volume(gain))
        elif "strm command" in line:
            command = line[-1]
            if command in "qfp":
                speaker.flushed.set()
        elif "error" in line.lower() or "warn" in line.lower():
            log("squeezelite:", line)


def drain(fd):
    while select.select([fd], [], [], 0)[0]:
        if not os.read(fd, 65536):
            return


def player(speaker, templates, client):
    while True:
        log("squeezelite starten:", " ".join(squeezelite_command()))
        speaker.ignore_ma_until = time.monotonic() + 10
        proc = subprocess.Popen(squeezelite_command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=watch_squeezelite, args=(proc.stderr, speaker), daemon=True).start()
        fd = proc.stdout.fileno()
        raw = bytearray()
        buf = bytearray()
        session = None
        silent_since = None
        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            if speaker.flushed.is_set():
                speaker.flushed.clear()
                drain(fd)
                raw.clear()
                buf.clear()
                if session is not None:
                    log(f"overgeslagen of gepauzeerd, {session_stats(session)}")
                    session.close(wait=False)
                    speaker.session = None
                    session = None
                    client.publish(T_PLAYING, "OFF", retain=True)
                continue
            raw += chunk
            usable = len(raw) & ~3
            audio = strip_silence(bytes(raw[:usable]))
            del raw[:usable]
            got_audio = any(audio)
            buf += audio
            now = time.monotonic()
            if got_audio:
                silent_since = None
            elif silent_since is None:
                silent_since = now
            if session is None and buf:
                session = Session(speaker, templates)
                speaker.session = session
                if not session.open():
                    speaker.session = None
                    session = None
                    buf.clear()
                    time.sleep(2)
                    continue
                log("afspelen gestart")
                client.publish(T_PLAYING, "ON", retain=True)
            while session is not None and len(buf) >= FRAME_BYTES and not speaker.flushed.is_set():
                session.push(bytes(buf[:FRAME_BYTES]))
                del buf[:FRAME_BYTES]
                if session.behind > RESYNC_AFTER:
                    log(f"te ver achter, sessie opnieuw starten, {session_stats(session)}")
                    session.close(wait=False)
                    speaker.session = None
                    session = None
                    time.sleep(0.5)
            if session is not None and silent_since is not None and now - silent_since > IDLE_STOP:
                if buf:
                    session.push(bytes(buf.ljust(FRAME_BYTES, b"\0")), last=True)
                    buf.clear()
                session.close()
                log(f"afspelen gestopt, {session_stats(session)}")
                speaker.session = None
                session = None
                client.publish(T_PLAYING, "OFF", retain=True)
            if not got_audio:
                time.sleep(0.02)
        if session is not None:
            session.close()
            speaker.session = None
            client.publish(T_PLAYING, "OFF", retain=True)
        proc.wait()
        log("squeezelite gestopt, opnieuw over 3 s")
        time.sleep(3)


def ha_request(path, data=None):
    req = urllib.request.Request(
        f"http://supervisor/core/api/{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def find_ma_player():
    if MA_PLAYER:
        return MA_PLAYER
    try:
        for state in ha_request("states"):
            attrs = state.get("attributes", {})
            if state["entity_id"].startswith("media_player.") and "mass_player_type" in attrs and attrs.get("friendly_name") == NAME:
                return state["entity_id"]
    except Exception as err:
        log("HA niet bereikbaar:", err)
    return None


def ma_service(service, data=None):
    entity = find_ma_player()
    if not entity:
        log("geen Music Assistant-speler gevonden in HA")
        return
    try:
        ha_request(f"services/media_player/{service}", {"entity_id": entity, **(data or {})})
    except Exception as err:
        log(f"{service} naar MA mislukt:", err)


def discovery(client):
    device = {
        "identifiers": [f"omni_{NODE}"],
        "name": NAME,
        "manufacturer": "Harman Kardon",
        "model": "Omni 10",
    }
    availability = [{"topic": T_BRIDGE}, {"topic": T_AVAIL}]
    volume = {
        "name": "Volume",
        "unique_id": f"{NODE}_volume",
        "command_topic": T_VOLUME_SET,
        "state_topic": T_VOLUME,
        "min": 0,
        "max": 50,
        "step": 1,
        "mode": "slider",
        "icon": "mdi:speaker",
        "availability": availability,
        "availability_mode": "all",
        "device": device,
    }
    playing = {
        "name": "Speelt af",
        "unique_id": f"{NODE}_playing",
        "state_topic": T_PLAYING,
        "icon": "mdi:music",
        "availability": availability,
        "availability_mode": "all",
        "device": device,
    }
    client.publish(f"homeassistant/number/{NODE}_volume/config", json.dumps(volume), retain=True)
    client.publish(f"homeassistant/binary_sensor/{NODE}_playing/config", json.dumps(playing), retain=True)
    client.publish(f"homeassistant/event/{NODE}_link/config", "", retain=True)


def main():
    templates = {k: bytes.fromhex(v) for k, v in json.load(open(os.path.join(HERE, "templates.json"))).items()}
    speaker = Speaker()
    log(f"add-on op {speaker.ip}, speaker {SPEAKER}, mqtt {MQTT_HOST}:{MQTT_PORT}")

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"omni-bridge-{NODE}")
    except AttributeError:
        client = mqtt.Client(client_id=f"omni-bridge-{NODE}")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(T_BRIDGE, "offline", retain=True)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        log("mqtt verbonden")
        client.publish(T_BRIDGE, "online", retain=True)
        client.publish(T_PLAYING, "OFF", retain=True)
        discovery(client)
        client.subscribe(T_VOLUME_SET)

    def on_message(client, userdata, msg):
        try:
            value = round(float(msg.payload.decode()))
        except ValueError:
            return
        log("volume ->", value)
        speaker.set_volume(value)

    def on_volume(volume):
        client.publish(T_VOLUME, str(volume), retain=True)
        if volume != speaker.ma_volume:
            speaker.ma_volume = volume
            speaker.ignore_ma_until = max(speaker.ignore_ma_until, time.monotonic() + 1.5)
            threading.Thread(target=ma_service, args=("volume_set", {"volume_level": volume / 50}), daemon=True).start()

    def on_ma_volume(volume):
        if time.monotonic() < speaker.ignore_ma_until:
            return
        target = (volume + 1) // 2
        speaker.ma_volume = target
        if target != speaker.volume:
            log("volume uit MA ->", target)
            speaker.set_volume(target)

    def on_button(service):
        log("knop op speaker ->", service)
        threading.Thread(target=ma_service, args=(service,), daemon=True).start()

    speaker.on_ma_volume = on_ma_volume
    speaker.on_button = on_button

    speaker.on_volume = on_volume
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    def watchdog():
        state = None
        while True:
            online = time.time() - speaker.last_seen < OFFLINE_AFTER
            if online != state:
                state = online
                client.publish(T_AVAIL, "online" if online else "offline", retain=True)
                log("speaker", "online" if online else "offline")
            time.sleep(5)

    threading.Thread(target=watchdog, daemon=True).start()
    player(speaker, templates, client)


if __name__ == "__main__":
    main()
