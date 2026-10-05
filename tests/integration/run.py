#!/usr/bin/env python3
"""Boot the real installer ISO against a fresh, disposable QEMU disk.

The instrumented ISO/payload use a test-only virtio agent. Normal production
images contain neither the service nor agent. All host disk writes stay inside
a newly created work directory, and no host block device is attached to QEMU.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads(Path(__file__).with_name("contract.json").read_text())
TOKEN = CONTRACT["token"]
SERIAL = CONTRACT["disk_serial"]
BUILDER = "ghcr.io/osbuild/image-builder@sha256:bb4bb67be80131bf149722b2e7dacc039434ee2b68abeb70c86c8221c8281f45"


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class Firmware:
    code: Path
    variables: Path
    code_format: str = "raw"
    variables_format: str = "raw"
    source: str = "explicit"


def find_firmware(descriptor: Path | None = None, *, code=None, variables=None):
    if code is not None or variables is not None:
        if code is None or variables is None:
            raise HarnessError("--ovmf-code and --ovmf-vars must be provided together")
        firmware = Firmware(Path(code).resolve(strict=True), Path(variables).resolve(strict=True))
        if not firmware.code.is_file() or not firmware.variables.is_file():
            raise HarnessError("firmware code and variables must be regular files, never host block devices")
        return firmware
    candidates = [descriptor] if descriptor else sorted(Path("/usr/share/qemu/firmware").glob("*.json"))
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text())
            if not {"secure-boot", "enrolled-keys"}.issubset(data.get("features", [])):
                continue
            if not any(target.get("architecture") == "x86_64" for target in data.get("targets", [])):
                continue
            mapping = data["mapping"]
            if mapping.get("device") != "flash" or mapping.get("mode") != "split":
                continue
            executable, template = mapping["executable"], mapping["nvram-template"]
            code_path = Path(executable["filename"])
            vars_path = Path(template["filename"])
            if code_path.is_file() and vars_path.is_file():
                return Firmware(code_path, vars_path, executable["format"], template["format"], str(candidate))
        except (OSError, KeyError, ValueError):
            continue
    if descriptor:
        raise HarnessError("firmware descriptor must declare x86_64 Secure Boot with enrolled keys and readable split flash files")
    # Ubuntu packages commonly ship these enrolled Microsoft-key templates.
    for prefix in (Path("/usr/share/OVMF"), Path("/usr/share/edk2/ovmf")):
        code_path, vars_path = prefix / "OVMF_CODE_4M.secboot.fd", prefix / "OVMF_VARS_4M.ms.fd"
        if code_path.is_file() and vars_path.is_file():
            return Firmware(code_path, vars_path, source="enrolled Microsoft-key template")
    raise HarnessError("enrolled Secure Boot OVMF firmware was not found; provide --firmware-json or --ovmf-code/--ovmf-vars")


def capabilities(args):
    binaries = {name: shutil.which(name) for name in ("qemu-system-x86_64", "qemu-img", "swtpm", "podman")}
    failures = [f"missing {name}" for name in ("qemu-system-x86_64", "qemu-img", "swtpm") if not binaries[name]]
    firmware = None
    try:
        selected = find_firmware(args.firmware_json, code=args.ovmf_code, variables=args.ovmf_vars)
        firmware = {"code": str(selected.code), "variables": str(selected.variables),
                    "code_format": selected.code_format, "variables_format": selected.variables_format,
                    "source": selected.source}
    except (HarnessError, OSError) as error:
        failures.append(str(error))
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    if args.accel == "kvm" and not kvm:
        failures.append("/dev/kvm is unavailable or inaccessible")
    if args.build and (os.geteuid() != 0 or not binaries["podman"]):
        failures.append("ISO building requires root and podman; a prebuilt integration ISO runs without root")
    return {"ok": not failures, "binaries": binaries, "firmware": firmware, "kvm": kvm,
            "acceleration": args.accel if args.accel != "auto" else ("kvm" if kvm else "tcg"),
            "rootful_build_available": os.geteuid() == 0 and bool(binaries["podman"]), "failures": failures}


def safe_option_path(path):
    value = str(Path(path).absolute())
    if "," in value or "\n" in value or "\r" in value:
        raise HarnessError("QEMU file paths must not contain commas or line separators")
    return value


def qemu_command(args, work, firmware, *, live, recovery_console=False):
    work = Path(work)
    disk = safe_option_path(work / "target.qcow2")
    code = safe_option_path(firmware.code)
    variables = safe_option_path(work / "OVMF_VARS")
    channel = safe_option_path(work / "agent.sock")
    tpm = safe_option_path(work / "swtpm.sock")
    acceleration = args.accel if args.accel != "auto" else ("kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg")
    argv = [
        "qemu-system-x86_64", "-machine", f"q35,accel={acceleration},smm=on",
        "-m", str(args.memory_mib), "-smp", str(args.cpus), "-cpu", "host" if acceleration == "kvm" else "max",
        "-global", "driver=cfi.pflash01,property=secure,value=on",
        "-drive", f"if=pflash,format={firmware.code_format},readonly=on,file={code}",
        "-drive", f"if=pflash,format={firmware.variables_format},file={variables}",
        "-drive", f"if=none,id=target,format=qcow2,file={disk}",
        "-device", f"virtio-blk-pci,drive=target,serial={SERIAL}",
        "-chardev", f"socket,id=tpm,path={tpm}", "-tpmdev", "emulator,id=tpm0,chardev=tpm",
        "-device", "tpm-tis,tpmdev=tpm0",
        "-device", "virtio-serial-pci", "-chardev", f"socket,id=agent,path={channel},server=on,wait=off",
        "-device", f"virtserialport,chardev=agent,name={CONTRACT['channel']}",
        "-fw_cfg", f"name={CONTRACT['fw_cfg_name']},string={TOKEN}",
        "-nic", "user,model=virtio-net-pci", "-display", args.display,
        "-monitor", "none", "-no-reboot",
    ]
    if recovery_console:
        argv.extend(["-chardev", f"socket,id=recovery,path={safe_option_path(work / 'recovery.sock')},server=on,wait=off",
                     "-serial", "chardev:recovery"])
    else:
        argv.extend(["-serial", f"file:{safe_option_path(work / ('live.serial.log' if live else 'installed.serial.log'))}"])
    if live:
        argv.extend(["-drive", f"file={safe_option_path(args.iso)},media=cdrom,readonly=on", "-boot", "order=d"])
    else:
        argv.extend(["-boot", "order=c"])
    return argv


def build_commands(args, work):
    work = Path(work)
    name = re.sub(r"[^a-z0-9_.-]", "-", work.name.lower())[:64]
    prefix = f"localhost/mertensiaos-integration/{name}"
    base = args.base_image or f"{prefix}:base"
    payload, installer_base, installer = (f"{prefix}:{tag}" for tag in ("payload", "installer-base", "installer"))
    target = args.target_image or payload
    commands = []
    if not args.base_image:
        commands.append(["podman", "build", "--pull", "-f", str(ROOT / "Containerfile"), "-t", base, str(ROOT)])
    commands.append(["podman", "build", "--build-arg", f"BASE_IMAGE={base}", "--build-arg", "INTEGRATION_ROLE=payload",
                     "-f", str(ROOT / "tests/integration/Containerfile"), "-t", payload, str(ROOT)])
    if args.upgrade_public_key:
        public = args.upgrade_public_key.read_bytes()
        if not public.startswith(b"-----BEGIN PUBLIC KEY-----") or b"PRIVATE KEY" in public:
            raise HarnessError("--upgrade-public-key must contain a PEM public signing key")
        commands[-1][2:2] = ["--build-arg", f"INTEGRATION_UPGRADE_IMAGE={args.upgrade_image}",
                             "--build-arg", "INTEGRATION_PUBLIC_KEY_B64=" + base64.b64encode(public).decode()]
    commands.append(["podman", "build", "--build-arg", f"BASE_IMAGE={base}", "--build-arg", f"SOURCE_IMAGE={payload}",
                     "--build-arg", f"TARGET_IMAGE={target}", "--build-arg", "BUILD_MODE=development",
                     "-f", str(ROOT / "Containerfile.installer"), "-t", installer_base, str(ROOT)])
    commands.append(["podman", "build", "--build-arg", f"BASE_IMAGE={installer_base}", "--build-arg", "INTEGRATION_ROLE=live",
                     "-f", str(ROOT / "tests/integration/Containerfile"), "-t", installer, str(ROOT)])
    commands.append([
        "podman", "run", "--rm", "--privileged", "--security-opt", "label=type:unconfined_t",
        "-v", "/var/lib/containers/storage:/var/lib/containers/storage",
        "-v", f"{work / 'iso'}:/output", args.builder_image,
        "build", "--bootc-ref", installer, "--bootc-installer-payload-ref", payload,
        "--bootc-default-fs", "ext4", "--output-dir", "/output", "--output-name", "mertensiaos-integration", "bootc-generic-iso",
    ])
    return commands, {"base": base, "payload": payload, "installer": installer, "target": target}


def wait_path(path, process, timeout):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            raise HarnessError(f"{Path(process.args[0]).name} exited {process.returncode}; inspect its log")
        if time.monotonic() >= deadline:
            raise HarnessError(f"timed out waiting for {path.name}")
        time.sleep(0.1)


class Guest:
    def __init__(self, args, work, firmware, live, recovery_key=None, *, on_secret=None, redact=None):
        self.args, self.work = args, work
        self.process = None
        self.socket = None
        self.stream = None
        self.log = None
        self.serial = None
        self.secret = None
        self.on_secret = on_secret
        self.redact = redact or (lambda value: value)
        self.counter = 0
        channel = work / "agent.sock"
        channel.unlink(missing_ok=True)
        (work / "recovery.sock").unlink(missing_ok=True)
        try:
            self.log = (work / ("qemu-live.log" if live else "qemu-installed.log")).open("ab")
            self.process = subprocess.Popen(qemu_command(args, work, firmware, live=live, recovery_console=recovery_key is not None),
                                            stdout=self.log, stderr=self.log)
            wait_path(channel, self.process, 30)
            if recovery_key is not None:
                wait_path(work / "recovery.sock", self.process, 30)
                self.serial = SerialRecovery(work, recovery_key)
            self.socket = socket.socket(socket.AF_UNIX)
            self.socket.settimeout(args.boot_timeout)
            self.socket.connect(str(channel))
            self.stream = self.socket.makefile("rwb", buffering=0)
            hello = self.receive()
            expected_role = "live" if live else "payload"
            if hello.get("type") == "startup-error":
                raise HarnessError(f"integration guest startup failed: {hello.get('error', 'unknown prerequisite failure')}")
            if hello != {"type": "hello", "protocol": CONTRACT["protocol"], "role": expected_role}:
                raise HarnessError(f"guest handshake failed: {hello}")
            if recovery_key is not None and not self.serial.prompt_answered:
                raise HarnessError("guest booted without answering the expected initramfs recovery prompt")
        except socket.timeout as error:
            stage = "live ISO" if live else "installed disk"
            state = "still running" if self.process and self.process.poll() is None else "exited"
            message = (f"timed out after {args.boot_timeout}s waiting for the integration agent on the {stage}; "
                       f"QEMU is {state}. Inspect {work / ('live.serial.log' if live else 'installed.serial.log')} "
                       f"and {work / ('qemu-live.log' if live else 'qemu-installed.log')}. "
                       "A guest that reached Linux should log integration-agent startup errors on its serial console.")
            self.close()
            raise HarnessError(message) from error
        except Exception:
            self.close()
            raise

    def receive(self):
        line = self.stream.readline(1024 * 1024)
        if not line:
            raise HarnessError("guest channel closed; inspect QEMU/serial logs")
        if len(line) >= 1024 * 1024:
            raise HarnessError("guest response exceeded the protocol size limit")
        return json.loads(line)

    def command(self, command, **values):
        self.counter += 1
        self.socket.settimeout(self.args.operation_timeout)
        message = {"id": self.counter, "command": command, **values}
        self.stream.write((json.dumps(message) + "\n").encode())
        while True:
            reply = self.receive()
            kind = reply.get("type")
            if kind == "secret":
                self.secret = reply["recovery_key"]
                if self.on_secret:
                    self.on_secret(self.secret)
            elif kind == "progress":
                event = reply.get("event", {})
                # Only phase names leave the guest; never print recovery data.
                print(self.redact(f"  installer: {event.get('id', event.get('event', 'progress'))}"), flush=True)
            elif kind == "result" and reply.get("id") == self.counter:
                if not reply.get("ok"):
                    raise HarnessError(reply.get("error", "guest operation failed"))
                return reply["result"]
            else:
                raise HarnessError("guest returned an unexpected protocol message")

    def poweroff(self):
        self.command("poweroff")
        try:
            self.process.wait(timeout=120)
        except subprocess.TimeoutExpired as error:
            raise HarnessError("guest failed to power off cleanly") from error
        if self.process.returncode:
            raise HarnessError(f"QEMU exited {self.process.returncode} after guest shutdown")
        self.close()

    def close(self):
        if self.serial:
            self.serial.close()
        if self.stream:
            self.stream.close()
        if self.socket:
            self.socket.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log:
            self.log.close()


class SerialRecovery:
    """Answer one real initramfs password prompt and redact console artifacts."""

    PROMPT = re.compile(rb"(?:please enter passphrase|enter passphrase|password:)", re.IGNORECASE)

    def __init__(self, work, recovery_key):
        self.key = recovery_key.encode()
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(1)
        self.socket.connect(str(work / "recovery.sock"))
        self.log = (work / "recovery.serial.log").open("ab")
        self.stop = threading.Event()
        self.prompt_answered = False
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        pending = b""
        recent = b""
        try:
            while not self.stop.is_set():
                try:
                    chunk = self.socket.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                pending += chunk
                recent = (recent + chunk)[-8192:]
                if not self.prompt_answered and self.PROMPT.search(recent):
                    self.socket.sendall(self.key + b"\n")
                    self.prompt_answered = True
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    self.log.write(line.replace(self.key, b"[redacted]") + b"\n")
                self.log.flush()
        except OSError:
            pass  # Guest shutdown/host cleanup closes the socket.
        finally:
            self.log.write(pending.replace(self.key, b"[redacted]"))
            self.log.flush()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.socket.close()
        self.log.close()


class Results:
    def __init__(self, work, junit=None):
        self.work = work
        self.junit = None
        if junit is not None:
            candidate = junit if junit.is_absolute() else work / junit
            candidate = candidate.absolute()
            try:
                candidate.relative_to(work.absolute())
            except ValueError as error:
                raise HarnessError("--junit must be inside the new --work-dir") from error
            if candidate != candidate.resolve(strict=False):
                raise HarnessError("--junit cannot contain symlink traversal or parent-directory escapes")
            if candidate.exists() and not stat.S_ISREG(candidate.lstat().st_mode):
                raise HarnessError("--junit must be a regular report file")
            self.junit = candidate
        self.steps = []
        self.secrets = []
        self.complete = False

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items() if key not in {"password", "recovery_key", "secret"}}
        return value

    def step(self, name, action):
        print(name, flush=True)
        began = time.monotonic()
        try:
            data = action()
            self.steps.append({"name": name, "status": "passed", "seconds": time.monotonic() - began,
                               "details": self.redact(data)})
            self.save()
            return data
        except Exception as error:
            self.steps.append({"name": name, "status": "failed", "seconds": time.monotonic() - began,
                               "error": self.redact(str(error))})
            self.save()
            raise

    def skip(self, name, reason):
        self.steps.append({"name": name, "status": "skipped", "seconds": 0, "reason": reason})
        self.save()

    def save(self):
        self.work.mkdir(parents=True, exist_ok=True)
        report = {"complete": self.complete,
                  "passed": self.complete and not any(step["status"] == "failed" for step in self.steps),
                  "steps": self.redact(self.steps)}
        (self.work / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        if self.junit:
            failures = sum(step["status"] == "failed" for step in self.steps)
            skipped = sum(step["status"] == "skipped" for step in self.steps)
            suite = ET.Element("testsuite", name="MertensiaOS ISO integration", tests=str(len(self.steps)),
                               failures=str(failures), skipped=str(skipped))
            for step in self.steps:
                case = ET.SubElement(suite, "testcase", classname="iso", name=step["name"], time=f"{step['seconds']:.3f}")
                if step["status"] == "failed":
                    ET.SubElement(case, "failure", message=step["error"]).text = step["error"]
                elif step["status"] == "skipped":
                    ET.SubElement(case, "skipped", message=step["reason"])
            self.junit.parent.mkdir(parents=True, exist_ok=True)
            if self.junit.is_symlink() or (self.junit.exists() and not stat.S_ISREG(self.junit.lstat().st_mode)):
                raise HarnessError("JUnit report destination became a symlink or nonregular file")
            descriptor, temporary = tempfile.mkstemp(prefix=".junit-", dir=self.junit.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    ET.ElementTree(suite).write(output, encoding="utf-8", xml_declaration=True)
                os.replace(temporary, self.junit)
            finally:
                Path(temporary).unlink(missing_ok=True)


def make_work_dir(requested):
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="mertensia-iso-"))
    work = requested.absolute()
    if work.exists() or work.is_symlink():
        raise HarnessError("--work-dir must be a new directory; existing disks and firmware are never reused")
    work.mkdir(parents=True, mode=0o700)
    if len(str(work / "swtpm.sock").encode()) >= 100:
        raise HarnessError("--work-dir path is too long for Unix sockets; choose a short path under /tmp")
    return work


def handoff_diagnostics(work, results=None):
    """Give the sudo caller reports/logs only, after every root write is done."""
    if os.geteuid() != 0:
        return
    uid_text, gid_text = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
    if not uid_text.isascii() or not uid_text.isdigit() or not gid_text.isascii() or not gid_text.isdigit():
        return
    uid, gid = int(uid_text), int(gid_text)
    if uid <= 0:
        return
    files = [work / name for name in ("result.json", "images.json", "build.log", "qemu-live.log", "qemu-installed.log",
                                     "live.serial.log", "installed.serial.log", "recovery.serial.log", "swtpm.log")]
    if results and results.junit:
        files.append(results.junit)
    for path in files:
        if not path.exists() and not path.is_symlink():
            continue
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise HarnessError("diagnostic artifact is not a regular file")
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    # Change the directory last: the caller cannot alter paths while root is
    # handing files over. TPM state, disk data and other private files keep
    # their original ownership and permissions.
    descriptor = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(descriptor, uid, gid)
    finally:
        os.close(descriptor)


def execute_build(args, work):
    commands, images = build_commands(args, work)
    (work / "iso").mkdir()
    (work / "images.json").write_text(json.dumps(images, indent=2) + "\n")
    with (work / "build.log").open("w") as log:
        for command in commands:
            print(f"Building: {shlex.join(command[:5])} ...", flush=True)
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise HarnessError(f"image build exited {result.returncode}; inspect {work / 'build.log'}")
    candidates = list((work / "iso").rglob("*.iso"))
    if len(candidates) != 1:
        raise HarnessError("image builder must produce exactly one integration ISO")
    return candidates[0]


def run_suite(args, work, firmware, results):
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(work / "target.qcow2"), f"{args.disk_gib}G"], check=True,
                   stdout=subprocess.DEVNULL)
    shutil.copyfile(firmware.variables, work / "OVMF_VARS")
    tpm_log = (work / "swtpm.log").open("ab")

    def start_tpm(state_directory):
        (work / state_directory).mkdir(mode=0o700, exist_ok=True)
        (work / "swtpm.sock").unlink(missing_ok=True)
        process = subprocess.Popen(["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={work / state_directory}",
                                    "--ctrl", f"type=unixio,path={work / 'swtpm.sock'}", "--flags", "not-need-init"],
                                   stdout=tpm_log, stderr=tpm_log)
        try:
            wait_path(work / "swtpm.sock", process, 30)
        except Exception:
            stop_process(process)
            raise
        return process

    def stop_process(process):
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def stop_tpm():
        stop_process(tpm)

    tpm = None
    guest = None
    recovery = None
    password = "Integration-" + secrets.token_urlsafe(24)
    results.secrets.append(password)
    try:
        tpm = start_tpm("tpm")
        def boot_live():
            nonlocal guest
            guest = Guest(args, work, firmware, True, on_secret=results.secrets.append, redact=results.redact)
            return {"agent_role": "live"}

        results.step("Boot actual live ISO with Secure Boot and TPM", boot_live)
        results.step("Installer hardware and disk selection checks", lambda: guest.command("probe"))
        results.step("Install actual backend and confirm recovery key", lambda: guest.command("install"))
        recovery = guest.secret
        if not recovery:
            raise HarnessError("installer did not return a recovery key")
        results.step("Cleanly power off the live system", guest.poweroff)
        guest = None

        def boot_installed():
            nonlocal guest
            guest = Guest(args, work, firmware, False, on_secret=results.secrets.append, redact=results.redact)
            return {"agent_role": "payload"}

        results.step("Boot installed disk without installation media", boot_installed)
        original = results.step("Verify TPM unlock, recovery key, enforcing SELinux and GDM", lambda: guest.command("root-checks", recovery_key=recovery))
        results.step("Create initial administrator and additional encrypted account", lambda: guest.command("create-accounts", password=password))
        results.step("Authenticate both accounts through GDM PAM, reject incorrect passwords and check retirement/cache",
                     lambda: guest.command("login-checks", password=password, check_wrong_password=True))
        results.step("Power off installed system", guest.poweroff)
        guest = None
        results.step("Reboot installed disk with persistent TPM state", boot_installed)
        results.step("Verify root unlock after reboot", lambda: guest.command("root-checks", recovery_key=recovery))
        results.step("Verify persisted accounts and login integration after reboot", lambda: guest.command("login-checks", password=password))
        if args.upgrade_image:
            results.step("Reject signed update with an incorrect verification key", lambda: guest.command("reject-update", image=args.upgrade_image))
            results.step("Stage signed integration update", lambda: guest.command("upgrade", image=args.upgrade_image))
            results.step("Power off before updated deployment boot", guest.poweroff)
            guest = None
            results.step("Boot updated deployment", boot_installed)
            upgraded = results.step("Verify updated root and recovery unlock", lambda: guest.command("root-checks", recovery_key=recovery))
            if upgraded["digest"] == original["digest"]:
                results.step("Verify upgrade changed deployment", lambda: (_ for _ in ()).throw(HarnessError("upgrade did not change the deployment digest")))
            else:
                results.step("Verify upgrade changed deployment", lambda: {"old": original["digest"], "new": upgraded["digest"]})
            results.step("Verify accounts survived update", lambda: guest.command("login-checks", password=password))
            results.step("Stage rollback", lambda: guest.command("rollback"))
            results.step("Power off before rollback boot", guest.poweroff)
            guest = None
            results.step("Boot rollback deployment", boot_installed)
            rolled_back = results.step("Verify rolled-back root and recovery unlock", lambda: guest.command("root-checks", recovery_key=recovery))
            if rolled_back["digest"] != original["digest"]:
                results.step("Verify rollback restored deployment", lambda: (_ for _ in ()).throw(HarnessError("rollback did not restore the original deployment digest")))
            else:
                results.step("Verify rollback restored deployment", lambda: {"digest": original["digest"]})
            results.step("Verify accounts survived rollback", lambda: guest.command("login-checks", password=password))
        else:
            results.skip("Signed update and rollback", "--upgrade-image was not supplied; requires a distinct signed integration payload reachable from the guest")
        results.step("Power off before forced initramfs recovery boot", guest.poweroff)
        guest = None
        stop_tpm()
        tpm = start_tpm("tpm-recovery")

        def boot_recovery():
            nonlocal guest
            guest = Guest(args, work, firmware, False, recovery_key=recovery, on_secret=results.secrets.append, redact=results.redact)
            return {"fresh_tpm_state": True, "initramfs_recovery_prompt_answered": guest.serial.prompt_answered}

        results.step("Boot installed disk through actual initramfs recovery prompt", boot_recovery)
        results.step("Verify recovered root with original TPM token unusable", lambda: guest.command("root-checks", recovery_key=recovery, expect_tpm=False))
        results.step("Verify accounts after root recovery", lambda: guest.command("login-checks", password=password))
        results.step("Power off recovered system", guest.poweroff)
        guest = None
        stop_tpm()
        tpm = start_tpm("tpm")
        results.step("Boot with restored original TPM state", boot_installed)
        results.step("Verify automatic TPM unlock after restoring original TPM", lambda: guest.command("root-checks", recovery_key=recovery))
        results.step("Final clean poweroff", guest.poweroff)
        guest = None
        results.skip("Graphical login and polkit UI", "PAM and helper integration are automated; see docs/testing.md for interactive GNOME/polkit acceptance checks")
    finally:
        if guest:
            guest.close()
        stop_tpm()
        tpm_log.close()
        # Do not leave encryption keys or account passwords in report artifacts.
        recovery = password = None


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--check", action="store_true", help="report prerequisites without building or booting")
    result.add_argument("--dry-run", action="store_true", help="print planned commands without writing files")
    result.add_argument("--build", action="store_true", help="build disposable agent overlays and real ISO (requires root)")
    result.add_argument("--build-only", action="store_true", help="build ISO without booting it; implies --build")
    result.add_argument("--iso", type=Path, help="prebuilt integration ISO, never an uninstrumented production ISO")
    result.add_argument("--work-dir", type=Path, help="new, short artifact directory; defaults to a new /tmp directory")
    result.add_argument("--junit", type=Path, help="JUnit XML output for CI")
    result.add_argument("--base-image", help="existing production payload to overlay instead of building Containerfile")
    result.add_argument("--target-image", help="future installed update target; fixture defaults to local payload")
    result.add_argument("--builder-image", default=BUILDER)
    result.add_argument("--upgrade-image", help="distinct signed integration payload for actual update and rollback tests")
    result.add_argument("--upgrade-public-key", type=Path, help="trust this public key for the integration update repo in test overlays only (with --build)")
    result.add_argument("--require-upgrade", action="store_true", help="fail argument validation unless --upgrade-image is supplied")
    result.add_argument("--firmware-json", type=Path, help="QEMU enrolled-key Secure Boot firmware descriptor")
    result.add_argument("--ovmf-code", type=Path, help="explicit raw Secure Boot firmware code")
    result.add_argument("--ovmf-vars", type=Path, help="explicit raw firmware variables with enrolled keys")
    result.add_argument("--accel", choices=("auto", "kvm", "tcg"), default="auto")
    result.add_argument("--memory-mib", type=int, default=12288)
    result.add_argument("--disk-gib", type=int, default=40)
    result.add_argument("--cpus", type=int, default=4)
    result.add_argument("--display", choices=("none", "gtk"), default="none")
    result.add_argument("--boot-timeout", type=int, default=1800)
    result.add_argument("--operation-timeout", type=int, default=3600)
    return result


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    args.build = args.build or args.build_only
    if args.disk_gib < 24 or args.memory_mib < 4096 or args.cpus < 1 or min(args.boot_timeout, args.operation_timeout) <= 0:
        cli.error("disk must be at least 24 GiB, memory at least 4096 MiB, CPUs/timeouts positive")
    if args.require_upgrade and not args.upgrade_image:
        cli.error("--require-upgrade requires --upgrade-image")
    if args.upgrade_public_key and not (args.build and args.upgrade_image):
        cli.error("--upgrade-public-key requires --build and --upgrade-image")
    available = capabilities(args)
    if args.check:
        print(json.dumps(available, indent=2))
        return 0 if available["ok"] else 1
    results = None
    work = None
    try:
        firmware = find_firmware(args.firmware_json, code=args.ovmf_code, variables=args.ovmf_vars)
        if args.dry_run:
            work = args.work_dir or Path("/tmp/mertensia-iso-DRY-RUN")
            if not args.iso:
                args.iso = work / "iso/mertensiaos-integration.iso"
            commands, images = build_commands(args, work) if args.build else ([], {})
            commands += [qemu_command(args, work, firmware, live=True), qemu_command(args, work, firmware, live=False)]
            print(json.dumps({"capabilities": available, "images": images, "commands": commands}, indent=2))
            return 0
        if not available["ok"]:
            raise HarnessError("; ".join(available["failures"]))
        if available.get("acceleration") == "tcg":
            print("Using software emulation (TCG); this can keep host CPUs busy for a long time. "
                  "Use --accel kvm when /dev/kvm is accessible.", file=sys.stderr, flush=True)
        if not args.build and (not args.iso or not args.iso.is_file() or args.iso.is_symlink()):
            raise HarnessError("provide a regular prebuilt integration ISO with --iso, or use --build as root")
        work = make_work_dir(args.work_dir)
        print(f"Artifacts: {work}", flush=True)
        results = Results(work, args.junit)
        if args.build:
            args.iso = results.step("Build real installer and isolated integration overlays", lambda: str(execute_build(args, work)))
            args.iso = Path(args.iso)
        if args.build_only:
            print(f"Integration ISO: {args.iso}", flush=True)
            return 0
        results.step("Run ISO integration suite", lambda: run_suite(args, work, firmware, results))
        results.complete = True
        results.save()
        print(f"Integration passed. Report: {work / 'result.json'}", flush=True)
        return 0
    except (HarnessError, OSError, subprocess.SubprocessError, ValueError) as error:
        message = results.redact(str(error)) if results else str(error)
        print(f"Integration failed: {message}", file=sys.stderr)
        return 1
    finally:
        if work is not None:
            try:
                handoff_diagnostics(work, results)
            except (HarnessError, OSError) as error:
                message = results.redact(str(error)) if results else str(error)
                print(f"Could not hand diagnostic artifacts to the sudo caller: {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
