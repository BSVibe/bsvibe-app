"""Domain exception hierarchy for the knowledge module."""


class KnowledgeError(Exception):
    """Base exception for all knowledge-module domain errors."""


class VaultPathError(KnowledgeError):
    """Raised when a path traversal attempt is detected."""


class SafeModeError(KnowledgeError):
    """Raised when the safe mode system encounters a failure."""
