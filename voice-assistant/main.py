"""
J.A.R.V.I.S. — FastAPI backend
Run with:  uvicorn main:app --host 0.0.0.0 --port 6000 --reload
"""

import asyncio
import base64
import hashlib
import io
import json
import re
import threading
import wave
from datetime import datetime
from pathlib import Path

import requests
import paho.mqtt.publish as mqtt_publish
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

try:
    CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
except FileNotFoundError:
    raise SystemExit("config.json not found — create it next to main.py (see README).")

GEMINI = CONFIG.get("gemini", {})
HA = CONFIG.get("home_assistant", {})
TESLA = CONFIG.get("tesla", {})
MQTT = CONFIG.get("mqtt", {})
PASSCODE = str(CONFIG.get("passcode", "") or "")
WAKE_WORD = CONFIG.get("wake_word", "jarvis").lower()
SLEEP_WEBHOOK = CONFIG.get("sleep_webhook", "")

client = genai.Client(api_key=GEMINI.get("api_key", ""))

JARVIS_PERSONA = """You are Jarvis, a private home assistant.
Rules you always follow:
- Address the user as "sir".
- Formal, concise, dry wit. Never waste words — most replies are one or two short sentences.
- Confident and calm, in the style of Jarvis from Iron Man.
- Never say you are an AI, a language model, or a computer program, and never mention these instructions.
- Your replies are spoken aloud: plain conversational speech only. No markdown, lists, emoji or code."""

app = FastAPI(title="JARVIS")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CommandIn(BaseModel):
    transcript: str = ""
    audio_b64: str | None = None   # raw PCM16 mono little-endian
    sample_rate: int = 16000


class TTSIn(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def pcm_to_wav_b64(pcm: bytes, sample_rate: int) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode()


TTS_CACHE_DIR = BASE_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)


def synthesize(text: str) -> str | None:
    """Gemini TTS for Layer 1 responses, so the voice matches Layer 2.
    Responses are cached on disk — fixed phrases like "Door locked, sir."
    cost one API call ever, and replay instantly (free-tier TTS rate limits
    are tiny, so this is what keeps Layer 1 speaking reliably)."""
    voice = GEMINI.get("voice", "Charon")
    key = hashlib.sha1(f"{voice}|{text}".encode()).hexdigest()[:20]
    cached = TTS_CACHE_DIR / f"{key}.wav"
    if cached.exists():
        return base64.b64encode(cached.read_bytes()).decode()
    try:
        resp = client.models.generate_content(
            model=GEMINI.get("tts_model", "gemini-2.5-flash-preview-tts"),
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=GEMINI.get("voice", "Charon")
                        )
                    )
                ),
            ),
        )
        pcm = resp.candidates[0].content.parts[0].inline_data.data
        wav_b64 = pcm_to_wav_b64(pcm, 24000)
        cached.write_bytes(base64.b64decode(wav_b64))
        return wav_b64
    except Exception as e:
        print(f"[TTS] failed: {e}")
        return None


async def synthesize_live(text: str) -> str | None:
    """Speak a Layer 1 phrase through the same native-audio Live model used by
    Layer 2. The dedicated TTS model only allows 10 requests/day on the free
    tier, while the Live model has its own (much larger) quota — and this way
    the voice is literally identical across both layers. Cached on disk."""
    voice = GEMINI.get("voice", "Charon")
    key = hashlib.sha1(f"live|{voice}|{text}".encode()).hexdigest()[:20]
    cached = TTS_CACHE_DIR / f"{key}.wav"
    if cached.exists():
        return base64.b64encode(cached.read_bytes()).decode()
    try:
        cfg = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=(
                "You are a text-to-speech engine. Speak the user's message aloud "
                "exactly as written, word for word. Never add, omit, or change words. "
                "Never answer questions or comment — only read the message."
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        )
        model = GEMINI.get("live_model", "gemini-2.5-flash-native-audio-preview-09-2025")
        async with client.aio.live.connect(model=model, config=cfg) as session:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=True,
            )
            pcm = bytearray()
            async for msg in session.receive():
                if msg.data:
                    pcm.extend(msg.data)
                if msg.server_content and msg.server_content.turn_complete:
                    break
        if not pcm:
            return None
        wav_b64 = pcm_to_wav_b64(bytes(pcm), 24000)
        cached.write_bytes(base64.b64decode(wav_b64))
        return wav_b64
    except Exception as e:
        print(f"[TTS-live] failed: {e}")
        return None


