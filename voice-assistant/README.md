# J.A.R.V.I.S. — Voice Assistant

FastAPI + Gemini 2.5 Flash Native Audio voice assistant with a Jarvis-style tablet UI.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Edit `config.json`:
   - `gemini.api_key` — from https://aistudio.google.com/apikey
   - `home_assistant.url` / `token` — HA base URL + long-lived access token (Profile → Security)
   - `home_assistant.lights` — spoken name → entity_id map (add as many as you like)
   - `mqtt` — broker for the door lock (topic `jarvis/door`, payload `lock` / `unlock`)
   - `passcode` — optional; if set, the tablet asks for it once on boot
3. Start everything with one command:
   ```
   uvicorn main:app --host 0.0.0.0 --port 8600 --reload
   ```
4. Open `http://<server-ip>:8600` on the tablet and tap **INITIALIZE**.

> **Do not use port 6000** — virtually all browsers (desktop *and* iOS/Android) block it
> as a legacy X11 "unsafe port" and show a blank page without ever contacting the server.

## How it works

- The browser listens continuously for the wake word **"Jarvis"** (Web Speech API) while a
  rolling WebAudio buffer captures raw mic PCM.
- **Layer 1 (local, instant, free):** lights on/off (Home Assistant REST), door lock/unlock
  (MQTT), timers (on-screen countdown), and "what time is it" — matched by regex in
  `main.py`, spoken back via Gemini TTS so the voice stays consistent.
- **Layer 2 (Gemini):** anything unmatched is sent as raw audio to Gemini 2.5 Flash Native
  Audio (Live API) and the reply audio streams back to the tablet speaker.

## Tesla (optional)

Voice control for the car goes through Home Assistant. First connect your Tesla account
to HA with one of: **Teslemetry** (teslemetry.com, easiest), **Tessie** (tessie.com), or
the DIY **Tesla Fleet** integration. Then set `tesla.enabled: true` in `config.json` and
fill in the entity IDs the integration created (Settings → Devices → your car in HA).

Commands: "lock/unlock the car", "warm up the car", "turn off the climate in the car",
"how charged is my car", "is the car charging", "what's the range on the car",
"open/close the trunk", "open the frunk", "start/stop charging the car".
Anything else mentioning the car goes to Gemini.

## Important notes

- **Microphone requires a secure context.** `http://<lan-ip>:8600` will block the mic in
  most browsers. Options:
  - iPad Safari / Chrome: it works on `localhost`, but for LAN use serve HTTPS:
    `uvicorn main:app --host 0.0.0.0 --port 8600 --ssl-keyfile key.pem --ssl-certfile cert.pem`
    (generate with `openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365`)
  - Android Chrome alternative: `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
    and add `http://<server-ip>:8600`.
- Wake-word detection uses the browser's Web Speech API (built into Safari and Chrome).
  If unavailable, tap the arc reactor to talk (tap again to send).
