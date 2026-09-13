"""
Register a face into the backend from an image file (no browser needed).

Usage:
    python register_face.py <name> <path-to-image>

Examples:
    python register_face.py "Your Name" C:\\path\\to\\me.jpg
    python register_face.py "Mom" mom_selfie.png

The encoding is saved to faces/<name>.npy and a snapshot to faces/<name>.jpg,
exactly like the in-browser "Register Face" button. If the server is running,
the new face is picked up automatically (faces are reloaded on each register
from the UI; for this script, just (re)start the server or it will be loaded on
next startup).

You can also list / remove registered faces:
    python register_face.py --list
    python register_face.py --remove <name>
"""

import sys
from pathlib import Path

import numpy as np
import face_recognition
from PIL import Image

FACES_DIR = Path(__file__).resolve().parent / "faces"
FACES_DIR.mkdir(exist_ok=True)


def safe_name(name: str) -> str:
    s = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip()
    return s.replace(" ", "_")


def list_faces() -> None:
    names = sorted(p.stem for p in FACES_DIR.glob("*.npy"))
    if not names:
        print("No faces registered yet.")
    else:
        print(f"{len(names)} registered face(s):")
        for n in names:
            print("  -", n)


def remove_face(name: str) -> None:
    safe = safe_name(name)
    removed = False
    for ext in (".npy", ".jpg"):
        f = FACES_DIR / f"{safe}{ext}"
        if f.exists():
            f.unlink()
            removed = True
    print(f"Removed '{safe}'." if removed else f"No face named '{safe}' found.")


def register(name: str, image_path: str) -> None:
    safe = safe_name(name)
    if not safe:
        sys.exit("Error: invalid name.")

    path = Path(image_path)
    if not path.exists():
        sys.exit(f"Error: image not found: {image_path}")

    try:
        img = np.array(Image.open(path).convert("RGB"))
    except Exception as e:
        sys.exit(f"Error: could not open image: {e}")

    print(f"Detecting face in {path.name} ...")
    locations = face_recognition.face_locations(img)
    if not locations:
        sys.exit("Error: no face detected in the image. Try a clearer, front-facing photo.")
    if len(locations) > 1:
        sys.exit(f"Error: {len(locations)} faces detected. Use a photo with only one face.")

    encodings = face_recognition.face_encodings(img, locations)
    if not encodings:
        sys.exit("Error: could not encode the face.")

    np.save(FACES_DIR / f"{safe}.npy", encodings[0])
    try:
        Image.fromarray(img).save(FACES_DIR / f"{safe}.jpg")
    except Exception:
        pass

    print(f"Registered '{safe}'  ->  faces/{safe}.npy")
    print("Restart the server (or it loads on next startup) to use this face.")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--list":
        list_faces()
        return
    if args[0] == "--remove":
        if len(args) < 2:
            sys.exit("Usage: python register_face.py --remove <name>")
        remove_face(args[1])
        return
    if len(args) != 2:
        sys.exit("Usage: python register_face.py <name> <path-to-image>")
    register(args[0], args[1])


if __name__ == "__main__":
    main()