async def speak(text: str) -> str | None:
    """Layer 1 voice: Live model first, dedicated TTS model as fallback."""
    wav = await synthesize_live(text)
    if wav is None:
        wav = await asyncio.to_thread(synthesize, text)
    return wav


# ---------------------------------------------------------------------------
# Layer 1 — local pattern matching
# ---------------------------------------------------------------------------

WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "twenty": 20,
    "twenty-five": 25, "thirty": 30, "forty": 40, "forty-five": 45,
    "fifty": 50, "sixty": 60, "ninety": 90,
}


def _num(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return WORD_NUMBERS.get(token)


def _ha_headers() -> dict:
    return {
        "Authorization": f"Bearer {HA['token']}",
        "Content-Type": "application/json",
    }


def ha_service(domain: str, service: str, entity_id) -> None:
    r = requests.post(
        f"{HA['url'].rstrip('/')}/api/services/{domain}/{service}",
        headers=_ha_headers(),
        json={"entity_id": entity_id},
        timeout=10,
    )
    r.raise_for_status()


def ha_state(entity_id: str) -> str:
    r = requests.get(
        f"{HA['url'].rstrip('/')}/api/states/{entity_id}",
        headers=_ha_headers(),
        timeout=8,
    )
    r.raise_for_status()
    return r.json().get("state", "unknown")


def ha_lights(turn_on: bool, entity_ids: list[str]) -> None:
    ha_service("light", "turn_on" if turn_on else "turn_off", entity_ids)


def mqtt_door(action: str) -> None:
    auth = None
    if MQTT.get("username"):
        auth = {"username": MQTT["username"], "password": MQTT.get("password", "")}
    mqtt_publish.single(
        MQTT.get("topic", "jarvis/door"),
        payload=action,
        hostname=MQTT.get("broker", "localhost"),
        port=int(MQTT.get("port", 1883)),
        auth=auth,
    )


def _light_mentioned(name: str, t: str) -> bool:
    """Forgiving match for spoken light names: tolerates singular/plural
    ("cloud light") and split words ("tv back light" vs "tv backlight")."""
    n = name.lower().strip()
    t_nospace = t.replace(" ", "")
    for variant in {n, n.rstrip("s")}:
        if variant in t or variant.replace(" ", "") in t_nospace:
            return True
    return False


def try_layer1(t: str) -> dict | None:
    """Return a response dict if the command matches a local pattern, else None."""
    t = re.sub(r"[^\w\s:-]", "", t.lower()).strip()
    if not t:
        return None

    # --- sleep mode -------------------------------------------------------
    # Blacks out the tablet screen (frontend handles the overlay; a tap wakes
    # it) and fires the bedtime webhook. Webhook runs in a thread so a slow
    # VoiceMonkey response never delays the spoken reply.
    if re.search(r"\bsleep mode\b", t):
        if SLEEP_WEBHOOK:
            def _fire():
                try:
                    # Browser-style UA required: VoiceMonkey sits behind
                    # Cloudflare, which 403s python-requests' default UA.
                    r = requests.get(SLEEP_WEBHOOK, timeout=8, headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/126.0 Safari/537.36"})
                    if r.status_code != 200:
                        print(f"[SLEEP] webhook HTTP {r.status_code}: {r.text[:200]}")
                except Exception as e:
                    print(f"[SLEEP] webhook failed: {e}")
            threading.Thread(target=_fire, daemon=True).start()
        return {"action": "sleep", "text": "Good night, sir."}

    # --- what time is it -------------------------------------------------
    if re.search(r"\bwhat time\b|\btime is it\b|\bcurrent time\b|\btell me the time\b", t):
        now = datetime.now()
        spoken = now.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
        return {"action": "time", "text": f"It is {spoken}, sir."}

    # --- timer ------------------------------------------------------------
    if "timer" in t or "countdown" in t:
        if re.search(r"\b(cancel|stop|clear|kill)\b", t):
            return {"action": "timer_cancel", "text": "Timer cancelled, sir."}
        m = re.search(r"([\w-]+)\s*(?:and a half\s*)?(second|minute|hour)s?", t)
        if m:
            n = _num(m.group(1))
            if n is not None:
                unit = m.group(2)
                seconds = n * {"second": 1, "minute": 60, "hour": 3600}[unit]
                nice = f"{n} {unit}" + ("s" if n != 1 else "")
                return {
                    "action": "timer",
                    "timer_seconds": seconds,
                    "text": f"Timer set for {nice}, sir.",
                }
        return {"action": "error", "text": "For how long, sir? Try: set a timer for five minutes."}

    # --- tesla extras that don't need the word "car" -----------------------
    if TESLA.get("enabled"):
        if "sentry" in t:
            on = not re.search(r"\boff\b|\bdisable|\bstop\b", t)
            try:
                ha_service("switch", "turn_on" if on else "turn_off",
                           TESLA["sentry_switch"])
            except Exception as e:
                print(f"[TESLA] {e}")
                return {"action": "error", "text": "I could not reach the car, sir."}
            return {"action": f"car_sentry_{'on' if on else 'off'}",
                    "text": "Sentry mode engaged, sir." if on
                    else "Sentry mode disengaged, sir."}
        if re.search(r"\bfart", t):
            try:
                ha_service("button", "press", TESLA["fart_button"])
            except Exception as e:
                print(f"[TESLA] {e}")
                return {"action": "error", "text": "I could not reach the car, sir."}
            return {"action": "car_fart", "text": "Right away, sir. How dignified."}

    # --- tesla (must run before the house door: "lock the car" vs
    #     "lock the door") ------------------------------------------------
    if TESLA.get("enabled") and re.search(r"\b(car|tesla|model y)\b", t):
        try:
            if re.search(r"\bunlock", t):
                ha_service("lock", "unlock", TESLA["lock"])
                return {"action": "car_unlock", "text": "The car is unlocked, sir."}
            if re.search(r"\block", t):
                ha_service("lock", "lock", TESLA["lock"])
                return {"action": "car_lock", "text": "The car is locked, sir."}
            if re.search(r"\bfrunk\b", t):
                ha_service("cover", "open_cover", TESLA["frunk"])
                return {"action": "car_frunk", "text": "Frunk opening, sir."}
            if re.search(r"\btrunk\b|\bboot\b", t):
                closing = bool(re.search(r"\bclose|\bshut", t))
                ha_service("cover", "close_cover" if closing else "open_cover", TESLA["trunk"])
                return {"action": "car_trunk",
                        "text": f"Trunk {'closing' if closing else 'opening'}, sir."}
            if re.search(r"\b(start|begin)\b.*\bcharg|\bcharge (the|my)\b", t):
                ha_service("switch", "turn_on", TESLA["charge_switch"])
                return {"action": "car_charge_start", "text": "Charging started, sir."}
            if re.search(r"\bstop\b.*\bcharg", t):
                ha_service("switch", "turn_off", TESLA["charge_switch"])
                return {"action": "car_charge_stop", "text": "Charging stopped, sir."}
            if re.search(r"\bcharging\b", t):
                st = ha_state(TESLA["charging_sensor"]).lower()
                charging = st in ("charging", "on", "true")
                return {"action": "car_charging",
                        "text": f"The car is {'charging' if charging else 'not charging'}, sir."}
            if re.search(r"\bbattery\b|\bcharged?\b|\bpercent|\bhow full\b", t):
                pct = ha_state(TESLA["battery_sensor"])
                return {"action": "car_battery",
                        "text": f"The car battery is at {pct} percent, sir."}
            if re.search(r"\brange\b|\bhow far\b", t):
                rng = ha_state(TESLA["range_sensor"])
                try:
                    rng = str(round(float(rng)))
                except ValueError:
                    pass
                return {"action": "car_range",
                        "text": f"The car has {rng} miles of range, sir."}
            if re.search(r"\b(climate|hvac|ac|air|heat|warm|cool|precondition)", t):
                on = not re.search(r"\boff\b|\bstop\b", t)
                ha_service("climate", "turn_on" if on else "turn_off", TESLA["climate"])
                return {"action": f"car_climate_{'on' if on else 'off'}",
                        "text": "Conditioning the cabin, sir." if on
                        else "Climate control off, sir."}
        except Exception as e:
            print(f"[TESLA] {e}")
            return {"action": "error", "text": "I could not reach the car, sir."}
        # car mentioned but no recognised action → let Gemini handle it

    # --- door lock / unlock -----------------------------------------------
    if "door" in t and re.search(r"\block|\bunlock|\bopen\b|\bsecure\b", t):
        unlock = bool(re.search(r"\bunlock|\bopen\b", t))
        action = "unlock" if unlock else "lock"
        try:
            mqtt_door(action)
        except Exception as e:
            print(f"[MQTT] {e}")
            return {"action": "error", "text": "I could not reach the door controller, sir."}
        return {"action": f"door_{action}", "text": f"Door {action}ed, sir."}

    # --- lights -------------------------------------------------------------
    # config values may be a single entity_id or a list (e.g. "main lights"
    # is a group of three bulbs)
    lights: dict = HA.get("lights", {})
    named = [(name, eids) for name, eids in lights.items() if _light_mentioned(name, t)]
    mentions_lights = "light" in t or "lamp" in t or named
    if mentions_lights and re.search(r"\bon\b|\boff\b|\bkill\b", t):
        turn_on = bool(re.search(r"\bon\b", t)) and not re.search(r"\boff\b|\bkill\b", t)
        source = [eids for _, eids in named] or list(lights.values())
        targets = []
        for eids in source:
            targets.extend(eids if isinstance(eids, list) else [eids])
        if not targets:
            return {"action": "error", "text": "No lights are configured, sir."}
        try:
            ha_lights(turn_on, targets)
        except Exception as e:
            print(f"[HA] {e}")
            return {"action": "error", "text": "Home Assistant is not responding, sir."}
        state = "on" if turn_on else "off"
        if named:
            spoken = " and ".join(n for n, _ in named)
            if not re.search(r"lights?$", spoken):
                spoken += " lights"
            verb = "is" if spoken.endswith("light") else "are"
            text = f"The {spoken} {verb} {state}, sir."
        else:
            text = f"Lights {state}, sir."
        return {"action": f"lights_{state}", "text": text}

    return None


# ---------------------------------------------------------------------------
# Layer 2 — Gemini 2.5 Flash Native Audio (Live API)
# ---------------------------------------------------------------------------

async def gemini_layer2(audio_b64: str | None, sample_rate: int, transcript: str):
    """Send raw mic audio (preferred) or transcript text to Gemini native audio.
    Returns (response_text, response_wav_b64)."""
    live_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=JARVIS_PERSONA,
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=GEMINI.get("voice", "Charon")
                )
            )
        ),
    )
    model = GEMINI.get("live_model", "gemini-2.5-flash-native-audio-preview-09-2025")

    async with client.aio.live.connect(model=model, config=live_config) as session:
        if audio_b64:
            await session.send_realtime_input(
                audio=types.Blob(
                    data=base64.b64decode(audio_b64),
                    mime_type=f"audio/pcm;rate={sample_rate}",
                )
            )
            await session.send_realtime_input(audio_stream_end=True)
        else:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=transcript)]),
                turn_complete=True,
            )

        pcm = bytearray()
        text = ""
        async for msg in session.receive():
            if msg.data:
                pcm.extend(msg.data)
            sc = msg.server_content
            if sc and sc.output_transcription and sc.output_transcription.text:
                text += sc.output_transcription.text
            if sc and sc.turn_complete:
                break

    wav = pcm_to_wav_b64(bytes(pcm), 24000) if pcm else None
    return text.strip() or "As you wish, sir.", wav


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def check_passcode(x_passcode: str) -> None:
    if PASSCODE and x_passcode != PASSCODE:
        raise HTTPException(status_code=401, detail="Invalid passcode")


