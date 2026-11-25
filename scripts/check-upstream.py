#!/usr/bin/env python3
"""Check upstream tools and correlate stable advisories with actual image inventories."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import rpm

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "build-inputs/config.json"
LOCK = ROOT / "build-inputs/lock.json"
BODHI = "https://bodhi.fedoraproject.org/updates/"


def get_json(url):
    with urlopen(Request(url, headers={"Accept": "application/json", "User-Agent": "MertensiaOS-build-inputs"}),
                 timeout=90) as response:
        return json.load(response)


def registry_pin(image):
    manifest = subprocess.check_output(["skopeo", "inspect", "--raw", f"docker://{image}"])
    return "sha256:" + hashlib.sha256(manifest).hexdigest()


def check_tools():
    config = json.loads(CONFIG.read_text())
    release = get_json("https://api.github.com/repos/coreos/chunkah/releases/latest")
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Chunkah did not return a stable version")
    chunkah = f"quay.io/coreos/chunkah:{tag}"
    builder = "ghcr.io/osbuild/image-builder"
    builder_digest = registry_pin(builder + ":latest")
    changed = config | {"chunkah": f"{chunkah}@{registry_pin(chunkah)}",
                        "image_builder": f"{builder}@{builder_digest}"}
    if changed != config:
        CONFIG.write_text(json.dumps(changed, indent=2) + "\n")


def inventory(rows):
    packages = {}
    for row in rows:
        name, epoch, version, release, arch, source = row.split("\t")
        if source.endswith(".src.rpm"):
            source_name, source_version, source_release = source.removesuffix(".src.rpm").rsplit("-", 2)
            source_release = re.sub(r"\.mertensia[0-9]+$", "", source_release)
        else:
            source_name, source_version, source_release = name, version, release
        packages.setdefault((name, arch), []).append(((epoch, version, release),
                                                     (source_name, source_version, source_release)))
    return packages


def bodhi_updates(fedora, names):
    names = sorted(set(names))
    updates = {}
    for start in range(0, len(names), 50):
        page = 1
        batch = ",".join(names[start:start + 50])
        while True:
            query = urlencode({"releases": f"F{fedora}", "status": "stable", "packages": batch,
                               "rows_per_page": 100, "page": page})
            response = get_json(f"{BODHI}?{query}")
            if response["page"] != page or not isinstance(response["pages"], int):
                raise ValueError("Invalid Fedora Bodhi pagination")
            for update in response["updates"]:
                if update["status"] != "stable" or not re.fullmatch(r"FEDORA-[0-9]{4}-[a-f0-9]+", update["alias"]):
                    raise ValueError("Unexpected stable Fedora advisory")
                updates[update["alias"]] = update
            if page >= response["pages"]:
                break
            page += 1
    return updates.values()


def source_compare(left, right):
    return rpm.labelCompare(("0", *left[1:]), ("0", *right[1:]))


def correlate(published, candidate, updates):
    fixed, pending = {}, {}
    for update in updates:
        for build in update["builds"]:
            target = build["nvr"].rsplit("-", 2)
            for key, installed in published.items():
                current = [source for _, source in installed if source[0] == target[0]]
                if not current or any(source_compare(source, target) >= 0 for source in current):
                    continue
                available = [source for _, source in candidate.get(key, ()) if source[0] == target[0]]
                # Install-only packages can retain an old fallback. Check the newest available build.
                covered = any(source_compare(source, target) >= 0 for source in available)
                outcome = fixed if covered else pending
                outcome.setdefault(update["alias"], {"type": update["type"], "packages": set()})
                outcome[update["alias"]]["packages"].add(f"{key[0]}.{key[1]} ({build['nvr']})")
    for alias in fixed.keys() & pending.keys():
        pending[alias]["packages"].update(fixed.pop(alias)["packages"])
    return fixed, pending


def diff(old, new, limit=None):
    removed = [f"- {row}" for row in sorted(set(old) - set(new))]
    added = [f"+ {row}" for row in sorted(set(new) - set(old))]
    lines = removed + added
    if limit and len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more rows in the workflow report"]
    return "\n".join(lines)


def input_rpms(inputs):
    return {f"{stage}\t{rpm['rpm']}\t{rpm['sha256']}" for stage, tx in inputs["transactions"].items()
            for rpm in tx["packages"]}


def compare(args):
    proposed = json.loads(args.draft.read_text())["inputs"]
    locked = json.loads(LOCK.read_text())["inputs"]
    published = args.published.read_text().splitlines()
    baseline = inventory(published)
    candidate = inventory(proposed["payload_inventory"])
    brave_key = ("brave-origin", "x86_64")
    [(brave, _)] = candidate[brave_key]
    for comparison in (baseline, inventory(locked["payload_inventory"])):
        for version, _ in comparison.get(brave_key, ()):
            if rpm.labelCompare(brave, version) < 0:
                raise ValueError("Brave stable resolution would downgrade brave-origin")
    sources = {source[0] for values in baseline.values() for _, source in values
               if not source[0].startswith("brave-")}
    fixed, pending = correlate(baseline, candidate, bodhi_updates(proposed["fedora"], sources))
    alerts = []
    for title, advisories in (("Fixes present in the resolved candidate", fixed),
                              ("Stable advisories awaiting a candidate fix", pending)):
        alerts.append(f"### {title}")
        for alias, record in sorted(advisories.items()):
            packages = ", ".join(sorted(record["packages"]))
            alerts.append(f"- [{alias}]({BODHI}{alias}) ({record['type']}) — {packages}")
        if not advisories:
            alerts.append("None.")
    refs = ["### Container inputs"]
    for name in ("base", "chunkah", "image_builder"):
        if locked[name] != proposed[name]:
            refs.append(f"- {name}: `{locked[name]}` → `{proposed[name]}`")
    if len(refs) == 1:
        refs.append("Unchanged.")
    sections = []
    for title, old, new in (("Locked RPM identities and SHA-256", input_rpms(locked), input_rpms(proposed)),
                            ("Payload inventory against the published baseline", published, proposed["payload_inventory"])):
        sections.append((title, old, new))
    versions = []
    for title, observed in (("Published", baseline), ("Locked", inventory(locked["payload_inventory"])),
                            ("Resolved", candidate)):
        version = ", ".join(f"{epoch}:{version}-{release}" for (epoch, version, release), _ in observed.get(brave_key, ()))
        versions.append(f"- {title}: {version or 'absent'}")
    header = [f"Published baseline `{args.baseline}`.", *refs, "### Brave Origin x86_64", *versions, *alerts]
    full = header[:]
    summary = header[:]
    for title, old, new in sections:
        full.extend([f"### {title}", "```diff", diff(old, new) or "(unchanged)", "```"])
        summary.extend([f"### {title}", "```diff", diff(old, new, 80) or "(unchanged)", "```"])
    args.report.write_text("\n".join(full) + "\n")
    # GitHub caps PR body length. Keep the complete advisory and RPM report on the workflow run.
    body = "\n".join(summary)
    if len(body.encode()) > 50_000:
        body = "\n".join([header[0], *refs, "See the full report for advisory links and package changes.",
                          *[f"- [{alias}]({BODHI}{alias})" for alias in sorted(fixed)[:100]]])
    args.body.write_text(body + "\n")
    print("security=" + str(any(record["type"] == "security" for record in fixed.values())).lower())
    print("changed=" + str(proposed != locked).lower())


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    commands.add_parser("tools")
    report = commands.add_parser("report")
    report.add_argument("draft", type=Path)
    report.add_argument("published", type=Path)
    report.add_argument("baseline")
    report.add_argument("report", type=Path)
    report.add_argument("body", type=Path)
    args = cli.parse_args()
    try:
        if args.action == "tools":
            check_tools()
        else:
            compare(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        cli.exit(1, f"Upstream check failed: {error}\n")


if __name__ == "__main__":
    main()
