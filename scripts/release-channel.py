#!/usr/bin/env python3
"""Record candidate metadata and move release channels by verified digest."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPO = "ghcr.io/mertensiaos/mertensiaos"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PREFIX = "org.mertensiaos."


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def verify(image, destination=None):
    args = ["bash", str(ROOT / "scripts/verify-release.sh"), image]
    if destination is not None:
        args.append(destination)
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def resolve(channel, *, optional=False):
    command = ["skopeo", "inspect", "--no-tags", "--format", "{{.Digest}}", f"docker://{REPO}:{channel}"]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        if optional and re.search(r"manifest[ _]unknown|name[ _]unknown", result.stderr, re.I):
            return None
        raise ValueError(f"Unable to resolve {channel}: {result.stderr.strip()}")
    value = result.stdout.strip()
    if not DIGEST.fullmatch(value):
        raise ValueError(f"Invalid digest for {channel}")
    return value


def labels(digest):
    data = json.loads(output("skopeo", "inspect", "--config", f"docker://{REPO}@{digest}"))
    return data["config"].get("Labels") or {}


def release_order(config):
    value = config.get(PREFIX + "release-order")
    if value is None:
        return (0, 0)  # Existing signed release from before ordered candidates.
    if not re.fullmatch(r"[1-9][0-9]*\.[1-9][0-9]*", value):
        raise ValueError("Invalid candidate release order")
    return tuple(map(int, value.split(".")))


def baseline():
    latest = resolve("latest", optional=True)
    channel = "latest" if latest else "testing"
    digest = latest or resolve("testing")
    verify(f"{REPO}@{digest}")
    return channel, digest


def promote(channel, digest):
    image = f"{REPO}@{digest}"
    verify(image)
    candidate = labels(digest)
    latest = resolve("latest", optional=True)
    testing = resolve("testing", optional=True)
    if latest == digest:
        raise ValueError("Candidate is already stable")
    previous = candidate.get(PREFIX + "upgrade-baseline", "")
    baseline_channel, _, baseline_digest = previous.partition("@")
    if baseline_channel not in {"latest", "testing"} or not DIGEST.fullmatch(baseline_digest):
        raise ValueError("Candidate is missing its signed upgrade baseline")
    if latest:
        if (baseline_channel, baseline_digest) != ("latest", latest):
            raise ValueError("Stable changed since this candidate's upgrade gate ran")
        verify(f"{REPO}@{latest}")
    elif baseline_channel != "testing":
        raise ValueError("Stable was deleted after the candidate's upgrade gate ran")
    else:
        verify(f"{REPO}@{baseline_digest}")
        if channel == "testing" and testing != baseline_digest:
            raise ValueError("Initial testing baseline changed since the upgrade gate ran")
    if channel == "latest" and testing != digest:
        raise ValueError("Stable promotion requires the current passing testing digest")
    if channel == "testing" and testing == digest:
        return
    order = release_order(candidate)
    if order <= release_order(labels(latest or baseline_digest)):
        raise ValueError("Candidate is older than the upgrade baseline")
    if channel == "testing" and testing:
        verify(f"{REPO}@{testing}")
        if order <= release_order(labels(testing)):
            raise ValueError("Candidate is older than the testing release")
    verify(image, f"docker://{REPO}:{channel}")


def metadata(digest, destination):
    image = f"{REPO}@{digest}"
    local = "localhost/mertensiaos:metadata-candidate"
    verify(image, f"containers-storage:{local}")
    config = labels(digest)
    lock = json.loads((ROOT / "build-inputs/lock.json").read_bytes())["inputs"]
    revision = hashlib.sha256((json.dumps(lock, indent=2, sort_keys=True) + "\n").encode()).hexdigest()
    if config[PREFIX + "input-lock"] != revision:
        raise ValueError("Signed candidate is not built from the release lock")
    rows = output("podman", "run", "--rm", "--network=none", "--entrypoint", "/usr/bin/rpm", local,
                  "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCERPM}\n")
    packages = sorted(row for row in rows.splitlines() if not row.startswith("gpg-pubkey\t"))
    if packages != lock["payload_inventory"]:
        raise ValueError("Final signed candidate inventory differs from the release lock")
    data = {
        "schema": 1,
        "image": image,
        "oci_digest": digest,
        "source_commit": config["org.opencontainers.image.revision"],
        "lock_revision": config[PREFIX + "input-lock"],
        "upgrade_baseline": config[PREFIX + "upgrade-baseline"],
        "package_inventory": packages,
        "fedora_bootc": lock["base"],
        "chunkah": lock["chunkah"],
        "image_builder": lock["image_builder"],
    }
    destination.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=("baseline", "testing", "latest", "metadata"))
    cli.add_argument("digest", nargs="?")
    cli.add_argument("destination", nargs="?", type=Path)
    args = cli.parse_args()
    try:
        if args.action == "baseline":
            channel, digest = baseline()
            print(f"channel={channel}\ndigest={digest}")
        else:
            if not args.digest or not DIGEST.fullmatch(args.digest):
                cli.error("Supply a sha256 digest")
            if args.action == "metadata":
                if args.destination is None:
                    cli.error("Supply the destination for release metadata")
                metadata(args.digest, args.destination)
            else:
                promote(args.action, args.digest)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        cli.exit(1, f"Release: {error}\n")


if __name__ == "__main__":
    main()
