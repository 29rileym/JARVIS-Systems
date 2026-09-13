#!/usr/bin/env bash
#
# Refresh this repo from the live deployment on the server.
#
# The apps run from ~/Downloads/JarvisVoiceAssistant and ~/Downloads/JarvisFACEDOOR;
# this repo is a clean copy of their source. Run this after editing code there,
# then review and commit from VS Code.
#
# Only the files listed below are ever copied. config.json, faces/, *.pem and
# the virtualenvs are never touched, so secrets cannot reach the repo by
# accident even if this script is run carelessly.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE="${JARVIS_LIVE_DIR:-$HOME/Downloads}"

VOICE_SRC="$LIVE/JarvisVoiceAssistant"
FACE_SRC="$LIVE/JarvisFACEDOOR"

VOICE_FILES=(README.md .gitignore config.example.json gen_cert.py index.html
             main.py requirements.txt start_jarvis.sh start_jarvis.bat)
FACE_FILES=(README.md .gitignore config.example.json index.html main.py
            register_face.py requirements.txt)

if [[ ! -d "$VOICE_SRC" || ! -d "$FACE_SRC" ]]; then
  echo "error: live app directories not found under $LIVE" >&2
  echo "       this script only runs on the server that hosts the apps." >&2
  echo "       set JARVIS_LIVE_DIR if they live somewhere else." >&2
  exit 1
fi

changed=0

copy_set() {
  local src="$1" dst="$2"; shift 2
  for f in "$@"; do
    if [[ ! -f "$src/$f" ]]; then
      echo "  skip    $f (missing in $src)"
      continue
    fi
    if [[ ! -f "$dst/$f" ]] || ! cmp -s "$src/$f" "$dst/$f"; then
      cp "$src/$f" "$dst/$f"
      echo "  updated $f"
      changed=$((changed + 1))
    fi
  done
}

echo "voice-assistant/"
copy_set "$VOICE_SRC" "$REPO/voice-assistant" "${VOICE_FILES[@]}"
echo "facedoor/"
copy_set "$FACE_SRC" "$REPO/facedoor" "${FACE_FILES[@]}"

echo
if [[ $changed -eq 0 ]]; then
  echo "already up to date."
else
  echo "$changed file(s) updated — review and commit them in VS Code."
fi
