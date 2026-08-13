"""SHA-256 input validation tests."""

import pytest

from facehugger.errors import InvalidHashError
from facehugger.hashes import normalize_sha256, sha256_bytes


def test_normalize_sha256_lowercases_valid_digest() -> None:
    """Uppercase hexadecimal input normalizes without changing its bytes."""
    digest = "A" * 64
    assert normalize_sha256(digest) == "a" * 64
    assert sha256_bytes(digest) == b"\xaa" * 32


@pytest.mark.parametrize("value", ["", "a" * 63, "sha256:" + "a" * 64, "a" * 63 + " "])
def test_normalize_sha256_rejects_noncanonical_input(value: str) -> None:
    """Only one complete, unprefixed hexadecimal digest is accepted."""
    with pytest.raises(InvalidHashError):
        normalize_sha256(value)
