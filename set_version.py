#!/usr/bin/env python3
"""Set the one version a release has, or check that everything agrees on it:

    ./set_version.py X.Y.Z
    ./set_version.py --check X.Y.Z

Setting rewrites an existing vtech-link pin but doesn't add one. --check needs the pin.
"""
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CARGO = ROOT / "vtech-link/Cargo.toml"
LOCKS = (ROOT / "vtech-link/Cargo.lock", ROOT / "vtech-link/fuzz/Cargo.lock")
MANIFEST = ROOT / "custom_components/vtech_baby/manifest.json"
LOCKED_CRATES = ("vtech-link-cli", "vtech-link-core", "vtech-link-py", "vtech-link-sim")
PIN = "vtech-link=="


def set_version(version: str):
    text = CARGO.read_text()
    text, count = re.subn(r'(\[workspace\.package\]\nversion = )"[^"]*"', rf'\1"{version}"', text)
    if count != 1:
        sys.exit(f"no workspace version in {CARGO}")
    CARGO.write_text(text)

    names = "|".join(LOCKED_CRATES)
    for lock in LOCKS:
        text = re.sub(rf'(name = "(?:{names})"\nversion = )"[^"]*"', rf'\1"{version}"', lock.read_text())
        lock.write_text(text)

    manifest = json.loads(MANIFEST.read_text())
    manifest["version"] = version
    manifest["requirements"] = [f"{PIN}{version}" if r.startswith(PIN) else r for r in manifest["requirements"]]
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


def check(version: str) -> list[str]:
    found = {
        "vtech-link/Cargo.toml": tomllib.loads(CARGO.read_text())["workspace"]["package"]["version"],
        "manifest.json version": json.loads(MANIFEST.read_text())["version"],
    }
    for lock in LOCKS:
        for package in tomllib.loads(lock.read_text())["package"]:
            if package["name"] in LOCKED_CRATES:
                found[f"{lock.relative_to(ROOT)} {package['name']}"] = package["version"]
    problems = [f"{where} is {value}" for where, value in found.items() if value != version]
    if f"{PIN}{version}" not in json.loads(MANIFEST.read_text())["requirements"]:
        problems.append(f"manifest.json doesn't require {PIN}{version}")
    return problems


def main():
    args = sys.argv[1:]
    checking = args[:1] == ["--check"]
    args = args[1:] if checking else args
    if len(args) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        sys.exit(__doc__)
    if not checking:
        set_version(args[0])
    elif problems := check(args[0]):
        sys.exit("\n".join(problems))


main()
