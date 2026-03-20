"""Domain-layer exceptions used by aggregates and command handlers."""


class DomainError(Exception):
    """Base class for domain validation failures."""


class InvalidStateTransition(DomainError):
    """Raised when a command attempts an out-of-order lifecycle transition."""


class BusinessRuleViolation(DomainError):
    """Raised when a command violates a business invariant."""
