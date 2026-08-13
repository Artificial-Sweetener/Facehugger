"""SHA-256 validation and binary conversion."""

import re

from facehugger.errors import InvalidHashError

_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)


def normalize_sha256(value: str) -> str:
    """Validate and normalize one complete SHA-256 digest."""
    if not _SHA256_PATTERN.fullmatch(value):
        raise InvalidHashError("A SHA-256 digest must contain exactly 64 hexadecimal characters.")
    return value.lower()


def sha256_bytes(value: str) -> bytes:
    """Return one validated SHA-256 digest in its 32-byte binary representation."""
    return bytes.fromhex(normalize_sha256(value))
