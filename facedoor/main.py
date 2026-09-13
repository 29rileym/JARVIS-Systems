"""
Jarvis FaceDoor — FastAPI backend.

Endpoints
  GET  /                 -> serves the frontend (index.html)
  POST /recognize        -> recognize a face from a base64 frame      { image }
  POST /passcode         -> verify the fallback 4-digit passcode       { passcode }
  POST /idle             -> apply the scanning/idle light scene
  GET  /door             -> current door lock status
  POST /door/lock        -> lock the door (publishes 'lock' to jarvis/door)
  GET  /faces            -> list registered names
  GET  /config           -> non-secret config (does NOT expose token/passcode)

Door lock: when access is granted and the door is locked, the backend publishes
'unlock' to MQTT topic jarvis/door and marks the door unlocked. The UI Lock
button publishes 'lock'. Registration is local-only (see register_face.py).

Recognition responses use status: "accepted" | "denied" | "no_face".
Face recognition ONLY runs when the user is making eye contact (looking at the
camera) — this is approximated from facial landmarks (frontal-pose symmetry).

Home Assistant scenes are triggered server-side so the long-lived token never
leaves the machine.

Run:  python main.py     (listens on 0.0.0.0:5000)
"""

import base64
import io
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import face_recognition
import paho.mqtt.client as mqtt
from PIL import Image
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Paths & config
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
FACES_DIR = BASE_DIR / "faces"
CONFIG_PATH = BASE_DIR / "config.json"
INDEX_PATH = BASE_DIR / "index.html"

FACES_DIR.mkdir(exist_ok=True)

# Tolerance for face matching. LOWER = stricter (fewer matches), HIGHER = more
# lenient. 0.6 is the library default; 0.55 is a good balance for phone cameras.
# Raise toward 0.6 if you keep getting wrongly denied; lower toward 0.45 if
# strangers get let in.
MATCH_TOLERANCE = 0.55
# How often (seconds) a Home Assistant scene may be (re)triggered, to avoid
# hammering HA while a face sits in frame.
HA_COOLDOWN = 8.0

# MQTT door lock
DOOR_TOPIC = "jarvis/door"
DEFAULT_MQTT_PORT = 1883


