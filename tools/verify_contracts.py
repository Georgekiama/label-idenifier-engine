"""
ExhibitPro - contract integrity check

A contract's declared version must move whenever its content moves. If it does
not, two runs of the same declared version can disagree, and every label the
engine has produced becomes unreproducible - which makes the audit ledger a
liar. This is the one guarantee the whole determinism story rests on, so it is
enforced mechanically rather than by memory.

    python tools/verify_contracts.py            # check   (exit 1 on drift)
    python tools/verify_contracts.py --update   # re-record after a version bump
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import loader  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Verify engine contract integrity")
    ap.add_argument("--update", action="store_true",
                    help="re-record the manifest after a deliberate version bump")
    args = ap.parse_args()

    names = loader.available()
    if not names:
        print("No contracts found.", file=sys.stderr)
        return 2

    if args.update:
        manifest = loader.write_manifest()
        print("Manifest updated:")
        for name in sorted(manifest):
            e = manifest[name]
            print(f"  {name:16s} {e['version']:12s} {e['content_hash']}")
        return 0

    ok, problems = loader.verify()
    print(f"Contracts ({len(names)}):")
    for name in names:
        c = loader.load(name)
        print(f"  {name:16s} {c['version']:12s} {c['_content_hash']}")
    if ok:
        print("\nOK - every contract matches its recorded version.")
        return 0
    print("\nFAIL - contract integrity broken:", file=sys.stderr)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
