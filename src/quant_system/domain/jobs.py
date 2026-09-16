"""JobRunner port. Only LocalJobRunner is implemented in V1; domain code must
not couple to any specific future task system."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from quant_system.domain.types import new_id, utc_now


@dataclass
class JobResult:
    job_id: str
    status: str  # SUCCEEDED | FAILED
    result: Any = None
    error: str | None = None
    started_at: str = ""
    ended_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class JobRunner(Protocol):
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> JobResult: ...


class LocalJobRunner:
    """Synchronous in-process runner."""

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> JobResult:
        job_id = new_id("job_")
        started = utc_now().isoformat()
        try:
            result = fn(*args, **kwargs)
            return JobResult(job_id=job_id, status="SUCCEEDED", result=result,
                             started_at=started, ended_at=utc_now().isoformat())
        except Exception as exc:  # noqa: BLE001 - captured in result, not swallowed silently
            return JobResult(job_id=job_id, status="FAILED", error=f"{type(exc).__name__}: {exc}",
                             started_at=started, ended_at=utc_now().isoformat())
