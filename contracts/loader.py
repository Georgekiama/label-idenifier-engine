"""
ExhibitPro - contract loader and integrity gate

Contracts are DATA, not code. Every threshold, weight and policy the engine
depends on lives in contracts/*.yaml so it can be reviewed and diffed as the
decision it is, rather than buried in a Python constant.

The integrity rule
------------------
A contract's declared `version` must change whenever its content changes.
Otherwise two runs of "SEG-1.0.0" can silently disagree, and every label the
engine ever produced becomes unreproducible - which would make the audit ledger
a liar. `contracts/manifest.json` records the content hash of each contract at
the moment its version was declared, and verify() compares them.

This is enforced by tests/test_contracts.py, not by discipline.

Workflow when you change a contract:
    1. edit contracts/<name>.yaml
    2. bump its `version`
    3. python tools/verify_contracts.py --update
    4. commit the contract and the manifest together
"""

import hashlib
import json
import os

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise ImportError("PyYAML is required to load engine contracts: pip install pyyaml") from e

CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(CONTRACTS_DIR, "manifest.json")

_cache = {}


def contract_path(name):
    return os.path.join(CONTRACTS_DIR, f"{name}.yaml")


def content_hash(name):
    """sha256[:16] of the contract file's raw bytes, newline-normalised.

    Normalising line endings means a Windows checkout and a Linux CI runner
    agree on the hash; without it the gate would fail on checkout style rather
    than on content.
    """
    with open(contract_path(name), "rb") as f:
        raw = f.read()
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()[:16]


def load(name):
    """Load and cache a contract by name."""
    if name not in _cache:
        with open(contract_path(name), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "version" not in data:
            raise ValueError(f"contract {name} is malformed or has no version")
        data["_content_hash"] = content_hash(name)
        _cache[name] = data
    return _cache[name]


def available():
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(CONTRACTS_DIR)
        if f.endswith(".yaml")
    )


def read_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return {}
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def write_manifest():
    manifest = {}
    for name in available():
        c = load(name)
        manifest[name] = {"version": c["version"], "content_hash": content_hash(name)}
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    return manifest


def verify():
    """Return (ok, problems). A contract whose bytes moved without its version
    moving is the failure this exists to catch."""
    manifest = read_manifest()
    problems = []
    if not manifest:
        return False, ["contracts/manifest.json is missing - run tools/verify_contracts.py --update"]
    for name in available():
        c = load(name)
        recorded = manifest.get(name)
        if recorded is None:
            problems.append(f"{name}: not in manifest")
            continue
        now = content_hash(name)
        if now != recorded["content_hash"]:
            if c["version"] == recorded["version"]:
                problems.append(
                    f"{name}: content changed but version is still {c['version']} - "
                    f"bump the version, then run tools/verify_contracts.py --update"
                )
            else:
                problems.append(
                    f"{name}: version moved {recorded['version']} -> {c['version']} but the "
                    f"manifest was not refreshed - run tools/verify_contracts.py --update"
                )
    for name in manifest:
        if name not in available():
            problems.append(f"{name}: in manifest but the contract file is gone")
    return (not problems), problems


def versions():
    """Version string of every contract, for stamping into engine output."""
    return {name: load(name)["version"] for name in available()}
