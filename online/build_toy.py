"""Copy the independent online frontend into a Toy-ready directory."""

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "web"
TARGET = ROOT / "toy"
BACKEND = "https://8.134.122.85/a-share/"


def main():
    TARGET.mkdir(exist_ok=True)
    for name in ("index.html", "app.js", "styles.css"):
        shutil.copy2(SOURCE / name, TARGET / name)
    (TARGET / "assets").mkdir(exist_ok=True)
    shutil.copy2(SOURCE / "assets" / "kurumi-avatar.jpg", TARGET / "assets" / "kurumi-avatar.jpg")
    (TARGET / "config.js").write_text(
        f"window.A_SHARE_BACKEND_URL = {BACKEND!r};\n", encoding="utf-8"
    )
    print(f"Toy frontend ready: {TARGET}")


if __name__ == "__main__":
    main()
