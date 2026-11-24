#!/usr/bin/env python3
"""Capture or replay one RPM transaction inside the pinned Fedora container."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

LOCK = Path("/var/mnt")
GPG = Path("/usr/share/pki/rpm-gpg/RPM-GPG-KEY-fedora-$release-x86_64")
BRAVE_KEY = "/run/mertensia-brave.asc"
QUERY = "%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCERPM}\n"


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def inventory():
    return sorted(row for row in output("rpm", "-qa", "--qf", QUERY).splitlines()
                  if not row.startswith("gpg-pubkey\t"))


def describe(path):
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"file": path.name, "sha256": checksum, "rpm": output("rpm", "-qp", "--qf", QUERY, str(path))}


def check_signatures(paths, key):
    with tempfile.TemporaryDirectory() as db:
        subprocess.run(["rpmkeys", "--dbpath", db, "--import", str(key)], check=True)
        command = ["rpmkeys", "--dbpath", db, "--define", "_pkgverify_level signature", "--checksig"]
        for start in range(0, len(paths), 100):
            subprocess.run(command + [str(path) for path in paths[start:start + 100]], check=True,
                           stdout=subprocess.DEVNULL)


def main():
    mode, name, action, *args = sys.argv[1:]
    if mode not in {"locked", "refresh"}:
        raise SystemExit("RPM input mode must be locked or refresh")
    directory = LOCK / name
    source = action == "source"
    destination = Path(args.pop(0)) if source else None
    before = inventory()
    if mode == "refresh":
        directory.mkdir()
        if source:
            subprocess.run(["dnf", "--setopt=skip_if_unavailable=False", "download", "--source",
                            "--destdir", str(directory), *args], check=True)
        else:
            # Use a per-stage cache. Start every transaction with no cached RPMs.
            cache = Path("/var/cache/libdnf5")
            for path in cache.rglob("*.rpm"):
                path.unlink()
            brave = ["--best", "--repofrompath=brave-browser,https://brave-browser-rpm-release.s3.brave.com/$basearch",
                     "--setopt=brave-browser.gpgcheck=1", f"--setopt=brave-browser.gpgkey=file://{BRAVE_KEY}",
                     "--setopt=brave-browser.includepkgs=brave-origin,brave-keyring"] if name == "brave" else []
            subprocess.run(["dnf", "-y", "--setopt=skip_if_unavailable=False", "--setopt=keepcache=True",
                            *brave, action, *args], check=True)
            for path in cache.rglob("*.rpm"):
                shutil.move(path, directory / path.name)
        packages = [describe(path) for path in sorted(directory.glob("*.rpm"))]
    else:
        transaction = json.loads((LOCK / "manifest.json").read_text())["transactions"][name]
        packages = transaction["packages"]
        paths = [directory / item["file"] for item in packages]
        if any(describe(path) != item for path, item in zip(paths, packages)):
            raise SystemExit(f"Locked RPM mismatch in {name}")

    fedora = []
    brave = []
    for item in packages:
        path = directory / item["file"]
        if item["rpm"].split("\t")[0] in {"brave-origin", "brave-keyring"}:
            brave.append(path)
        else:
            fedora.append(path)
    if fedora:
        key = Path(str(GPG).replace("$release", output("rpm", "-E", "%fedora")))
        check_signatures(fedora, key)
    if brave:
        check_signatures(brave, BRAVE_KEY)

    if source:
        if len(packages) != 1 or packages[0]["rpm"].split("\t")[0] != args[0]:
            raise SystemExit(f"Expected exactly one source RPM for {args[0]}")
        shutil.copyfile(directory / packages[0]["file"], destination / packages[0]["file"])
    elif mode == "locked":
        # Our three locally rebuilt RPM sets are explicit inputs from sibling stages.
        local = [arg for arg in args if arg.startswith("/") and arg.endswith(".rpm") and action == "install"]
        if packages or local:
            subprocess.run(["dnf", "-y", "--disablerepo=*", "--setopt=localpkg_gpgcheck=False",
                            "install", *[str(directory / item["file"]) for item in packages], *local], check=True)

    after = inventory()
    # Only record the delta, so independent upstream changes do not churn every transaction.
    delta = {"packages": packages, "added": sorted(set(after) - set(before)),
             "removed": sorted(set(before) - set(after))}
    if mode == "refresh":
        (directory / "transaction.json").write_text(json.dumps(delta, sort_keys=True) + "\n")
    elif delta != transaction:
        raise SystemExit(f"RPM transaction drift in {name}: {delta['added']=} {delta['removed']=}")


if __name__ == "__main__":
    main()