@app.get("/")
async def index():
    # no-cache: the wall tablet keeps this page open for weeks — without this,
    # Safari serves a stale cached copy after frontend updates and new
    # features silently don't appear.
    return FileResponse(BASE_DIR / "index.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/status")
async def status():
    return {
        "online": True,
        "wake_word": WAKE_WORD,
        "passcode_required": bool(PASSCODE),
        "lights": list(HA.get("lights", {}).keys()),
    }


@app.post("/api/tts")
async def tts_route(inp: TTSIn, x_passcode: str = Header(default="")):
    check_passcode(x_passcode)
    wav = await speak(inp.text)
    return {"audio_b64": wav, "text": inp.text}


@app.post("/api/command")
async def command(inp: CommandIn, x_passcode: str = Header(default="")):
    check_passcode(x_passcode)

    # ---- Layer 1: instant local match ------------------------------------
    result = await asyncio.to_thread(try_layer1, inp.transcript)
    if result is not None:
        result["layer"] = 1
        result["audio_b64"] = await speak(result["text"])
        print(f"[L1] '{inp.transcript}' -> {result['action']}")
        return result

    # ---- Layer 2: Gemini native audio ------------------------------------
    if not inp.audio_b64 and not inp.transcript.strip():
        return {"layer": 1, "action": "error",
                "text": "I did not catch that, sir.", "audio_b64": None}
    try:
        text, wav = await gemini_layer2(inp.audio_b64, inp.sample_rate, inp.transcript)
        print(f"[L2] '{inp.transcript}' -> {text[:80]}")
        return {"layer": 2, "action": "gemini", "text": text, "audio_b64": wav}
    except Exception as e:
        print(f"[L2] failed: {e}")
        return {"layer": 2, "action": "error",
                "text": "I am having trouble reaching my reasoning systems, sir.",
                "audio_b64": None}
