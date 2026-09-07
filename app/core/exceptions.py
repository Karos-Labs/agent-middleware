"""Domain-level exceptions and their HTTP mapping."""

from __future__ import annotations


class MiddlewareError(Exception):
    """Base class for all errors raised by this service."""


class MessagePublishError(MiddlewareError):
    """Raised when publishing a message to Pub/Sub fails."""

    def __init__(self, topic: str, cause: Exception | None = None) -> None:
        self.topic = topic
        self.cause = cause
        super().__init__(f"Failed to publish message to topic '{topic}': {cause}")


class ResourceNotFoundError(MiddlewareError):
    """Raised when a requested resource does not exist (or was logically deleted)."""

    def __init__(self, resource: str, identifier: object) -> None:
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} '{identifier}' was not found")


class ResourceConflictError(MiddlewareError):
    """Raised when a write would violate a uniqueness or state invariant."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidStateError(MiddlewareError):
    """Raised when an operation is not valid for the current state of a resource."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class IncompleteAgentConfigurationError(MiddlewareError):
    """Raised when an agent cannot produce a job payload (e.g. no active prompt)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)

class ValidationRefusedError(MiddlewareError):
    """Raised when a version cannot be published, carrying EVERY reason.

    A list rather than a message. A 40-step version validated one refusal at a
    time is forty round trips, and the author fixes the first problem, tries
    again, and learns about the second -- which is how "publish" acquires a
    reputation for being unpredictable when it is being perfectly consistent.
    """

    def __init__(self, message: str, problems: list[dict[str, object]]) -> None:
        super().__init__(message)
        self.problems = problems


class ServiceUnavailableError(MiddlewareError):
    """Raised when a dependency this route needs is not configured.

    Distinct from a 500: nothing is broken. The configuration database has no
    DSN in this environment yet, which is a deployment state, and saying so is
    more useful than a stack trace.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
