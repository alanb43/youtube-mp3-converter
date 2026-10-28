#!/usr/bin/env python3
"""Build 'YouTube MP3 Converter.app' into ~/Applications (searchable via Spotlight).

The bundle is self-contained: its own copy of ytmp3.py plus a private venv.
Re-run this script after editing ytmp3.py to update the installed app.
"""
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

NAME = "YouTube MP3 Converter"
HERE = Path(__file__).resolve().parent
APP = Path.home() / "Applications" / f"{NAME}.app"
RES = APP / "Contents" / "Resources"
MACOS = APP / "Contents" / "MacOS"


def make_png(path, size=1024):
    """Red rounded square with a white play triangle, stdlib only."""
    r = size * 0.22          # corner radius
    cx = cy = size / 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            # rounded-square mask
            dx = max(abs(x - cx) - (cx - r), 0)
            dy = max(abs(y - cy) - (cy - r), 0)
            inside = dx * dx + dy * dy <= r * r
            if not inside:
                row += b"\x00\x00\x00\x00"
                continue
            # play triangle: left edge x=0.38s, tip x=0.70s, half-height 0.2s at left
            u, v = x / size, abs(y - cy) / size
            tri = 0.38 <= u <= 0.70 and v <= 0.20 * (0.70 - u) / 0.32
            row += b"\xff\xff\xff\xff" if tri else b"\xd9\x2b\x2b\xff"
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def make_icns(dest, work):
    master = work / "icon_1024.png"
    make_png(master)
    iconset = work / "AppIcon.iconset"
    iconset.mkdir()
    for s in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = s * scale
            out = iconset / f"icon_{s}x{s}{'@2x' if scale == 2 else ''}.png"
            subprocess.run(["sips", "-z", str(px), str(px), str(master), "--out", str(out)],
                           check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)], check=True)


def main():
    if APP.exists():
        shutil.rmtree(APP)
    RES.mkdir(parents=True)
    MACOS.mkdir(parents=True)

    shutil.copy(HERE / "ytmp3.py", RES / "ytmp3.py")
    reqs = (HERE / "requirements.txt").read_text()
    (RES / "requirements.txt").write_text(reqs)

    print("Creating private environment…")
    subprocess.run([sys.executable, "-m", "venv", str(RES / "venv")], check=True)
    subprocess.run([str(RES / "venv" / "bin" / "pip"), "install", "-q", "-r",
                    str(RES / "requirements.txt")], check=True)

    work = HERE / "_icon_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    make_icns(RES / "AppIcon.icns", work)
    shutil.rmtree(work)

    launcher = MACOS / NAME
    launcher.write_text(
        '#!/bin/sh\n'
        'R="$(cd "$(dirname "$0")/../Resources" && pwd)"\n'
        'exec arch -arm64 "$R/venv/bin/python" "$R/ytmp3.py" "$@" 2>>"$HOME/Library/Logs/YouTubeMP3Converter.log"\n')
    launcher.chmod(0o755)

    (APP / "Contents" / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{NAME}</string>
  <key>CFBundleDisplayName</key><string>{NAME}</string>
  <key>CFBundleIdentifier</key><string>local.youtube-mp3-converter</string>
  <key>CFBundleExecutable</key><string>{NAME}</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
""")
    # Ad-hoc sign so Gatekeeper treats the locally built bundle consistently, then index it.
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(APP)], capture_output=True)
    subprocess.run(["mdimport", str(APP)], capture_output=True)
    subprocess.run(["touch", str(APP)])
    print(f"Installed: {APP}")


if __name__ == "__main__":
    main()
