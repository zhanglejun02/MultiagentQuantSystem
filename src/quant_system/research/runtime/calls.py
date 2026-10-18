"""Shared, domain-neutral model budgets, audit receipts, and structured calls."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import BoundedSemaphore, Event, RLock
from time import monotonic
from typing import Any, Literal, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    computed_field,
    model_validator,
)

from quant_system.agents.config import AgentLLMConfig, AgentRoleConfig
from quant_system.agents.llm import CompletionGateway, LLMGateway, LLMMessage, LLMResponse
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import new_id

Result = TypeVar("Result")


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_calls: int = Field(default=30, ge=0)
    max_tokens: int = Field(default=200_000, ge=0)
    timeout_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    max_parallel_calls: int = Field(default=3, ge=1)


@dataclass(frozen=True)
class Reservation:
    sequence: int
    tokens: int
    _ledger: BudgetLedger = field(repr=False, compare=False)


class BudgetLedger:
    """Charge all ancestors once under a root lock; never hold it during I/O."""

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        clock: Callable[[], float] = monotonic,
        parent: BudgetLedger | None = None,
    ) -> None:
        self.limits = limits
        self._parent = parent
        self._root: BudgetLedger = parent._root if parent is not None else self
        self._clock: Callable[[], float] = parent._clock if parent is not None else clock
        self._deadline = self._clock() + limits.timeout_seconds
        if parent is not None:
            self._deadline = min(self._deadline, parent._deadline)
        self._lock: RLock = parent._lock if parent is not None else RLock()
        self._cancelled: Event = parent._cancelled if parent is not None else Event()
        self._local_slots = BoundedSemaphore(limits.max_parallel_calls)
        self._semaphore: BoundedSemaphore = parent._semaphore if parent is not None else self._local_slots
        self._calls_used = 0
        self._tokens_used = 0
        self._tokens_reserved = 0
        self._sequence = 0
        self._reservations: dict[int, Reservation] = {}
        self._settlements: dict[int, int] = {}
        self._history_consumed = False

    def child(self, limits: BudgetLimits) -> BudgetLedger:
        return BudgetLedger(limits, parent=self)

    def _lineage(self) -> Iterator[BudgetLedger]:
        ledger: BudgetLedger | None = self
        while ledger is not None:
            yield ledger
            ledger = ledger._parent

    @staticmethod
    def _count(value: int, name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise QuantSystemError(f"{name} must be a nonnegative integer")

    def _check_locked(self) -> None:
        if self._cancelled.is_set():
            raise QuantSystemError("research cancelled")
        if self._clock() >= self._deadline:
            raise QuantSystemError("research deadline exceeded")
        for ledger in self._lineage():
            if ledger._calls_used > ledger.limits.max_calls:
                raise QuantSystemError("research LLM call budget exceeded")
            if ledger._tokens_used + ledger._tokens_reserved > ledger.limits.max_tokens:
                raise QuantSystemError("research token budget exceeded")

    def check(self) -> None:
        with self._lock:
            self._check_locked()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled.set()

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def can_afford(self, calls: int, tokens: int = 0) -> bool:
        self._count(calls, "calls")
        self._count(tokens, "tokens")
        with self._lock:
            try:
                self._check_locked()
            except QuantSystemError:
                return False
            return all(
                ledger._calls_used + calls <= ledger.limits.max_calls
                and ledger._tokens_used + ledger._tokens_reserved + tokens <= ledger.limits.max_tokens
                for ledger in self._lineage()
            )

    def consume_history(self, receipts: list[CallReceipt]) -> None:
        with self._lock:
            if (
                self._parent is not None
                or self._history_consumed
                or self._calls_used
                or self._tokens_used
                or self._tokens_reserved
                or self._sequence
                or self._reservations
                or self._settlements
            ):
                raise QuantSystemError("history requires a pristine root budget ledger")
            self._check_locked()
            sequences = []
            identities: set[str] = set()
            tokens_used = 0
            for receipt in receipts:
                self._count(receipt.sequence, "history sequence")
                self._count(receipt.used_tokens, "history used_tokens")
                self._count(receipt.reservation_tokens, "history reservation_tokens")
                if not receipt.call_id or receipt.call_id in identities:
                    raise QuantSystemError("history call identities must be unique and nonempty")
                identities.add(receipt.call_id)
                sequences.append(receipt.sequence)
                tokens_used += (
                    max(receipt.used_tokens, receipt.reservation_tokens)
                    if receipt.status in {"STARTED", "UNKNOWN"}
                    else receipt.used_tokens
                )
            if sorted(sequences) != list(range(1, len(receipts) + 1)):
                raise QuantSystemError("history call sequences must be contiguous from one")
            if len(receipts) > self.limits.max_calls:
                raise QuantSystemError("research LLM call budget exceeded")
            if tokens_used > self.limits.max_tokens:
                raise QuantSystemError("research token budget exceeded")
            self._calls_used = self._sequence = len(receipts)
            self._tokens_used = tokens_used
            self._history_consumed = True

    def reserve(self, tokens: int) -> Reservation:
        self._count(tokens, "tokens")
        with self._lock:
            self._check_locked()
            lineage = tuple(self._lineage())
            for ledger in lineage:
                if ledger._calls_used + 1 > ledger.limits.max_calls:
                    raise QuantSystemError("research LLM call budget exceeded")
                if ledger._tokens_used + ledger._tokens_reserved + tokens > ledger.limits.max_tokens:
                    raise QuantSystemError("research token budget exceeded")
            self._root._sequence += 1
            reservation = Reservation(sequence=self._root._sequence, tokens=tokens, _ledger=self)
            self._root._reservations[reservation.sequence] = reservation
            for ledger in lineage:
                ledger._calls_used += 1
                ledger._tokens_reserved += tokens
            return reservation

    def settle(self, reservation: Reservation, used_tokens: int) -> None:
        """Commit actual usage once, even when it exceeds the conservative estimate."""
        self._count(used_tokens, "used_tokens")
        with self._lock:
            if self._root._reservations.get(reservation.sequence) is not reservation:
                raise QuantSystemError("reservation does not belong to this budget root")
            if reservation.sequence in self._root._settlements:
                if self._root._settlements[reservation.sequence] != used_tokens:
                    raise QuantSystemError("reservation was already settled with different usage")
                return
            self._root._settlements[reservation.sequence] = used_tokens
            exceeded = False
            for ledger in reservation._ledger._lineage():
                ledger._tokens_reserved -= reservation.tokens
                ledger._tokens_used += used_tokens
                exceeded |= ledger._tokens_used + ledger._tokens_reserved > ledger.limits.max_tokens
            if exceeded:
                raise QuantSystemError("research token budget exceeded")

    def usage(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls_used": self._calls_used,
                "tokens_used": self._tokens_used,
                "tokens_reserved": self._tokens_reserved,
                "max_calls": self.limits.max_calls,
                "max_tokens": self.limits.max_tokens,
            }

    @contextmanager
    def _slot(self) -> Generator[None, None, None]:
        acquired: list[BoundedSemaphore] = []
        try:
            for ledger in self._lineage():
                while True:
                    self.check()
                    if ledger._local_slots.acquire(timeout=min(0.05, self.remaining_seconds())):
                        acquired.append(ledger._local_slots)
                        break
            self.check()
            yield
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()


class CallReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    sequence: int
    role: str
    provider: str
    model: str
    purpose: str = ""
    status: Literal["STARTED", "COMPLETED", "FAILED", "INVALID", "UNKNOWN"]
    messages: list[LLMMessage]
    response: LLMResponse | None = None
    error: str | None = None
    parse_error: str | None = None
    reservation_tokens: int
    used_tokens: int = 0
    repair_of: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def _route_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("route"), dict):
            payload = dict(value)
            for name in ("provider", "model"):
                if name not in payload and name in value["route"]:
                    payload[name] = value["route"][name]
            return payload
        return value

    @computed_field
    def route(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}

    def trace(self) -> dict[str, Any]:
        response = self.response
        return {
            "role": self.role,
            "provider": response.provider if response is not None else self.provider,
            "model": response.model if response is not None else self.model,
            "input_tokens": response.input_tokens if response is not None else None,
            "output_tokens": response.output_tokens if response is not None else None,
            "raw_request_id": response.raw_request_id if response is not None else None,
        }


class _InvalidResponse(QuantSystemError):
    def __init__(self, receipt: CallReceipt) -> None:
        super().__init__(receipt.parse_error)
        self.receipt = receipt


class StructuredCaller:
    def __init__(
        self,
        config: AgentLLMConfig,
        gateway: CompletionGateway,
        ledger: BudgetLedger,
        *,
        sink: Callable[[CallReceipt], None] | None = None,
        allowed_roles: dict[str, AgentRoleConfig] | None = None,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._ledger = ledger
        self._sink = sink
        self._allowed_roles = dict(allowed_roles) if allowed_roles is not None else None
        self._receipts: dict[int, CallReceipt] = {}
        self._receipt_lock = RLock()

    @property
    def receipts(self) -> list[CallReceipt]:
        with self._receipt_lock:
            return [self._receipts[index].model_copy(deep=True) for index in sorted(self._receipts)]

    @staticmethod
    def parse(content: str, response_type: type[Result]) -> Result:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if (
                len(lines) < 3
                or lines[-1].strip() != "```"
                or lines[0].strip().lower() not in {"```", "```json"}
            ):
                raise QuantSystemError("response has an unsupported or incomplete JSON code fence")
            text = "\n".join(lines[1:-1])
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise QuantSystemError("response is not valid JSON") from exc
        try:
            return TypeAdapter(response_type).validate_python(payload)
        except ValidationError as exc:
            details = exc.errors(include_url=False, include_context=False, include_input=False)
            raise QuantSystemError(f"response does not match schema: {json.dumps(details)}") from None

    def call(
        self,
        role: str,
        messages: list[LLMMessage],
        response_type: type[Result],
        *,
        binding: AgentRoleConfig | None = None,
        validator: Callable[[Result], None] | None = None,
        purpose: str = "",
        repair_limit: int = 1,
    ) -> Result:
        def validate(response: LLMResponse) -> Result:
            parsed = self.parse(response.content, response_type)
            if validator is not None:
                validator(parsed)
            return parsed

        request_messages = list(messages)
        repair_of: str | None = None
        attempts = 1 + min(1, max(0, repair_limit))
        for attempt in range(attempts):
            try:
                return self._attempt(
                    role, request_messages, validate, binding=binding, purpose=purpose, repair_of=repair_of
                )
            except _InvalidResponse as exc:
                if attempt + 1 == attempts:
                    raise QuantSystemError(str(exc)) from None
                receipt = exc.receipt
                assert receipt.response is not None
                repair_of = receipt.call_id
                request_messages = [
                    *messages,
                    LLMMessage(role="assistant", content=receipt.response.content),
                    LLMMessage(
                        role="user",
                        content=(
                            "Your previous response failed local validation: "
                            f"{receipt.parse_error}. Preserve its factual judgment and return JSON only "
                            "matching this schema exactly: "
                            f"{json.dumps(TypeAdapter(response_type).json_schema())}"
                        ),
                    ),
                ]
        raise AssertionError("unreachable repair loop")

    def complete(
        self,
        role: str,
        messages: list[LLMMessage],
        *,
        binding: AgentRoleConfig | None = None,
        purpose: str = "",
    ) -> LLMResponse:
        return self._attempt(
            role, messages, lambda response: response, binding=binding, purpose=purpose, repair_of=None
        )

    def _error(self, error: BaseException) -> str:
        message = str(error) or type(error).__name__
        secrets = {
            os.environ.get(provider.api_key_env, "").strip() for provider in self._config.providers.values()
        }
        for secret in sorted(secrets - {""}, key=len, reverse=True):
            message = message.replace(secret, "[REDACTED]")
        return message[:4000]

    def _publish(self, receipt: CallReceipt, *, persist: bool = True) -> None:
        with self._receipt_lock:
            self._receipts[receipt.sequence] = receipt.model_copy(deep=True)
        if persist and self._sink is not None:
            self._sink(receipt.model_copy(deep=True))

    @staticmethod
    def _used_tokens(response: LLMResponse, reservation_tokens: int) -> int:
        known = [
            value
            for value in (response.input_tokens, response.output_tokens)
            if value is not None and value >= 0
        ]
        return sum(known) if len(known) == 2 else max(reservation_tokens, sum(known))

    def _attempt(
        self,
        role: str,
        messages: list[LLMMessage],
        validate: Callable[[LLMResponse], Result],
        *,
        binding: AgentRoleConfig | None,
        purpose: str,
        repair_of: str | None,
    ) -> Result:
        if not messages:
            raise QuantSystemError("LLM request requires at least one message")
        if self._allowed_roles is not None:
            configured = self._allowed_roles.get(role)
            if configured is None:
                raise QuantSystemError(f"role is not declared by this task: {role}")
            if binding is None:
                binding = configured
            elif (
                binding.provider != configured.provider
                or binding.model != configured.model
                or binding.max_output_tokens > configured.max_output_tokens
            ):
                raise QuantSystemError("call binding exceeds the registered role binding")
        if binding is None:
            _, binding = self._config.route(role)
        if binding.provider not in self._config.providers:
            raise QuantSystemError(f"LLM provider is not configured: {binding.provider}")
        request_messages = [message.model_copy(deep=True) for message in messages]
        serialized = json.dumps(
            [message.model_dump(mode="json") for message in request_messages], ensure_ascii=False
        )
        reservation_tokens = len(serialized.encode("utf-8")) + 256 + binding.max_output_tokens
        with self._ledger._slot():
            reservation = self._ledger.reserve(reservation_tokens)
            receipt = CallReceipt(
                call_id=new_id("call_"),
                sequence=reservation.sequence,
                role=role,
                provider=binding.provider,
                model=binding.model,
                purpose=purpose,
                status="STARTED",
                messages=[message.model_copy(deep=True) for message in request_messages],
                reservation_tokens=reservation_tokens,
                repair_of=repair_of,
            )
            failure: BaseException | None = None
            invalid = False
            started = False
            used_tokens = reservation_tokens
            result: Result | None = None
            try:
                try:
                    self._publish(receipt)
                except Exception as exc:
                    raise QuantSystemError(f"call audit sink failed: {self._error(exc)}") from None
                started = True
                self._ledger.check()
                if isinstance(self._gateway, LLMGateway):
                    response = self._gateway.complete_bound(
                        binding, request_messages, timeout_seconds=self._ledger.remaining_seconds()
                    )
                else:
                    response = self._gateway.complete(role, request_messages)
                used_tokens = self._used_tokens(response, reservation_tokens)
                receipt = receipt.model_copy(update={"response": response.model_copy(deep=True)})
                self._ledger.check()
                try:
                    result = validate(response)
                except Exception as exc:
                    invalid = True
                    receipt = receipt.model_copy(
                        update={"status": "INVALID", "parse_error": self._error(exc)}
                    )
                else:
                    receipt = receipt.model_copy(update={"status": "COMPLETED"})
                self._ledger.check()
            except BaseException as exc:
                failure = exc
                invalid = False
                receipt = receipt.model_copy(
                    update={
                        "status": "FAILED" if isinstance(exc, Exception) else "UNKNOWN",
                        "error": self._error(exc),
                    }
                )
            receipt = receipt.model_copy(update={"used_tokens": used_tokens})
            try:
                self._ledger.settle(reservation, used_tokens)
            except Exception as exc:
                failure = exc
                invalid = False
                receipt = receipt.model_copy(update={"status": "FAILED", "error": self._error(exc)})
            try:
                self._publish(receipt, persist=started)
            except BaseException as exc:
                failure = (
                    QuantSystemError(f"call audit sink failed: {self._error(exc)}")
                    if isinstance(exc, Exception)
                    else exc
                )
                invalid = False
                receipt = receipt.model_copy(update={"status": "UNKNOWN", "error": self._error(failure)})
                self._publish(receipt, persist=False)
            if failure is not None:
                if isinstance(failure, Exception):
                    raise QuantSystemError(self._error(failure)) from None
                raise failure
            self._ledger.check()
            if invalid:
                raise _InvalidResponse(receipt)
            return cast(Result, result)
