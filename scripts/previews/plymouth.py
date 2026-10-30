#!/usr/bin/python3
"""Capture the real native theme and synthetic prompts inside a disposable container.

Run with scripts/test-plymouth.sh. This is renderer coverage, not a boot test.
No real passwords, host displays, host disks or host Plymouth socket are used.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from PIL import Image
from Xlib import X, XK, display
from Xlib.ext import xtest


def plymouth(*arguments):
    return subprocess.run(["plymouth", *arguments], check=True, capture_output=True)


def wait_for(predicate, description):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise RuntimeError("Timed out waiting for " + description)


def capture(connection, path):
    connection.sync()
    root = connection.screen().root
    geometry = root.get_geometry()
    raw = root.get_image(0, 0, geometry.width, geometry.height, X.ZPixmap, 0xffffffff)
    image = Image.frombytes("RGB", (geometry.width, geometry.height), raw.data, "raw", "BGRX")
    image.save(path)
    return image


def key(connection, name):
    code = connection.keysym_to_keycode(XK.string_to_keysym(name))
    if not code:
        raise RuntimeError("Unknown synthetic key: " + name)
    xtest.fake_input(connection, X.KeyPress, code)
    xtest.fake_input(connection, X.KeyRelease, code)
    connection.sync()


def focus_splash(connection):
    # No window manager is needed; direct focus delivers XTest keys to GTK.
    children = connection.screen().root.query_tree().children
    for window in children:
        if window.get_attributes().map_state == X.IsViewable:
            window.set_input_focus(X.RevertToParent, X.CurrentTime)
            connection.sync()
            return True
    return False


def verify_splash(image, multihead=False, scale=1):
    # Check native output so a silent text-renderer fallback cannot pass.
    regions = ((0, 0, 800, 600), (800, 0, 640, 480)) if multihead else ((0, 0, *image.size),)
    for x, y, width, height in regions:
        if image.getpixel((x + 8, y + 8)) != (33, 21, 47):
            raise RuntimeError("Native splash background was not rendered")
        center_x = x + width // 2
        center_y = y + int((height / scale - 112) * .42 * scale) + 56 * scale
        region = image.crop((center_x - 56 * scale, center_y - 56 * scale,
                             center_x + 56 * scale, center_y + 56 * scale))
        colors = region.getcolors(maxcolors=region.width * region.height)
        if sum(count for count, pixel in colors if pixel == (231, 221, 247)) < 1000:
            raise RuntimeError("Bloom missing or clipped on a display")


def preview(output, name, width, height, *, scale=1, multihead=False):
    directory = output / name
    directory.mkdir()
    environment = dict(os.environ, DISPLAY=":77", GDK_BACKEND="x11", PLYMOUTH_FORCE_SCALE=str(scale))
    if multihead:
        environment["PLY_CREATE_FAKE_MULTI_HEAD_SETUP"] = "1"
    server = daemon = question = None
    connection = None
    try:
        with (directory / "xvfb.log").open("wb") as xlog, (directory / "plymouth.log").open("wb") as plog:
            server = subprocess.Popen(["Xvfb", ":77", "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
                                      stdout=xlog, stderr=xlog)
            wait_for(lambda: Path("/tmp/.X11-unix/X77").exists(), "private X server")
            connection = display.Display(":77")
            daemon = subprocess.Popen(["plymouthd", "--no-daemon", "--debug", "--no-boot-log",
                                       "--kernel-command-line=quiet rhgb", "--tty=/dev/tty1"],
                                      env=environment, stdout=plog, stderr=plog)
            wait_for(lambda: subprocess.run(["plymouth", "--ping"], capture_output=True).returncode == 0,
                     "private Plymouth daemon")
            plymouth("show-splash")
            wait_for(lambda: focus_splash(connection), "native splash window")
            time.sleep(.3)
            normal = capture(connection, directory / "boot.png")
            verify_splash(normal, multihead, scale)
            question = subprocess.Popen(["plymouth", "ask-for-password", "--prompt=Enter disk recovery key:"],
                                        stdout=subprocess.DEVNULL, stderr=plog)
            time.sleep(.3)
            capture(connection, directory / "recovery-empty.png")
            for letter in "sample":
                key(connection, letter)
            key(connection, "Caps_Lock")
            time.sleep(.3)
            capture(connection, directory / "recovery-masked.png")
            key(connection, "Caps_Lock")
            key(connection, "Return")
            question.wait(timeout=5)
            plymouth("display-message", "--text=Recovery key rejected. Try again.")
            question = subprocess.Popen(["plymouth", "ask-for-password", "--prompt=Enter disk recovery key:"],
                                        stdout=subprocess.DEVNULL, stderr=plog)
            time.sleep(.3)
            capture(connection, directory / "recovery-retry.png")
            key(connection, "Return")
            question.wait(timeout=5)
            plymouth("hide-message", "--text=Recovery key rejected. Try again.")
            # two-step retains its label until another message replaces it.
            plymouth("display-message", "--text=")
            question = subprocess.Popen(["plymouth", "ask-question", "--prompt=Continue?"],
                                        stdout=subprocess.DEVNULL, stderr=plog)
            time.sleep(.2)
            for letter in "yes":
                key(connection, letter)
            time.sleep(.2)
            capture(connection, directory / "question.png")
            key(connection, "Return")
            question.wait(timeout=5)
            time.sleep(.2)
            verify_splash(capture(connection, directory / "returned-to-boot.png"), multihead, scale)
            for mode in ("reboot", "shutdown", "firmware-upgrade"):
                plymouth("change-mode", "--" + mode)
                if mode == "firmware-upgrade":
                    plymouth("system-update", "--progress=35")
                time.sleep(.3)
                capture(connection, directory / (mode + ".png"))
            plymouth("quit")
            daemon.wait(timeout=5)
    finally:
        if connection:
            connection.close()
        for process in (question, daemon, server):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return {"name": name, "width": width, "height": height, "scale": scale,
            "mixed_displays": multihead, "native_renderer": "x11", "passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        for name, width, height, scale, mixed in (
            ("800x600", 800, 600, 1, False), ("1920x1080", 1920, 1080, 1, False),
            ("3840x2160-hidpi", 3840, 2160, 2, False), ("mixed-800x600-640x480", 1440, 600, 1, True),
        ):
            results.append(preview(args.output, name, width, height, scale=scale, multihead=mixed))
            print(name + ": native splash and synthetic prompts captured", flush=True)
    finally:
        (args.output / "result.json").write_text(json.dumps(
            {"coverage": "native renderer and synthetic prompts; no VM boot", "displays": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
