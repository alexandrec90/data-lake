"""stable_hash is the privacy mechanism for social authors (Québec rule: stored hashed only)
— pin the algorithm so a refactor can't silently switch to something weaker or recoverable."""

import hashlib

from data_lake.ingestion.base import stable_hash


def test_stable_hash_is_sha256_hex():
    assert stable_hash("some_author") == hashlib.sha256(b"some_author").hexdigest()
    # literal digest, so even a hashlib-misuse refactor can't slip through
    assert stable_hash("a") == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"


def test_stable_hash_is_deterministic_and_fixed_width():
    first, second = stable_hash("alice"), stable_hash("alice")
    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_stable_hash_distinguishes_inputs():
    assert stable_hash("alice") != stable_hash("bob")


def test_stable_hash_handles_unicode():
    assert stable_hash("québec_trader") == hashlib.sha256("québec_trader".encode()).hexdigest()
