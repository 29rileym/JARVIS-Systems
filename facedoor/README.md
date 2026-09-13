# J.A.R.V.I.S — FaceDoor

A Jarvis-style facial access-control web app. FastAPI + `face_recognition`
backend, a single-file futuristic frontend, and Home Assistant light scenes for
accepted / denied states.

## Features
- Live camera feed with an animated scanning overlay when a face is detected.
- **Eye-contact gate** — recognition only runs when you're looking directly at
  the camera (frontal-pose check from facial landmarks).
- Face registration is **local-only** (via `register_face.py`) — the web app
  has no registration UI or endpoint, so nobody on the network can enroll
  themselves.
- Denied passcode screen auto-returns to face scanning after 15 seconds.
- Accepted → green UI + Home Assistant *accepted* scene.
- Denied → red UI + 4-digit passcode fallback. Correct passcode = accepted
  scene; wrong passcode = denied scene.
- Everything configurable in `config.json` — no code edits needed.

## File structure
```
main.py           FastAPI backend (serves the frontend too)
index.html        frontend (inline CSS + JS)
config.json       all user-configurable settings
requirements.txt  dependencies
faces/            stored face encodings (<name>.npy) + snapshots
```

## Setup
```bash
pip install -r requirements.txt
python main.py
```
Open **http://localhost:5000** (or `http://<this-machine-ip>:5000` from another
device on the LAN). The frontend talks to the backend at the same origin, so no
extra wiring is needed.

> Camera access requires a secure context. `localhost` works. To open it from
> another device by IP, browsers need HTTPS — run behind a reverse proxy with a
> cert, or use the machine locally.

> `face_recognition` depends on `dlib`. On Windows, `pip install dlib-bin`
> (already in requirements) avoids needing a C++ build toolchain.

## Configuring Home Assistant (`config.json`)
| key | meaning |
|-----|---------|
| `ha_url` | Home Assistant base URL, e.g. `http://homeassistant.local:8123` |
| `ha_token` | a long-lived access token (Profile → Security → Create Token) |
| `passcode` | 4-digit fallback passcode |
| `accepted_scene` | list of entities to trigger on access granted |
| `denied_scene` | list of entities to trigger on access denied |

Each scene entry is one entity:
```json
{ "entity_id": "light.living_room", "rgb_color": [0, 200, 255], "brightness": 220 }
```
- For a light, set any of `rgb_color`, `brightness`, `color_name`, `effect`,
  `color_temp`.
- For an HA scene or script, just give its id: `{ "entity_id": "scene.welcome_home" }`.

The token never leaves the machine — scenes are triggered server-side.

## Registering faces (local only)
```bash
python register_face.py "Maddox" C:\path\to\selfie.jpg   # add from a photo
python register_face.py --list                            # list registered
python register_face.py --remove Maddox                   # remove one
```
Restart the server after registering so it loads the new face.

## API
| method | path | body | result |
|--------|------|------|--------|
| POST | `/recognize` | `{image}` | `{status: accepted\|denied\|no_face, ...}` |
| POST | `/passcode` | `{passcode}` | `{status: accepted\|denied}` |
| GET | `/faces` | — | registered names |

There is **no** `/register` endpoint — enrollment is local-only by design.
