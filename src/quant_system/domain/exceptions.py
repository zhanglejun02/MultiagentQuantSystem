from __future__ import annotations


class QuantSystemError(Exception):
    """Base for all domain errors."""


class PITViolationError(QuantSystemError):
    """A query attempted to read data not yet available at as_of."""


class ImmutabilityError(QuantSystemError):
    """Attempted to mutate or re-register an immutable artifact."""


class ArtifactNotFoundError(QuantSystemError):
    pass


class ContentHashMismatchError(QuantSystemError):
    pass


class InvalidTransitionError(QuantSystemError):
    """Illegal experiment/order state transition."""


class MarketRuleViolationError(QuantSystemError):
    pass


class RiskRejectedError(QuantSystemError):
    pass


class InsufficientCashError(QuantSystemError):
    pass


class InsufficientPositionError(QuantSystemError):
    pass


class LedgerInvariantError(QuantSystemError):
    """Accounting invariant broken; fail closed."""


class ReconciliationError(QuantSystemError):
    pass


class KillSwitchEngagedError(QuantSystemError):
    pass


class ReplayMismatchError(QuantSystemError):
    pass
