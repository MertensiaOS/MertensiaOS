#!/usr/bin/python3
"""Fixed test operations inside a disposable QEMU guest; no remote shell."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import pwd
import subprocess
import tempfile
import time
from pathlib import Path


PORT = Path("/dev/virtio-ports/org.mertensia.integration")
GUARD = Path("/sys/firmware/qemu_fw_cfg/by_name/opt/org.mertensia/integration/raw")
TOKEN = b"mertensia-disposable-integration-v1"
DISK = "/dev/vda"
SERIAL = "MERTENSIA-INTEGRATION"
ROLE = Path("/usr/share/mertensia-integration/role")
ACCOUNT = "integration_admin"
SECOND_ACCOUNT = "integration_user"
ROOT_PARTITION = "/dev/vda3"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(argv, *, input_text=None, timeout=180):
    result = subprocess.run(
        argv, input=input_text, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout,
    )
    if result.returncode:
        # Never include stdin (passwords/recovery keys) in diagnostics.
        raise RuntimeError(f"{Path(argv[0]).name} exited {result.returncode}: {result.stderr[-4000:]}")
    return result.stdout.strip()


def guard_guest():
    require(os.geteuid() == 0, "the integration agent requires guest root")
    require(GUARD.read_bytes() in {TOKEN, TOKEN + b"\0"}, "dedicated QEMU integration opt-in is absent")
    require(run(["systemd-detect-virt", "--vm"]) in {"qemu", "kvm"}, "guest is not QEMU")
    require(run(["lsblk", "-dnro", "SERIAL", DISK]) == SERIAL, "test disk serial does not match")
    disks = json.loads(run(["lsblk", "--json", "-d", "-o", "PATH,TYPE,RO"]))["blockdevices"]
    require(
        {disk["path"] for disk in disks if disk["type"] == "disk" and not disk["ro"]} == {DISK},
        "guest must contain exactly one writable disk",
    )


def role():
    value = ROLE.read_text().strip()
    require(value in {"live", "payload"}, "invalid integration image role")
    return value


def status():
    return json.loads(run(["bootc", "status", "--json"]))


def boot_identity():
    data = status()
    booted = data.get("status", {}).get("booted")
    require(isinstance(booted, dict), "bootc has no booted deployment")
    image = booted.get("image", {})
    digest = image.get("imageDigest") or booted.get("ostree", {}).get("checksum")
    require(bool(digest), "bootc status has no deployment identity")
    return {"digest": digest, "image": image.get("image", {}).get("image", ""), "status": data}


def secure_boot():
    values = list(Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"))
    return bool(values) and all(len(path.read_bytes()) >= 5 and path.read_bytes()[4] == 1 for path in values)


def live_probe():
    require(role() == "live", "installation requires the integration live image")
    rows = run(["/usr/libexec/mertensia-installer-helper", "probe"]).splitlines()
    result = json.loads(rows[-1])
    require(result.get("ok") is True, f"installer requirements failed: {result}")
    listed = json.loads(run(["/usr/libexec/mertensia-installer-helper", "list-disks"]).splitlines()[-1])
    require(any(disk["path"] == DISK for disk in listed["disks"]), "installer cannot select the test disk")
    return {"requirements": result, "test_disk_visible": True}


def install(send):
    live_probe()
    process = subprocess.Popen(
        ["/usr/libexec/mertensia-installer-helper", "install", "--disk", DISK],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    recovery = None
    complete = False
    try:
        for line in process.stdout:
            event = json.loads(line)
            if event.get("event") == "recovery-key":
                recovery = event["key"]
                # The host keeps this in memory; it is omitted from JSON/JUnit logs.
                send({"type": "secret", "recovery_key": recovery})
                process.stdin.write("confirm-recovery\n")
                process.stdin.flush()
            elif event.get("event") == "error":
                raise RuntimeError(event.get("message", "installer returned an error"))
            else:
                send({"type": "progress", "event": event})
                complete = complete or event.get("event") == "complete"
        code = process.wait(timeout=180)
        require(code == 0 and complete and recovery is not None, "installer did not complete its recovery handshake")
        # Success must include cleanup, rather than merely an installed filesystem.
        require(not Path("/dev/mapper/mertensia-root").exists(), "installer left its root mapping open")
        def mounted(node):
            return any(node.get("mountpoints") or []) or any(mounted(child) for child in node.get("children", []))

        layout = json.loads(run(["lsblk", "--json", "-o", "PATH,MOUNTPOINTS", DISK]))
        require(not any(mounted(node) for node in layout["blockdevices"]), "installer left a test partition mounted")
        metadata = json.loads(run(["cryptsetup", "luksDump", "--dump-json-metadata", ROOT_PARTITION]))
        require(len(metadata["keyslots"]) == 2, "installed volume retains an unexpected encryption keyslot")
        return {"installed": True, "recovery_key_received": True, "target_cleaned_up": True}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


def root_checks(recovery_key, *, expect_tpm=True):
    require(role() == "payload", "installed test payload is not booted")
    require(secure_boot(), "Secure Boot is disabled in the installed system")
    require(Path("/dev/tpmrm0").exists() or Path("/dev/tpm0").exists(), "TPM disappeared after installation")
    source = ""
    # OSTree can expose / through composefs; its physical encrypted filesystem
    # remains mounted at /sysroot. Verify the backing mount in either layout.
    for target in ("/sysroot", "/"):
        result = subprocess.run(["findmnt", "--nofsroot", "-nro", "SOURCE", target],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if result.returncode == 0 and result.stdout.strip().startswith("/dev/mapper/"):
            source = result.stdout.strip()
            break
    require(source.startswith("/dev/mapper/"), "root was not unlocked into an encrypted mapping")
    mapping = source.removeprefix("/dev/mapper/")
    mapped = run(["cryptsetup", "status", mapping])
    require(ROOT_PARTITION in mapped, "root mapping does not use the disposable test disk")
    require(Path("/sys/fs/selinux/enforce").read_text().strip() == "1", "installed SELinux is not enforcing")
    require("tpm2-device=auto" in Path("/etc/crypttab").read_text(), "installed crypttab lacks TPM unlock")
    token = subprocess.run(["cryptsetup", "open", "--test-passphrase", "--token-only", "--token-type", "systemd-tpm2", ROOT_PARTITION],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    expected_code = 0 if expect_tpm else 2
    require(token.returncode == expected_code,
            f"TPM unlock returned {token.returncode}; expected {'success' if expect_tpm else 'key rejection (exit 2)'}")
    with tempfile.NamedTemporaryFile(mode="w", dir="/run", prefix="integration-recovery-") as key:
        os.fchmod(key.fileno(), 0o600)
        key.write(recovery_key)
        key.flush()
        run(["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens", "--key-file", key.name, ROOT_PARTITION])
    run(["systemctl", "is-active", "gdm.service"])
    return {"secure_boot": True, "selinux_enforcing": True, "tpm_unlock": expect_tpm,
            "recovery_key_unlock": True, "gdm_active": True, **boot_identity()}


def account_request(user, password, *, firstboot):
    data = {"username": user, "real_name": "Integration Test", "password": password,
            "firstboot": firstboot, "admin": firstboot}
    if firstboot:
        data.update(hostname="mertensia-integration", locale="en_US.UTF-8", timezone="UTC", keymap="us")
    caller = "mertensia-setup" if firstboot else ACCOUNT
    environment = os.environ.copy()
    # This root-only test fixture exercises the real helper's caller/lifecycle
    # handling. GUI polkit authorization remains a separate manual UI check.
    environment["PKEXEC_UID"] = str(pwd.getpwnam(caller).pw_uid)
    result = subprocess.run(
        ["/usr/libexec/mertensia-accounts-helper"], input=json.dumps(data) + "\n",
        text=True, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
    )
    reply = json.loads(result.stdout.splitlines()[-1])
    require(result.returncode == 0 and reply.get("ok") is True, f"account helper failed: {reply}")
    return reply


def create_accounts(password):
    require(role() == "payload", "accounts must be created in the installed payload")
    first = account_request(ACCOUNT, password, firstboot=True)
    require(first.get("firstboot") is True, "initial request was not handled as first boot")
    second = account_request(SECOND_ACCOUNT, password, firstboot=False)
    require(second.get("firstboot") is False, "additional account used first-boot privileges")
    return {"initial_admin_created": True, "additional_user_created": True}


def pam_authenticate(user, password, *, expect_success=True):
    """Authenticate via GDM's real PAM stack, without faking a GUI login."""
    pam = ctypes.CDLL(ctypes.util.find_library("pam"))
    libc = ctypes.CDLL(ctypes.util.find_library("c"))

    class Message(ctypes.Structure):
        _fields_ = [("style", ctypes.c_int), ("text", ctypes.c_char_p)]

    class Response(ctypes.Structure):
        _fields_ = [("text", ctypes.c_char_p), ("code", ctypes.c_int)]

    callback_type = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.POINTER(Message)),
        ctypes.POINTER(ctypes.POINTER(Response)), ctypes.c_void_p,
    )
    libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    libc.calloc.restype = ctypes.c_void_p
    libc.strdup.argtypes = [ctypes.c_char_p]
    libc.strdup.restype = ctypes.c_void_p
    libc.free.argtypes = [ctypes.c_void_p]

    @callback_type
    def conversation(count, messages, responses, _data):
        pointer = libc.calloc(count, ctypes.sizeof(Response))
        if not pointer:
            return 5  # PAM_BUF_ERR
        result = ctypes.cast(pointer, ctypes.POINTER(Response))
        for index in range(count):
            style = messages[index].contents.style
            if style in {1, 2}:  # PAM_PROMPT_ECHO_OFF / PAM_PROMPT_ECHO_ON
                value = password if style == 1 else user
                result[index].text = ctypes.cast(libc.strdup(value.encode()), ctypes.c_char_p)
            elif style not in {3, 4}:  # PAM_ERROR_MSG / PAM_TEXT_INFO
                for allocated in range(index):
                    raw = ctypes.cast(ctypes.byref(result[allocated]), ctypes.POINTER(ctypes.c_void_p))[0]
                    if raw:
                        libc.free(raw)
                libc.free(pointer)
                return 6  # PAM_CONV_ERR
        responses[0] = result
        return 0

    class Conversation(ctypes.Structure):
        _fields_ = [("callback", callback_type), ("data", ctypes.c_void_p)]

    pam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Conversation), ctypes.POINTER(ctypes.c_void_p)]
    pam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    handle = ctypes.c_void_p()
    conv = Conversation(conversation, None)
    code = pam.pam_start(b"gdm-password", user.encode(), ctypes.byref(conv), ctypes.byref(handle))
    require(code == 0, f"PAM initialization failed ({code})")
    try:
        code = pam.pam_authenticate(handle, 0)
        if not expect_success:
            require(code in {7, 11}, f"wrong password did not produce a PAM authentication rejection for {user} ({code})")
            return
        require(code == 0, f"GDM PAM authentication failed for {user} ({code})")
        code = pam.pam_acct_mgmt(handle, 0)
        require(code == 0, f"GDM PAM account validation failed for {user} ({code})")
    finally:
        pam.pam_end(handle, code)


