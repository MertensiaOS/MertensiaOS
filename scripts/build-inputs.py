#!/usr/bin/env python3
"""Resolve, retain and consume MertensiaOS build inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "build-inputs/config.json"
LOCK = ROOT / "build-inputs/lock.json"
REPOSITORY = "ghcr.io/mertensiaos/mertensiaos-integration"
TRANSACTIONS = {
    *(f"{name}-{step}" for name in ("accountsservice", "gnome-control-center", "skopeo")
      for step in ("tools", "source", "builddep")),
    "fedora-update", "payload-core", "payload-desktop", "payload-skopeo", "payload-apps", "brave", "installer",
}
RECIPE = ("Containerfile", "Containerfile.installer", "scripts/locked-rpms.py",
          "scripts/build-accountsservice.sh", "scripts/build-gnome-control-center.sh", "scripts/build-skopeo.sh")
RPM_QUERY = "%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCERPM}\n"


def canonical(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def output(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def recipe():
    return {name: checksum(ROOT / name) for name in RECIPE}


def input_id(lock):
    return digest(canonical(lock["inputs"]))


def read_lock(path=LOCK):
    lock = json.loads(path.read_bytes())
    if lock["schema"] != 1 or set(lock["inputs"]["transactions"]) != TRANSACTIONS:
        raise ValueError("Incomplete or unsupported build-input lock")
    if not re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[a-f0-9]{64}", lock["bundle"]):
        raise ValueError("Build-input bundle must use an immutable digest in the official repository")
    inputs = lock["inputs"]
    if not re.fullmatch(r"quay\.io/fedora/fedora-bootc@sha256:[a-f0-9]{64}", inputs["base"]):
        raise ValueError("Invalid Fedora bootc pin")
    return lock


def verify_snapshot(lock, directory):
    for name, transaction in lock["inputs"]["transactions"].items():
        for item in transaction["packages"]:
            filename = item["file"]
            if not re.fullmatch(r"[a-zA-Z0-9_+.~^%-]+\.rpm", filename):
                raise ValueError(f"Invalid locked RPM filename: {filename}")
            rpm = directory / name / filename
            if rpm.is_symlink() or checksum(rpm) != item["sha256"]:
                raise ValueError(f"Locked RPM checksum mismatch: {rpm}")


def acquire(lock, directory):
    image = lock["bundle"]
    run("podman", "pull", image)
    container = output("podman", "create", "--entrypoint", "/unused", image)
    try:
        run("podman", "cp", f"{container}:/rpms/.", str(directory))
    finally:
        run("podman", "rm", container)


def get_inventory(image):
    rows = output("podman", "run", "--rm", "--network=none", "--entrypoint", "/usr/bin/rpm",
                  image, "-qa", "--qf", RPM_QUERY)
    return sorted(row for row in rows.splitlines() if not row.startswith("gpg-pubkey\t"))


def build(kind, image, parent, snapshot, lock, *, refresh=False, args=()):
    release = input_id(lock) if not refresh else "refresh"
    options = ["--security-opt", "label=disable", "--build-arg", f"INPUT_LOCK_REVISION={release}"]
    if refresh:
        options += ["--no-cache", "--build-arg", "RPM_INPUT_MODE=refresh"]
    else:
        options += ["--network=none"]
    mode = "rw" if refresh else "ro"
    options += ["--volume", f"{snapshot.resolve()}:/run/mertensia-lock:{mode}"]
    if kind == "payload":
        if not refresh:
            config = json.loads(CONFIG.read_text())
            if any(lock["inputs"][name] != config[name] for name in config):
                raise ValueError("Configure tool/Fedora updates through the lock refresh")
        options += ["--build-arg", f"FEDORA_BASE={lock['inputs']['base']}"]
        containerfile = "Containerfile"
    else:
        options += ["--build-arg", f"BASE_IMAGE={parent}"]
        containerfile = "Containerfile.installer"
    run("podman", "build", *options, *args, "-f", containerfile, "-t", image, ".")


def normal_build(args):
    lock = read_lock()
    if lock["inputs"]["recipe"] != recipe():
        raise ValueError("RPM build recipe changed; refresh build-inputs/lock.json")
    options = ["--jobs", args.jobs, "--pull=missing"]
    if args.kind == "installer":
        if args.mode == "production":
            observed = output("podman", "image", "inspect", "--format",
                              '{{index .Config.Labels "org.mertensiaos.input-lock"}}', args.parent)
            if observed != input_id(lock):
                raise ValueError("Production payload and installer must use the same build-input lock")
        options += ["--build-arg", f"SOURCE_IMAGE={args.source or args.parent}",
                    "--build-arg", f"TARGET_IMAGE={args.target or args.parent}", "--build-arg", f"BUILD_MODE={args.mode}"]
    with tempfile.TemporaryDirectory(prefix="mertensia-rpms-") as work:
        directory = Path(work) / "rpms"
        if args.snapshot:
            shutil.copytree(args.snapshot, directory)
        else:
            directory.mkdir()
            acquire(lock, directory)
        verify_snapshot(lock, directory)
        (directory / "manifest.json").write_bytes(canonical(lock["inputs"]))
        options += [item for label in args.label for item in ("--label", label)]
        build(args.kind, args.image, args.parent, directory, lock, args=options)
    if args.kind == "payload" and get_inventory(args.image) != lock["inputs"]["payload_inventory"]:
        raise ValueError("Payload package inventory differs from the build-input lock")


def base_for(config):
    reference = f"quay.io/fedora/fedora-bootc:{config['fedora']}"
    data = json.loads(output("skopeo", "inspect", "--raw", f"docker://{reference}"))
    [manifest] = [item for item in data["manifests"]
                  if item["platform"]["architecture"] == "amd64" and item["platform"]["os"] == "linux"]
    return reference.split(":")[0] + "@" + manifest["digest"]


def collect(snapshot, draft, config, base, payload):
    lock = {"schema": 1, "inputs": config | {"base": base, "recipe": recipe()}}
    lock["inputs"]["transactions"] = {
        name: json.loads((snapshot / name / "transaction.json").read_text()) for name in sorted(TRANSACTIONS)
    }
    lock["inputs"]["payload_inventory"] = get_inventory(payload)
    verify_snapshot(lock, snapshot)
    draft.write_bytes(canonical(lock))


def refresh(args):
    config = json.loads(CONFIG.read_text())
    base = base_for(config)
    snapshot = args.snapshot.resolve()
    snapshot.mkdir(parents=True)
    lock = {"inputs": {"base": base}}
    payload = "localhost/mertensiaos:input-refresh"
    build("payload", payload, None, snapshot, lock, refresh=True, args=("--jobs", args.jobs))
    build("installer", "localhost/mertensiaos:installer-input-refresh", payload, snapshot, lock, refresh=True)
    collect(snapshot, args.draft, config, base, payload)


def retain(args):
    lock = json.loads(args.draft.read_text())
    if LOCK.exists():
        previous = read_lock()
        if previous["inputs"] == lock["inputs"]:
            print("Build inputs are unchanged")
            return
    verify_snapshot(lock, args.snapshot)
    tag = f"{REPOSITORY}:lock-{input_id(lock)}"
    with tempfile.TemporaryDirectory(prefix="mertensia-bundle-") as work:
        context = Path(work) / "image"
        context.mkdir()
        # Reuse the same inode for identical artifacts across transactions.
        files = {}
        lines = ["FROM scratch"]
        for name, transaction in sorted(lock["inputs"]["transactions"].items()):
            group = context / name
            group.mkdir()
            for item in transaction["packages"]:
                path = group / item["file"]
                previous = files.get(item["sha256"])
                if previous:
                    os.link(previous, path)
                else:
                    shutil.copyfile(args.snapshot / name / item["file"], path)
                    files[item["sha256"]] = path
            if transaction["packages"]:
                lines.append(f"COPY {name}/ /rpms/{name}/")
        (context / "Containerfile").write_text("\n".join(lines) + "\n")
        local = "localhost/mertensiaos:input-bundle"
        run("podman", "build", "--network=none", "-t", local, str(context))
        result = Path(work) / "digest"
        run("podman", "push", "--digestfile", str(result), local, f"docker://{tag}")
        lock["bundle"] = f"{REPOSITORY}@{result.read_text().strip()}"
    # Check the anonymously readable retention copy before changing the Git lock.
    with tempfile.TemporaryDirectory(prefix="mertensia-bundle-check-") as work:
        run("skopeo", "inspect", "--raw", "--no-creds", f"docker://{lock['bundle']}")
        directory = Path(work)
        acquire(lock, directory)
        verify_snapshot(lock, directory)
    temporary = LOCK.with_suffix(".tmp")
    temporary.write_bytes(canonical(lock))
    temporary.replace(LOCK)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("kind", choices=("payload", "installer"))
    build_parser.add_argument("image")
    build_parser.add_argument("--parent")
    build_parser.add_argument("--source")
    build_parser.add_argument("--target")
    build_parser.add_argument("--mode", choices=("development", "production"), default="development")
    build_parser.add_argument("--snapshot", type=Path)
    build_parser.add_argument("--jobs", default="2")
    build_parser.add_argument("--label", action="append", default=[])
    for action in ("refresh", "retain"):
        command = commands.add_parser(action)
        command.add_argument("snapshot", type=Path)
        command.add_argument("draft", type=Path)
        if action == "refresh":
            command.add_argument("--jobs", default="2")
    commands.add_parser("revision")
    get = commands.add_parser("get")
    get.add_argument("name", choices=("base", "fedora", "chunkah", "image_builder"))
    args = cli.parse_args()
    try:
        if args.action == "build":
            if args.kind == "installer" and not args.parent:
                cli.error("an installer build requires --parent")
            normal_build(args)
        elif args.action == "refresh":
            refresh(args)
        elif args.action == "retain":
            retain(args)
        elif args.action == "revision":
            print(input_id(read_lock()))
        else:
            print(read_lock()["inputs"][args.name])
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        cli.exit(1, f"Build inputs: {error}\n")


if __name__ == "__main__":
    main()
