<!--
  📸 SCREENSHOTS TO ADD. Look for the "SCREENSHOT NEEDED" notes below.
  These comments are hidden on GitHub and only show up when you edit this file.

  1. The voice assistant tablet screen (arc reactor UI)   <- most important
  2. FaceDoor's "ACCESS GRANTED" screen
  3. (Optional) A short GIF or video of the door unlocking with the lights changing

  Put the images in docs/screenshots/, then replace each note with the image line it shows.

  Before you take them, check that none show:
  - IP addresses in the browser's address bar (crop it out or use the tablet's full-screen mode)
  - Anyone's face except your own
  - Your house number, street, or the outside of your home
-->

# J.A.R.V.I.S. Home

**A self-hosted, Iron Man–style smart home system.** Talk to your house and
it answers back. Walk up to your front door and it recognizes your face.
Everything runs on one Ubuntu mini PC, connected through Home Assistant and
MQTT. There is no cloud subscription, and your face data never leaves the house.

<!--
  📸 SCREENSHOT NEEDED: the voice assistant tablet screen, as the main image.
  Replace this comment with:
  ![Jarvis voice assistant running on the wall tablet](docs/screenshots/voice-assistant.png)
-->

---

## What it does

### 🎙️ Voice Assistant: *"Jarvis, turn on the lights."*
A wall-mounted tablet listens for the wake word **"Jarvis"** and responds in a
natural voice.

- **Instant smart-home control.** Lights, door locks, timers and the time are
  handled locally and instantly, with no AI round-trip.
- **Conversational AI for everything else.** Other questions are streamed
  as raw audio to **Gemini 2.5 Flash Native Audio**, and the spoken reply
  streams back live.
- **Tesla integration.** Lock the car, warm it up, check the battery and range,
  open the frunk, or start charging, all by voice.

### 🚪 FaceDoor: facial-recognition entry
A camera at the door scans faces and unlocks it for people it knows.

- **Live face scanning** with an animated heads-up display overlay.
- **Eye-contact gate.** Recognition only runs when you look straight at the
  camera, so the door can't be opened by someone just walking past.
- **Lighting that responds.** A welcome light scene plays when access is granted
  and a warning scene plays when it's denied.
- **Passcode fallback** if your face isn't recognized.
- **Tamper-resistant enrollment.** New faces can only be added from the server
  itself, not from anywhere on the network.

<!--
  📸 SCREENSHOT NEEDED: FaceDoor's green "ACCESS GRANTED" screen.
  Replace this comment with:
  ![FaceDoor granting access](docs/screenshots/facedoor.png)

  Optional: a GIF of the door unlocking and the lights changing makes a great demo.
  ![FaceDoor demo](docs/screenshots/facedoor-demo.gif)
-->

---

## How it all connects

```
 tablet ──HTTPS──> voice-assistant :8600 ─┬─> Home Assistant :8123 ──> lights, Tesla
                                          └─> MQTT :1883 ─┐
 phone  ──HTTPS──> facedoor :5000 ────────────────────────┤ topic: jarvis/door
                                                          └─> Pi Pico 2 W ──> door lock
```

Both apps publish to the same MQTT topic. A **Raspberry Pi Pico 2 W** subscribes
to it and controls the physical door lock.

## Built with

| | |
|---|---|
| **Backend** | Python, FastAPI, Uvicorn |
| **AI & vision** | Gemini 2.5 Flash Native Audio (Live API), `face_recognition` / dlib |
| **Smart home** | Home Assistant REST API, MQTT |
| **Hardware** | Ubuntu mini PC, wall tablet, Raspberry Pi Pico 2 W |
| **Frontend** | Vanilla HTML/CSS/JS, Web Speech API, WebAudio |

Each app has **one backend file and one frontend file**, with no build step
and no bundler.

---

## Project layout

| Folder | What's inside | Port |
|---|---|---|
| [`voice-assistant/`](voice-assistant/) | Wake-word voice assistant | 8600 (HTTPS) |
| [`facedoor/`](facedoor/) | Facial access control | 5000 (HTTPS) |

Each folder has its own README with full setup steps, architecture notes and
API reference.

## Quick start

The two apps are independent, so you can set up either one on its own.

<details>
<summary><b>Voice assistant</b></summary>

```bash
cd voice-assistant
cp config.example.json config.json     # add your Gemini + Home Assistant keys
./start_jarvis.sh
```
</details>

<details>
<summary><b>FaceDoor</b></summary>

Install the packages in this order. dlib is hard to build from source.

```bash
cd facedoor
python3 -m venv venv && source venv/bin/activate
pip install dlib-bin || pip install dlib
pip install --no-deps face_recognition face_recognition_models
pip install fastapi "uvicorn[standard]" numpy Pillow requests click \
            cryptography paho-mqtt "setuptools<81"
cp config.example.json config.json
python register_face.py "Your Name" /path/to/photo.jpg
python main.py
```
</details>

> **Note:** Both apps use a self-signed HTTPS certificate, because browsers only
> allow microphone and camera access over a secure connection. Accept the
> browser warning once on each device.

---

## Security and privacy

**Secrets stay out of the repo.** All settings live in a `config.json` in each
app folder. That file holds API keys, a Home Assistant token and the door
passcode, so it is never committed. Copy `config.example.json` and fill in
your own values.

Also excluded on purpose:

- `facedoor/faces/`: biometric data that opens a real door. Re-enroll locally
  with `register_face.py`.
- `*.pem`: TLS private keys. Both apps generate new ones automatically.
- `venv/`, `.venv/`, `tts_cache/`, `__pycache__/`: machine-specific files that
  are rebuilt when needed.

**If a secret is ever committed by mistake,** deleting the file isn't enough,
because it stays in the git history. Replace the secret first: create a new
Gemini API key, a new Home Assistant token (both apps use it), a new FaceDoor
passcode and a new VoiceMonkey token, and delete the `*.pem` files so new keys
are generated. Clean up the git history only after that.

> ⚠️ **Local network only.** This system trusts everything on the home network.
> FaceDoor has no login, the voice assistant's passcode is off by default, and
> the MQTT door topic has no authentication. **Do not expose these apps to the
> internet.** Add real authentication before you set up any remote access.

---

## Syncing from the live server

This repo is a clean copy of the code running on the home server:

| Repo folder | Live directory |
|---|---|
| `voice-assistant/` | `~/Downloads/JarvisVoiceAssistant/` |
| `facedoor/` | `~/Downloads/JarvisFACEDOOR/` |

The live folders are never moved, because their virtual environments use
absolute paths. After editing code on the server, refresh this copy:

```bash
./sync-from-server.sh
```

The script copies only source files. It never touches `config.json`, `faces/`,
keys or virtual environments.