def login_checks(password, *, check_wrong_password=False):
    import dbus

    deadline = time.monotonic() + 90
    while not Path("/var/lib/mertensia/setup-account-disabled").exists() and time.monotonic() < deadline:
        time.sleep(1)
    require(Path("/var/lib/mertensia/firstboot-complete").exists(), "first-boot marker is absent")
    require(Path("/var/lib/mertensia/setup-account-disabled").exists(), "setup account retirement did not finish")
    require("AutomaticLoginEnable=False" in Path("/etc/gdm/custom.conf").read_text(), "setup autologin remains enabled")
    run(["systemctl", "start", "mertensia-login-users.service"])
    bus = dbus.SystemBus()
    manager = dbus.Interface(bus.get_object("org.freedesktop.Accounts", "/org/freedesktop/Accounts"), "org.freedesktop.Accounts")
    cached = set()
    for path in manager.ListCachedUsers():
        props = dbus.Interface(bus.get_object("org.freedesktop.Accounts", path), "org.freedesktop.DBus.Properties")
        cached.add(str(props.Get("org.freedesktop.Accounts.User", "UserName")))
    for user in (ACCOUNT, SECOND_ACCOUNT):
        require(user in cached, f"{user} is absent from GDM's AccountsService cache")
        public = json.loads(run(["homectl", "inspect", "--json=short", user]))
        require(public.get("storage") == "luks", f"{user} does not have an encrypted home")
        require(public.get("fileSystemType") == "ext4", f"{user} home filesystem is unexpected")
        require(("wheel" in public.get("memberOf", [])) == (user == ACCOUNT), "account privileges are wrong")
        pam_authenticate(user, password)
        if check_wrong_password:
            pam_authenticate(user, password + "-incorrect", expect_success=False)
            # Clear any failure tally before later reboot/update checks.
            pam_authenticate(user, password)
    # A setup caller must no longer be able to create an administrator.
    environment = os.environ.copy()
    environment["PKEXEC_UID"] = str(pwd.getpwnam("mertensia-setup").pw_uid)
    denied_username = "integration_denied"
    denied_request = {"username": denied_username, "real_name": "Denied Test", "password": password,
                      "firstboot": True, "admin": True, "hostname": "mertensia-integration",
                      "locale": "en_US.UTF-8", "timezone": "UTC", "keymap": "us"}
    denied = subprocess.run(["/usr/libexec/mertensia-accounts-helper"], input=json.dumps(denied_request) + "\n", text=True,
                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    reply = json.loads(denied.stdout.splitlines()[-1])
    require(denied.returncode != 0 and "initial setup is already complete" in reply.get("message", ""),
            "retired setup caller did not receive a lifecycle authorization denial")
    missing = subprocess.run(["homectl", "inspect", denied_username], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    require(missing.returncode != 0, "retired setup caller created an account")
    run(["systemctl", "is-active", "gdm.service"])
    return {"encrypted_homes": True, "admin_and_user_privileges": True, "gdm_pam_authentication": True,
            "incorrect_password_check": check_wrong_password,
            "login_users_cached": True, "setup_retired": True, "gdm_active": True}


def stage_upgrade(image):
    require(role() == "payload", "upgrade requires the installed system")
    require(isinstance(image, str) and image and not image.startswith("-"), "invalid upgrade image")
    before = boot_identity()
    run(["bootc", "switch", "--enforce-container-sigpolicy", image], timeout=2400)
    staged = status().get("status", {}).get("staged")
    require(isinstance(staged, dict), "bootc did not stage the requested upgrade")
    return {"previous_digest": before["digest"], "staged": True, "signature_policy_enforced": True}


def reject_wrong_signer(image):
    """Prove the same signed update is rejected when its trusted key is wrong."""
    require(role() == "payload", "signature rejection requires the installed system")
    require(isinstance(image, str) and image and not image.startswith("-"), "invalid update image")
    path = Path("/etc/containers/policy.json")
    original = path.read_bytes()
    policy = json.loads(original)
    head, separator, tail = image.partition("@")[0].rpartition("/")
    require(bool(separator) and "integration" in tail, "negative signature test requires an integration repository")
    repository = head + "/" + tail.partition(":")[0]
    scope = policy.get("transports", {}).get("docker", {}).get(repository)
    require(isinstance(scope, list) and any(rule.get("type") == "sigstoreSigned" for rule in scope),
            "integration repository has no signature verification rule")
    staged_before = status().get("status", {}).get("staged")
    # An ephemeral unrelated key is generated locally; production private keys
    # are never available to the guest or the test agent.
    private = run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1"])
    public = run(["openssl", "pkey", "-pubout"], input_text=private)
    private = None
    with tempfile.NamedTemporaryFile(mode="w", dir="/run", prefix="integration-wrong-signer-") as key:
        key.write(public + "\n")
        key.flush()
        for rule in scope:
            if rule.get("type") == "sigstoreSigned":
                rule["keyPath"] = key.name
        try:
            # Preserve the inode's production SELinux label and file mode.
            path.write_text(json.dumps(policy) + "\n")
            failed = subprocess.run(["bootc", "switch", "--enforce-container-sigpolicy", image],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2400)
            require(failed.returncode != 0 and "signature" in failed.stderr.lower(),
                    "update was not rejected for signature verification with an incorrect key")
            require(status().get("status", {}).get("staged") == staged_before,
                    "rejected update changed the staged deployment")
        finally:
            path.write_bytes(original)
    return {"incorrect_signing_key_rejected": True, "no_deployment_staged": True, "original_trust_restored": True}


def main():
    guard_guest()
    deadline = time.monotonic() + 120
    while not PORT.exists() and time.monotonic() < deadline:
        time.sleep(1)
    descriptor = os.open(PORT, os.O_RDWR)
    with os.fdopen(os.dup(descriptor), "r", buffering=1) as incoming, os.fdopen(descriptor, "w", buffering=1) as outgoing:
        def send(message):
            outgoing.write(json.dumps(message, separators=(",", ":")) + "\n")
            outgoing.flush()

        send({"type": "hello", "protocol": 1, "role": role()})
        for line in incoming:
            request = json.loads(line)
            identifier = request["id"]
            command = request["command"]
            try:
                if command == "probe":
                    result = live_probe()
                elif command == "install":
                    result = install(send)
                elif command == "root-checks":
                    result = root_checks(request["recovery_key"], expect_tpm=request.get("expect_tpm", True))
                elif command == "create-accounts":
                    result = create_accounts(request["password"])
                elif command == "login-checks":
                    result = login_checks(request["password"], check_wrong_password=request.get("check_wrong_password", False))
                elif command == "reject-update":
                    result = reject_wrong_signer(request["image"])
                elif command == "upgrade":
                    result = stage_upgrade(request["image"])
                elif command == "rollback":
                    require(role() == "payload", "rollback requires the installed system")
                    run(["bootc", "rollback"])
                    result = {"rollback_staged": True}
                elif command == "poweroff":
                    send({"type": "result", "id": identifier, "ok": True, "result": {"poweroff": True}})
                    subprocess.Popen(["systemctl", "poweroff", "--no-block"])
                    return
                else:
                    raise RuntimeError(f"unsupported integration operation: {command}")
                send({"type": "result", "id": identifier, "ok": True, "result": result})
            except Exception as error:
                send({"type": "result", "id": identifier, "ok": False, "error": str(error)})


if __name__ == "__main__":
    main()
