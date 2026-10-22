"""Package the A-share Toy frontend and independent API source."""

from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
DEST = ROOT.parent / "releases" / "a-share-online-v1.3-zero-price-fix.zip"
FILES = [
    ROOT / "README.md",
    ROOT / "server" / "app.py",
    ROOT / "server" / "ashare_engine.py",
    ROOT / "server" / "requirements.txt",
    *sorted((ROOT / "toy").rglob("*")),
]

with ZipFile(DEST, "w", ZIP_DEFLATED) as archive:
    for file in FILES:
        if file.is_file():
            archive.write(file, file.relative_to(ROOT.parent))
print(DEST)
