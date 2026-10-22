"""Build a deployable source archive without local databases or credentials."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent
files = [
    "server/app.py",
    "server/engine.py",
    "server/requirements.txt",
    "web/index.html",
    "web/app.js",
    "web/styles.css",
    "web/assets/kurumi-avatar.jpg",
    "deploy/fx-online.service",
    "deploy/nginx-fx-online.conf",
    "deploy/phone_comfyui-fx-proxy.conf",
    "README.md",
]
target = root / "fx-online.zip"
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, name)
print(f"Built {target} ({target.stat().st_size:,} bytes)")