def load_config() -> dict:
    """Read config.json fresh every time so edits apply without a restart."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[config] {CONFIG_PATH} not found — Home Assistant disabled.")
        return {}
    except json.JSONDecodeError as e:
        print(f"[config] config.json is invalid JSON: {e}")
        return {}


# --------------------------------------------------------------------------- #
# Known-face store
# --------------------------------------------------------------------------- #
known_encodings: list[np.ndarray] = []
known_names: list[str] = []


def load_known_faces() -> None:
    """Load every encoding saved in /faces (stored as <name>.npy)."""
    known_encodings.clear()
    known_names.clear()
    for npy in sorted(FACES_DIR.glob("*.npy")):
        try:
            known_encodings.append(np.load(npy))
            known_names.append(npy.stem)
        except Exception as e:  # pragma: no cover - corrupt file
            print(f"[faces] could not load {npy.name}: {e}")
    print(f"[faces] loaded {len(known_names)} known face(s): {known_names}")


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #
def decode_base64_image(data: str) -> Optional[np.ndarray]:
    """Decode a base64 (optionally data-URL) string into an RGB numpy array."""
    try:
        if "," in data and data.strip().startswith("data:"):
            data = data.split(",", 1)[1]
        raw = base64.b64decode(data)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        return np.array(img)
    except Exception as e:
        print(f"[image] decode failed: {e}")
        return None


def is_making_eye_contact(landmarks: dict) -> bool:
    """
    Approximate 'looking directly at the camera' from facial landmarks.

    True gaze tracking needs the pupils; with face_recognition landmarks we
    instead require a roughly frontal head pose, which is a good proxy: the
    nose must sit centered between the two eyes (no big left/right yaw) and the
    eyes must be roughly level (no big head tilt).
    """
    try:
        left_eye = np.mean(landmarks["left_eye"], axis=0)
        right_eye = np.mean(landmarks["right_eye"], axis=0)
        # Nose tip: prefer nose_tip, fall back to the bottom of the nose bridge.
        nose_pts = landmarks.get("nose_tip") or landmarks.get("nose_bridge")
        nose = np.mean(nose_pts, axis=0)
    except (KeyError, ValueError):
        return False

    eye_dx = right_eye[0] - left_eye[0]
    if abs(eye_dx) < 1e-3:
        return False

    # Horizontal position of nose between the eyes (0 = left eye, 1 = right eye).
    # Centered (~0.5) means the head faces the camera rather than turned away.
    ratio = (nose[0] - left_eye[0]) / eye_dx
    centered = 0.35 <= ratio <= 0.65

    # Reject large head tilt: vertical offset between eyes vs. their separation.
    eye_dy = abs(right_eye[1] - left_eye[1])
    level = eye_dy < abs(eye_dx) * 0.30

    return bool(centered and level)


# --------------------------------------------------------------------------- #
# Home Assistant
# --------------------------------------------------------------------------- #
_last_ha_trigger = {"scene": None, "at": 0.0}


def _send_entity_command(url: str, token: str, ent: dict) -> dict:
    """Send one light/scene/script command to Home Assistant. Never raises."""
    entity_id = ent.get("entity_id")
    if not entity_id:
        return {"ok": False, "error": "missing entity_id"}

    domain = entity_id.split(".", 1)[0]
    turn_off = str(ent.get("state", "")).lower() == "off" or ent.get("off") is True

    if turn_off:
        service = f"{domain}/turn_off"
        payload = {"entity_id": entity_id}
    elif domain == "select":
        # e.g. Govee DIY scenes are exposed as a select entity ("DIY Scene"
        # dropdown), not as a light effect. {"entity_id": "select.x", "option": "Blue pulse"}
        service = "select/select_option"
        payload = {"entity_id": entity_id, "option": ent.get("option")}
    elif domain in ("scene", "script"):
        service = f"{domain}/turn_on"
        payload = {"entity_id": entity_id}
    else:
        service = "light/turn_on"
        payload = {"entity_id": entity_id}
        for k in ("rgb_color", "brightness", "color_name", "effect", "color_temp", "color_temp_kelvin", "transition"):
            if k in ent:
                payload[k] = ent[k]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(f"{url}/api/services/{service}", headers=headers, json=payload, timeout=5)
        return {"entity_id": entity_id, "ok": r.ok, "code": r.status_code}
    except requests.RequestException as e:
        return {"entity_id": entity_id, "ok": False, "error": str(e)}


def trigger_ha_scene(scene_key: str) -> dict:
    """
    Trigger a light scene from config ('scanning_scene' / 'accepted_scene' /
    'denied_scene') via the HA REST API.

    Each entry is one entity. scene.*/script.* entities are turned on directly;
    anything else is treated as a light and gets rgb_color / brightness etc.

    An entry may also have a scheduled follow-up:
        "then": { ...another command for the same entity... },
        "then_after": seconds
    e.g. flash green then return to an effect:
        { "entity_id": "light.x", "rgb_color": [0,255,0],
          "then": { "effect": "Starry Sky" }, "then_after": 1 }

    Returns a small summary dict (never raises).
    """
    cfg = load_config()
    url = (cfg.get("ha_url") or "").rstrip("/")
    token = cfg.get("ha_token") or ""
    entities = cfg.get(scene_key) or []

    if not url or not token or "PASTE_YOUR" in token:
        return {"ha": "skipped", "reason": "Home Assistant not configured"}

    # Debounce repeat triggers of the same scene.
    now = time.time()
    if _last_ha_trigger["scene"] == scene_key and (now - _last_ha_trigger["at"]) < HA_COOLDOWN:
        return {"ha": "debounced"}
    _last_ha_trigger.update(scene=scene_key, at=now)

    results = []
    for ent in entities:
        if isinstance(ent, str):
            ent = {"entity_id": ent}
        if not ent.get("entity_id"):
            continue

        results.append(_send_entity_command(url, token, ent))

        # Schedule the follow-up command (e.g. revert to an effect / turn off).
        follow = ent.get("then")
        if isinstance(follow, dict):
            delay = float(ent.get("then_after", 1))
            follow_ent = {"entity_id": ent["entity_id"], **follow}
            threading.Timer(delay, _send_entity_command, args=(url, token, follow_ent)).start()

    ok = all(r.get("ok") for r in results) if results else False
    return {"ha": "ok" if ok else "partial", "scene": scene_key, "results": results}


# --------------------------------------------------------------------------- #
# MQTT door lock
# --------------------------------------------------------------------------- #
door_status = "locked"          # in-memory door state: "locked" | "unlocked"
_mqtt_client: Optional[mqtt.Client] = None
_mqtt_connected = False


def init_mqtt() -> None:
    """Connect to the MQTT broker from config (mqtt_broker, optional :port)."""
    global _mqtt_client
    cfg = load_config()
    broker = (cfg.get("mqtt_broker") or "").strip()
    if not broker:
        print("[mqtt] no 'mqtt_broker' in config — MQTT disabled")
        return

    host, port = broker, DEFAULT_MQTT_PORT
    if ":" in broker:
        host, p = broker.rsplit(":", 1)
        port = int(p) if p.isdigit() else DEFAULT_MQTT_PORT

    try:  # paho-mqtt 2.x callback API; fall back for 1.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    def on_connect(c, u, flags, rc, properties=None):
        global _mqtt_connected
        _mqtt_connected = True
        print(f"[mqtt] connected to {host}:{port}")

    def on_disconnect(c, u, *args):
        global _mqtt_connected
        _mqtt_connected = False
        print("[mqtt] disconnected")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    try:
        client.connect_async(host, port, keepalive=30)
        client.loop_start()       # background thread; auto-reconnects
        _mqtt_client = client
        print(f"[mqtt] connecting to {host}:{port} ...")
    except Exception as e:
        print(f"[mqtt] could not start client: {e}")


def publish_door(command: str) -> dict:
    """Publish 'lock'/'unlock' to jarvis/door. Never raises."""
    if _mqtt_client is None:
        return {"mqtt": "disabled"}
    try:
        info = _mqtt_client.publish(DOOR_TOPIC, command, qos=1)
        return {
            "mqtt": "published" if info.rc == mqtt.MQTT_ERR_SUCCESS else "queued",
            "topic": DOOR_TOPIC, "command": command, "connected": _mqtt_connected,
        }
    except Exception as e:
        return {"mqtt": "error", "error": str(e)}


def grant_unlock() -> dict:
    """Called when access is granted: unlock the door if it is currently locked."""
    global door_status
    if door_status == "locked":
        res = publish_door("unlock")
        door_status = "unlocked"
        return {"door_status": door_status, "door_message": "Door unlocked", **res}
    return {"door_status": door_status, "door_message": "Already unlocked"}


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_known_faces()
    init_mqtt()
    yield
    if _mqtt_client is not None:
        _mqtt_client.loop_stop()
        _mqtt_client.disconnect()


app = FastAPI(title="Jarvis FaceDoor", lifespan=lifespan)


class FrameRequest(BaseModel):
    image: str  # base64 / data URL


class PasscodeRequest(BaseModel):
    passcode: str


@app.get("/")
def index():
    if INDEX_PATH.exists():
        return FileResponse(INDEX_PATH)
    return JSONResponse({"error": "index.html not found"}, status_code=404)


@app.get("/faces")
def list_faces():
    return {"names": known_names, "count": len(known_names)}


@app.get("/config")
def public_config():
    """Expose only non-secret bits the frontend may want."""
    cfg = load_config()
    return {
        "ha_configured": bool(cfg.get("ha_token") and "PASTE_YOUR" not in cfg.get("ha_token", "")),
        "passcode_len": len(str(cfg.get("passcode", "1234"))),
    }


@app.get("/door")
def door_state():
    """Current door lock status (for the UI on load)."""
    return {"door_status": door_status, "mqtt_connected": _mqtt_connected}


@app.post("/door/lock")
def door_lock():
    """Lock the door if it is currently unlocked (the UI Lock button)."""
    global door_status
    if door_status == "unlocked":
        res = publish_door("lock")
        door_status = "locked"
        return {"door_status": door_status, "door_message": "Door locked", **res}
    # Already locked -> do nothing.
    return {"door_status": door_status, "door_message": "Already locked"}


# NOTE: there is intentionally no HTTP /register endpoint. Faces can only be
# enrolled locally on this machine via `python register_face.py`, so nobody who
# opens the web app over the network can add themselves.


@app.post("/idle")
def idle():
    """Apply the scanning/idle light scene (called when the UI starts scanning)."""
    ha = trigger_ha_scene("scanning_scene")
    return {"status": "scanning", **ha}


@app.post("/recognize")
def recognize(req: FrameRequest):
    frame = decode_base64_image(req.image)
    if frame is None:
        return JSONResponse({"status": "error", "message": "Could not decode image"}, status_code=400)

    locations = face_recognition.face_locations(frame)
    if not locations:
        return {"status": "no_face", "message": "No face detected"}

    # Use the largest face in frame.
    locations.sort(key=lambda b: (b[2] - b[0]) * (b[1] - b[3]), reverse=True)
    top, right, bottom, left = locations[0]
    box = {"top": int(top), "right": int(right), "bottom": int(bottom), "left": int(left)}

    # Eye-contact gate: only run recognition for a frontal/looking face.
    landmarks_list = face_recognition.face_landmarks(frame, [locations[0]])
    looking = bool(landmarks_list) and is_making_eye_contact(landmarks_list[0])
    if not looking:
        return {
            "status": "no_face",          # not actionable yet
            "eye_contact": False,
            "box": box,
            "message": "Look directly at the camera",
        }

    encodings = face_recognition.face_encodings(frame, [locations[0]])
    if not encodings:
        return {"status": "no_face", "eye_contact": True, "box": box, "message": "Could not read face"}

    if known_encodings:
        distances = face_recognition.face_distance(known_encodings, encodings[0])
        best = int(np.argmin(distances))
        if distances[best] <= MATCH_TOLERANCE:
            name = known_names[best]
            ha = trigger_ha_scene("accepted_scene")
            door = grant_unlock()   # unlock door if locked, else "already unlocked"
            return {
                "status": "accepted",
                "name": name,
                "eye_contact": True,
                "confidence": round(float(1 - distances[best]), 3),
                "box": box,
                **ha,
                **door,
            }

    # Face present, looking, but not recognized -> denied (passcode fallback).
    return {
        "status": "denied",
        "name": "Unknown",
        "eye_contact": True,
        "box": box,
        "message": "Face not recognized",
    }


@app.post("/passcode")
def passcode(req: PasscodeRequest):
    cfg = load_config()
    expected = str(cfg.get("passcode", "1234"))
    if req.passcode.strip() == expected:
        ha = trigger_ha_scene("accepted_scene")
        door = grant_unlock()
        return {"status": "accepted", "name": "Passcode", "message": "Passcode accepted", **ha, **door}
    ha = trigger_ha_scene("denied_scene")
    return {"status": "denied", "message": "Incorrect passcode", **ha}


def get_lan_ip() -> str:
    """Best-effort detection of this machine's primary LAN IPv4 address."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks the route
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert() -> tuple[Path, Path]:
    """
    Create a self-signed cert/key (cert.pem, key.pem) if missing, valid for
    localhost, 127.0.0.1, and this machine's current LAN IP. Browsers require
    HTTPS for camera access on anything other than localhost, so phones on the
    LAN need this. Returns (cert_path, key_path).
    """
    cert_path = BASE_DIR / "cert.pem"
    key_path = BASE_DIR / "key.pem"
    lan_ip = get_lan_ip()

    # Regenerate if missing, or if the LAN IP changed since last time.
    if cert_path.exists() and key_path.exists():
        try:
            from cryptography import x509
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ips = [str(ip) for ip in san.get_values_for_type(__import__("ipaddress").IPv4Address)]
            if lan_ip in ips:
                return cert_path, key_path
        except Exception:
            pass  # fall through and regenerate

    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    print(f"[https] generating self-signed certificate for {lan_ip} ...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jarvis FaceDoor")])
    alt_names = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv4Address(lan_ip)),
    ]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


if __name__ == "__main__":
    import uvicorn

    lan_ip = get_lan_ip()
    cert_path, key_path = ensure_cert()
    print("\n  Jarvis FaceDoor is running over HTTPS:")
    print(f"    On this PC:    https://localhost:5000")
    print(f"    On your phone: https://{lan_ip}:5000   (same Wi-Fi)")
    print("    First visit shows a security warning — tap Advanced -> Proceed.\n")
    uvicorn.run(
        app, host="0.0.0.0", port=5000,
        ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
    )
