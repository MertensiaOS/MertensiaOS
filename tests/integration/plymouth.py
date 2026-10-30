"""Private QMP screenshots and keyboard entry for the optional graphical boot run."""

import json
from pathlib import Path
import re
import socket
import threading
import time


def qmp(path, command, **arguments):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        with connection.makefile("rwb", buffering=0) as stream:
            if "QMP" not in json.loads(stream.readline()):
                raise RuntimeError("Invalid private QMP greeting")
            for operation, parameters in (("qmp_capabilities", {}), (command, arguments)):
                stream.write(json.dumps({"execute": operation, "arguments": parameters}).encode() + b"\n")
                while True:
                    response = json.loads(stream.readline())
                    if "error" in response:
                        raise RuntimeError("Private QMP command failed: " + operation)
                    if "return" in response:
                        break
            return response["return"]


def ppm_pixels(content):
    header = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\n", content)
    if not header:
        raise ValueError("QEMU did not produce a P6 screenshot")
    width, height = map(int, header.groups())
    pixels = content[header.end():]
    if len(pixels) != width * height * 3:
        raise ValueError("QEMU screenshot is incomplete")
    return width, height, pixels


def classify_ppm(content):
    """Recognize this theme, including its native light entry, without OCR."""
    width, height, pixels = ppm_pixels(content)
    native = pixels.count(bytes.fromhex("21152f")) > width * height * .7
    native = native and pixels.count(bytes.fromhex("e7ddf7")) > 1000
    prompt = native and pixels.count(bytes.fromhex("f8f5ef")) > 1000
    return native, prompt


class GraphicalBoot:
    """Observe actual boot and answer only a visible native recovery prompt.

    A fresh TPM forces recovery in the existing harness. Only synthetic UI
    previews exercise retry text automatically; real wrong-key retry remains an
    interactive acceptance check. The real key never enters a saved command/log.
    """

    def __init__(self, work, label, recovery_key=None):
        self.work = Path(work)
        self.monitor = self.work / "plymouth-qmp.sock"
        self.prefix = "plymouth-" + label
        self.key = recovery_key
        self.stop = threading.Event()
        self.prompt_answered = False
        self.splash_seen = False
        self.error = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.observe, daemon=True)
        self.thread.start()

    def screenshot(self, name):
        path = self.work / (self.prefix + "-" + name + ".ppm")
        with self.lock:
            qmp(self.monitor, "screendump", filename=str(path))
        return path.read_bytes()

    def type_recovery(self):
        for character in self.key + "\n":
            # Preserve the installer's uppercase, case-sensitive recovery key.
            code = {"-": "minus", "\n": "ret"}.get(character, character.lower())
            if code not in ("minus", "ret") and not re.fullmatch("[a-z0-9]", code):
                raise RuntimeError("Unsupported recovery-key character")
            keys = [{"type": "qcode", "data": code}]
            if character.isupper():
                keys.insert(0, {"type": "qcode", "data": "shift"})
            with self.lock:
                qmp(self.monitor, "send-key", keys=keys, **{"hold-time": 10})
            if self.stop.wait(.04):
                return
        self.prompt_answered = True
        self.key = None

    def observe(self):
        try:
            while not self.stop.is_set():
                current = self.screenshot("current")
                native, prompt = classify_ppm(current)
                if native and not self.splash_seen:
                    (self.work / (self.prefix + "-boot.ppm")).write_bytes(current)
                    self.splash_seen = True
                if prompt and self.key is not None and not self.prompt_answered:
                    (self.work / (self.prefix + "-recovery.ppm")).write_bytes(current)
                    self.type_recovery()
                if self.stop.wait(.2):
                    return
        except (OSError, ValueError, RuntimeError) as error:
            if not self.stop.is_set():
                # Never retain a request or key in diagnostics.
                self.error = type(error).__name__ + ": graphical boot observation failed"

    def verify_handoff(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error:
                raise RuntimeError(self.error)
            current = self.screenshot("handoff")
            native, _ = classify_ppm(current)
            width, height, pixels = ppm_pixels(current)
            step = max(1, width * height // 4096) * 3
            drawn = len({pixels[index:index + 3] for index in range(0, len(pixels), step)}) > 8
            if self.splash_seen and not native and drawn:
                if self.key is not None or ("recovery" in self.prefix and not self.prompt_answered):
                    raise RuntimeError("Native recovery prompt was not answered")
                return
            time.sleep(.2)
        raise RuntimeError("Native Plymouth splash or graphical handoff was not observed")

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)
        self.key = None
        (self.work / (self.prefix + "-current.ppm")).unlink(missing_ok=True)
