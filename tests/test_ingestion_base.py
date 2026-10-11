"""stable_hash is the privacy mechanism for social authors (Québec rule: stored hashed only)
— pin the algorithm so a refactor can't silently switch to something weaker or recoverable."""

import hashlib

import pytest

from data_lake.ingestion.base import AllItemsFailedError, run_each, stable_hash


def test_stable_hash_is_sha256_hex():
    assert stable_hash("some_author") == hashlib.sha256(b"some_author").hexdigest()
    # literal digest, so even a hashlib-misuse refactor can't slip through. Bound to a
    # name rather than inlined so the allowlist pragma has a line short enough to stay
    # on: `ruff format` wraps an over-long assert into parentheses, which moves a
    # trailing comment onto the closing paren — and detect-secrets reads the finding's
    # own line only, so the suppression would silently stop applying.
    sha256_of_a = "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"  # pragma: allowlist secret
    assert stable_hash("a") == sha256_of_a


def test_stable_hash_is_deterministic_and_fixed_width():
    first, second = stable_hash("alice"), stable_hash("alice")
    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_stable_hash_distinguishes_inputs():
    assert stable_hash("alice") != stable_hash("bob")


def test_stable_hash_handles_unicode():
    assert stable_hash("québec_trader") == hashlib.sha256("québec_trader".encode()).hexdigest()


def _work(item: str) -> int:
    if item.startswith("bad"):
        raise RuntimeError(f"{item} exploded")
    return len(item)


def test_run_each_sums_and_tolerates_some_failures(caplog):
    assert run_each(["ab", "bad1", "abc"], _work, what="thing") == 5
    assert "thing bad1 failed: bad1 exploded" in caplog.text


def test_run_each_raises_when_every_item_failed():
    with pytest.raises(AllItemsFailedError, match=r"every thing failed \(bad1, bad2\)") as info:
        run_each(["bad1", "bad2"], _work, what="thing")
    assert str(info.value.__cause__) == "bad1 exploded"


def test_run_each_with_no_items_is_zero():
    assert run_each([], _work, what="thing") == 0


def test_run_each_lets_a_bug_abort_the_batch_at_once():
    calls = []

    def buggy(item: str) -> int:
        calls.append(item)
        raise TypeError("a bug, not a provider failure")

    with pytest.raises(TypeError):
        run_each(["a", "b"], buggy, what="thing")
    assert calls == ["a"]
