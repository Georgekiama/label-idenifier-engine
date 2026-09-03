"""
ExhibitPro - contract integrity gate

The determinism guarantee is only real if it is mechanical. A contract whose
content moves without its version moving makes every previously produced label
unreproducible and turns the audit ledger into a liar, so this is a build
failure, not a warning.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from contracts import loader  # noqa: E402


def test_contracts_exist():
    assert loader.available(), "no contracts found in contracts/"


def test_every_contract_matches_its_recorded_version():
    ok, problems = loader.verify()
    assert ok, "contract integrity broken:\n  " + "\n  ".join(problems)


def test_contract_versions_are_stamped_into_output():
    """Segment maps must record which contract produced them, or a label cannot
    be reproduced from its ledger entry."""
    import segmenter
    assert segmenter._SEG["version"] == loader.load("segmentation")["version"]


def test_every_weight_is_used_and_every_signal_is_weighted():
    """A weight with no signal is dead config; a signal with no weight crashes
    at runtime, on some document, later."""
    import re
    import segmenter
    src = open(os.path.join(ROOT, "segmenter.py"), encoding="utf-8").read()
    fired = set(re.findall(r'fire\("([a-z_]+)"', src))
    fired |= set(re.findall(r'make_signal\("([a-z_]+)"', src))
    weighted = set(segmenter.WEIGHTS)
    assert fired - weighted == set(), f"signals fired with no weight: {fired - weighted}"
    assert weighted - fired == set(), f"weights defined but never fired: {weighted - fired}"


def test_review_only_signals_are_real_signals():
    import segmenter
    unknown = segmenter.REVIEW_ONLY_SIGNALS - set(segmenter.WEIGHTS)
    assert not unknown, f"review-only names that are not signals: {unknown}"


@pytest.mark.parametrize("policy", ["conservative", "balanced", "aggressive"])
def test_every_policy_is_selectable(policy):
    import segmenter
    original = segmenter.ACTIVE_POLICY
    try:
        th = segmenter.set_policy(policy)
        assert th > 0
        assert segmenter.REFINE_BAND[1] == th
    finally:
        segmenter.set_policy(original)
