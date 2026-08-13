"""Domain-specific exceptions raised by Facehugger."""


class FacehuggerError(Exception):
    """Base class for expected Facehugger failures."""


class InvalidHashError(FacehuggerError, ValueError):
    """Raised when a value is not one complete SHA-256 digest."""


class IndexUnavailableError(FacehuggerError):
    """Raised when the static index cannot be retrieved or found in cache."""


class IndexIntegrityError(FacehuggerError):
    """Raised when a manifest or shard violates the lookup protocol."""


class MetadataError(FacehuggerError):
    """Raised when Hub metadata cannot be inspected safely."""


class ProofStopError(FacehuggerError):
    """Raised when proof evidence reaches a mandatory stop condition."""
