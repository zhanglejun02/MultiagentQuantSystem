from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest
from pydantic import BaseModel, ValidationError

from quant_system.agents.config import AgentLLMConfig, AgentRoleConfig
from quant_system.agents.llm import LLMGateway, LLMMessage, LLMResponse
from quant_system.domain.exceptions import QuantSystemError
from quant_system.research.runtime.calls import BudgetLedger, BudgetLimits, CallReceipt, StructuredCaller


class Answer(BaseModel):
    value: int


class FakeGateway:
    def __init__(self, *responses):
        self.responses = list(responses) or [
            LLMResponse(
                content='{"value": 7}',
                provider="fake",
                model="model",
                input_tokens=5,
                output_tokens=3,
            )
        ]
        self.calls = []

    def complete(self, role, messages):
        self.calls.append((role, list(messages)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _config():
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                "fake": {
                    "kind": "openai",
                    "base_url": "https://example.invalid",
                    "api_key_env": "TEST_CALL_KEY",
                }
            },
            "roles": {"researcher": {"provider": "fake", "model": "model", "max_output_tokens": 100}},
        }
    )


def _messages():
    return [LLMMessage(role="user", content="Return a value")]


def test_budget_limits_are_frozen_and_reject_extra_fields():
    limits = BudgetLimits()
    assert limits.model_dump() == {
        "max_calls": 30,
        "max_tokens": 200_000,
        "timeout_seconds": 600,
        "max_parallel_calls": 3,
    }
    with pytest.raises(ValidationError, match="frozen"):
        limits.max_calls = 1
    with pytest.raises(ValidationError, match="Extra inputs"):
        BudgetLimits.model_validate({"hidden_retry": 3})
    for field, value in (
        ("max_calls", -1),
        ("max_tokens", -1),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("max_parallel_calls", 0),
    ):
        with pytest.raises(ValidationError):
            BudgetLimits.model_validate({field: value})
    assert not BudgetLedger(BudgetLimits(max_calls=0)).can_afford(1)


def test_parent_child_budgets_charge_atomically_once_and_share_sequences():
    root = BudgetLedger(BudgetLimits(max_calls=3, max_tokens=100))
    child = root.child(BudgetLimits(max_calls=2, max_tokens=70))
    grandchild = child.child(BudgetLimits(max_calls=1, max_tokens=50))
    sibling = root.child(BudgetLimits(max_calls=2, max_tokens=60))
    first = grandchild.reserve(40)
    assert first.sequence == 1
    assert root.usage()["tokens_reserved"] == child.usage()["tokens_reserved"] == 40
    assert not grandchild.can_afford(1)
    with pytest.raises(QuantSystemError, match="call budget"):
        grandchild.reserve(1)
    with pytest.raises(QuantSystemError, match="token budget"):
        child.reserve(31)
    assert root.usage()["calls_used"] == 1
    second = sibling.reserve(60)
    assert second.sequence == 2
    assert not root.can_afford(1, 1)
    root.settle(first, 10)
    grandchild.settle(first, 10)
    assert root.usage()["tokens_used"] == child.usage()["tokens_used"] == 10
    assert root.usage()["tokens_reserved"] == 60
    third = child.reserve(20)
    assert third.sequence == 3
    assert not root.can_afford(1)
    sibling.settle(second, second.tokens)
    child.settle(third, 5)
    assert root.usage() == {
        "calls_used": 3,
        "tokens_used": 75,
        "tokens_reserved": 0,
        "max_calls": 3,
        "max_tokens": 100,
    }
    assert child.usage()["tokens_used"] == 15
    assert grandchild.usage()["calls_used"] == 1
    with pytest.raises(QuantSystemError, match="different usage"):
        root.settle(first, 11)
    with pytest.raises(QuantSystemError, match="belong"):
        BudgetLedger(BudgetLimits()).settle(first, 10)


def _history_receipt(sequence=1, **updates):
    return CallReceipt(
        call_id=f"planner:{sequence}",
        sequence=sequence,
        role="planner",
        provider="fake",
        model="model",
        status="COMPLETED",
        messages=_messages(),
        reservation_tokens=100,
        used_tokens=8,
    ).model_copy(update=updates, deep=True)


def test_history_charges_root_once_without_fabricating_reservations_or_child_usage():
    root = BudgetLedger(BudgetLimits(max_calls=3, max_tokens=120))
    root.consume_history([_history_receipt(), _history_receipt(2, status="INVALID", used_tokens=12)])
    assert root.usage()["calls_used"] == 2
    assert root.usage()["tokens_used"] == 20
    assert root._reservations == root._settlements == {}
    child = root.child(BudgetLimits(max_calls=2, max_tokens=100))
    assert child.usage()["calls_used"] == child.usage()["tokens_used"] == 0
    reservation = child.reserve(100)
    assert reservation.sequence == 3
    child.settle(reservation, 10)
    assert root.usage()["calls_used"] == 3
    assert root.usage()["tokens_used"] == 30
    assert child.usage()["calls_used"] == 1
    assert child.usage()["tokens_used"] == 10
    with pytest.raises(QuantSystemError, match="pristine root"):
        root.consume_history([_history_receipt()])
    with pytest.raises(QuantSystemError, match="pristine root"):
        child.consume_history([])
    with pytest.raises(QuantSystemError, match="call budget"):
        child.reserve(1)


@pytest.mark.parametrize("status", ["STARTED", "UNKNOWN"])
def test_unsettled_history_is_charged_conservatively_and_blocks_send(status):
    root = BudgetLedger(BudgetLimits(max_tokens=100))
    root.consume_history([_history_receipt(status=status, used_tokens=0)])
    gateway = FakeGateway()
    caller = StructuredCaller(_config(), gateway, root)
    with pytest.raises(QuantSystemError, match="token budget"):
        caller.call("researcher", _messages(), Answer)
    assert root.usage()["tokens_used"] == 100
    assert root.usage()["tokens_reserved"] == 0
    assert caller.receipts == gateway.calls == []


@pytest.mark.parametrize("problem", ["calls", "tokens", "sequence", "identity", "negative", "boolean"])
def test_invalid_history_is_atomic_and_does_not_claim_sequence(problem):
    root = BudgetLedger(BudgetLimits(max_calls=1, max_tokens=100))
    history = [_history_receipt()]
    if problem == "calls":
        history.append(_history_receipt(2))
    elif problem == "tokens":
        history[0] = _history_receipt(used_tokens=101)
    elif problem == "sequence":
        history[0] = _history_receipt(2)
    elif problem == "identity":
        history[0] = _history_receipt(call_id="")
    elif problem == "negative":
        history[0] = _history_receipt(used_tokens=-1)
    else:
        history[0] = _history_receipt().model_copy(update={"sequence": True})
    before = root.usage()
    with pytest.raises(QuantSystemError):
        root.consume_history(history)
    assert root.usage() == before
    assert root.reserve(1).sequence == 1


def test_empty_history_still_cannot_be_consumed_twice_or_after_usage():
    root = BudgetLedger(BudgetLimits())
    root.consume_history([])
    with pytest.raises(QuantSystemError, match="pristine root"):
        root.consume_history([_history_receipt()])
    used = BudgetLedger(BudgetLimits())
    used.settle(used.reserve(1), 0)
    with pytest.raises(QuantSystemError, match="pristine root"):
        used.consume_history([])


def test_concurrent_history_consumption_can_charge_only_once():
    root = BudgetLedger(BudgetLimits())
    barrier = Barrier(2, timeout=5)

    def consume():
        barrier.wait()
        try:
            root.consume_history([_history_receipt()])
            return True
        except QuantSystemError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _index: consume(), range(2))) == [False, True]
    assert root.usage()["calls_used"] == 1
    assert root.usage()["tokens_used"] == 8
    assert root.reserve(1).sequence == 2


def test_concurrent_reservations_cannot_overdraw_parent_or_local_budgets():
    root = BudgetLedger(BudgetLimits(max_calls=3, max_tokens=70))
    children = [root.child(BudgetLimits(max_calls=2, max_tokens=40)) for _ in range(2)]
    barrier = Barrier(12, timeout=5)

    def reserve(index):
        barrier.wait()
        try:
            return children[index % 2].reserve(20)
        except QuantSystemError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        reservations = [item for item in pool.map(reserve, range(12)) if item is not None]
    assert sorted(item.sequence for item in reservations) == [1, 2, 3]
    assert root.usage()["tokens_reserved"] == 60
    assert sum(child.usage()["calls_used"] for child in children) == 3
    assert all(child.usage()["tokens_reserved"] <= 40 for child in children)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda item: root.settle(item, 10), reservations))
    assert root.usage()["tokens_used"] == 30
    assert root.usage()["tokens_reserved"] == 0


def test_actual_overage_is_recorded_before_failing_closed():
    root = BudgetLedger(BudgetLimits(max_tokens=20))
    child = root.child(BudgetLimits(max_tokens=15))
    reservation = child.reserve(10)
    with pytest.raises(QuantSystemError, match="token budget"):
        child.settle(reservation, 21)
    assert root.usage()["tokens_used"] == child.usage()["tokens_used"] == 21
    assert root.usage()["tokens_reserved"] == child.usage()["tokens_reserved"] == 0
    assert not root.can_afford(0)
    with pytest.raises(QuantSystemError, match="token budget"):
        child.check()


def test_children_inherit_deadlines_and_root_cancellation():
    now = [100.0]
    root = BudgetLedger(BudgetLimits(timeout_seconds=10), clock=lambda: now[0])
    child = root.child(BudgetLimits(timeout_seconds=20))
    short = child.child(BudgetLimits(timeout_seconds=2))
    assert child.remaining_seconds() == 10
    now[0] = 102
    with pytest.raises(QuantSystemError, match="deadline"):
        short.reserve(1)
    assert not short.can_afford(1)
    assert short.remaining_seconds() == 0
    assert root.remaining_seconds() == 8
    child.cancel()
    assert not root.can_afford(1)
    for ledger in (root, child, short):
        with pytest.raises(QuantSystemError, match="cancelled"):
            ledger.check()
    assert root.usage()["calls_used"] == 0


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_reservations_do_not_change_usage(value):
    ledger = BudgetLedger(BudgetLimits())
    with pytest.raises(QuantSystemError, match="nonnegative integer"):
        ledger.reserve(value)
    assert ledger.usage()["calls_used"] == 0


def test_started_receipt_is_persisted_before_send_and_snapshots_are_safe():
    persisted = []
    snapshots = []
    ledger = BudgetLedger(BudgetLimits())
    gateway = FakeGateway()

    def sink(receipt):
        persisted.append(receipt.model_dump(mode="json"))
        if receipt.status == "STARTED":
            assert not gateway.calls
            snapshots.extend(caller.receipts)
        receipt.messages.clear()

    caller = StructuredCaller(_config(), gateway, ledger, sink=sink)
    messages = _messages()
    result = caller.call("researcher", messages, Answer, purpose="unit")
    assert result.value == 7
    assert [item["status"] for item in persisted] == ["STARTED", "COMPLETED"]
    receipt = caller.receipts[0]
    assert receipt.role == "researcher"
    assert receipt.purpose == "unit"
    assert receipt.used_tokens == ledger.usage()["tokens_used"] == 8
    assert ledger.usage()["tokens_reserved"] == 0
    assert receipt.route == {"provider": "fake", "model": "model"}
    assert receipt.trace() == {
        "role": "researcher",
        "provider": "fake",
        "model": "model",
        "input_tokens": 5,
        "output_tokens": 3,
        "raw_request_id": None,
    }
    assert snapshots[0].status == "STARTED"
    assert snapshots[0].response is None
    receipt.messages.clear()
    messages.clear()
    assert len(caller.receipts[0].messages) == 1
    json.dumps(caller.receipts[0].model_dump(mode="json"))


@pytest.mark.parametrize("tokens", [(None, None), (5, None), (-1, 3)])
def test_missing_or_negative_usage_is_conservatively_settled(tokens):
    response = LLMResponse(
        content="plain text", provider="fake", model="model", input_tokens=tokens[0], output_tokens=tokens[1]
    )
    ledger = BudgetLedger(BudgetLimits())
    caller = StructuredCaller(_config(), FakeGateway(response), ledger)
    assert caller.complete("researcher", _messages()) == response
    receipt = caller.receipts[0]
    assert receipt.used_tokens == receipt.reservation_tokens
    assert ledger.usage()["tokens_used"] == receipt.reservation_tokens
    assert ledger.usage()["tokens_reserved"] == 0


def test_provider_failure_is_audited_and_never_retried(monkeypatch):
    monkeypatch.setenv("TEST_CALL_KEY", "secret-key-sentinel")
    gateway = FakeGateway(QuantSystemError("provider unavailable secret-key-sentinel"))
    ledger = BudgetLedger(BudgetLimits())
    events = []
    caller = StructuredCaller(_config(), gateway, ledger, sink=events.append)
    with pytest.raises(QuantSystemError, match="provider unavailable") as caught:
        caller.call("researcher", _messages(), Answer)
    assert len(gateway.calls) == 1
    assert [item.status for item in events] == ["STARTED", "FAILED"]
    receipt = caller.receipts[0]
    assert receipt.used_tokens == receipt.reservation_tokens
    assert "secret-key-sentinel" not in json.dumps(receipt.model_dump(mode="json"))
    assert "secret-key-sentinel" not in str(caught.value)
    assert receipt.trace()["raw_request_id"] is None
    assert ledger.usage()["tokens_reserved"] == 0


def test_started_sink_failure_blocks_gateway_and_settles_reservation():
    gateway = FakeGateway()
    ledger = BudgetLedger(BudgetLimits())

    def sink(receipt):
        raise OSError("audit disk unavailable")

    caller = StructuredCaller(_config(), gateway, ledger, sink=sink)
    with pytest.raises(QuantSystemError, match="audit sink failed"):
        caller.call("researcher", _messages(), Answer)
    assert not gateway.calls
    receipt = caller.receipts[0]
    assert receipt.status == "FAILED"
    assert ledger.usage()["tokens_used"] == receipt.reservation_tokens
    assert ledger.usage()["tokens_reserved"] == 0


@pytest.mark.parametrize("repair_limit", [1, 9])
def test_schema_repair_has_separate_receipt_and_preserves_first_error(repair_limit):
    gateway = FakeGateway(
        LLMResponse(
            content='{"wrong": true}', provider="fake", model="model", input_tokens=2, output_tokens=1
        ),
        LLMResponse(
            content='```json\n{"value": 8}\n```',
            provider="fake",
            model="model",
            input_tokens=3,
            output_tokens=2,
        ),
    )
    ledger = BudgetLedger(BudgetLimits())
    events = []
    caller = StructuredCaller(_config(), gateway, ledger, sink=events.append)
    assert caller.call("researcher", _messages(), Answer, repair_limit=repair_limit).value == 8
    original, repaired = caller.receipts
    assert original.status == "INVALID"
    assert original.parse_error and "schema" in original.parse_error
    assert original.response.content == '{"wrong": true}'
    assert repaired.status == "COMPLETED"
    assert repaired.repair_of == original.call_id
    assert repaired.sequence == original.sequence + 1
    assert repaired.messages[-2].role == "assistant"
    assert original.parse_error in repaired.messages[-1].content
    assert [event.status for event in events] == ["STARTED", "INVALID", "STARTED", "COMPLETED"]
    assert ledger.usage()["calls_used"] == 2
    assert ledger.usage()["tokens_used"] == 8


def test_validator_is_generic_and_can_request_one_repair():
    gateway = FakeGateway(
        LLMResponse(content='{"value": 0}', provider="fake", model="model"),
        LLMResponse(content='{"value": 2}', provider="fake", model="model"),
    )

    def positive(answer):
        if answer.value <= 0:
            raise QuantSystemError("value must be positive")

    caller = StructuredCaller(_config(), gateway, BudgetLedger(BudgetLimits()))
    result = caller.call("researcher", _messages(), Answer, validator=positive)
    assert result.value == 2
    assert caller.receipts[0].parse_error == "value must be positive"


def test_binding_preserves_legacy_logical_role_for_fake_gateways():
    gateway = FakeGateway()
    config = _config()
    before = config.model_dump()
    binding = AgentRoleConfig(provider="fake", model="new-model", max_output_tokens=20)
    caller = StructuredCaller(config, gateway, BudgetLedger(BudgetLimits()))
    assert caller.call("macro.rates-review", _messages(), Answer, binding=binding).value == 7
    assert gateway.calls[0][0] == "macro.rates-review"
    assert caller.receipts[0].model == "new-model"
    assert config.model_dump() == before


@pytest.mark.parametrize("method", ["call", "complete"])
@pytest.mark.parametrize("problem", ["role", "unknown", "empty", "provider", "model", "tokens"])
def test_managed_caller_rejects_role_and_binding_bypass_before_reserving(method, problem):
    config = _config()
    config = config.model_copy(update={"providers": {**config.providers, "other": config.providers["fake"]}})
    binding = config.roles["researcher"]
    allowed_roles = {"macro.rates-review": binding}
    role = "macro.rates-review"
    if problem == "role":
        role, binding = "researcher", None
    elif problem == "unknown":
        role = "macro.undeclared"
    elif problem == "empty":
        allowed_roles = {}
    else:
        binding = binding.model_copy(
            update={
                "provider": {"provider": "other"},
                "model": {"model": "unapproved-model"},
                "tokens": {"max_output_tokens": binding.max_output_tokens + 1},
            }[problem]
        )
    gateway = FakeGateway()
    root = BudgetLedger(BudgetLimits())
    ledger = root.child(BudgetLimits())
    persisted = []
    caller = StructuredCaller(config, gateway, ledger, sink=persisted.append, allowed_roles=allowed_roles)
    before = ledger.usage()
    error = "role is not declared" if problem in {"role", "unknown", "empty"} else "binding exceeds"
    with pytest.raises(QuantSystemError, match=error):
        if method == "call":
            caller.call(role, _messages(), Answer, binding=binding)
        else:
            caller.complete(role, _messages(), binding=binding)
    assert gateway.calls == caller.receipts == persisted == []
    assert ledger.usage() == root.usage() == before


@pytest.mark.parametrize("method", ["call", "complete"])
@pytest.mark.parametrize("explicit", [False, True])
def test_managed_caller_resolves_logical_binding_without_ambient_roles(monkeypatch, method, explicit):
    monkeypatch.setenv("TEST_CALL_KEY", "test-only")
    config = _config()
    before = config.model_dump()
    binding = AgentRoleConfig(provider="fake", model="managed-model", max_output_tokens=20)
    captured = []

    def transport(url, headers, body, timeout):
        captured.append(body["model"])
        return {"choices": [{"message": {"content": '{"value": 6}'}}]}

    caller = StructuredCaller(
        config,
        LLMGateway(config, transport),
        BudgetLedger(BudgetLimits()),
        allowed_roles={"macro.rates-review": binding},
    )
    requested = binding.model_copy(update={"max_output_tokens": 10}) if explicit else None
    if method == "call":
        assert caller.call("macro.rates-review", _messages(), Answer, binding=requested).value == 6
    else:
        assert caller.complete("macro.rates-review", _messages(), binding=requested).content == '{"value": 6}'
    assert captured == ["managed-model"]
    assert caller.receipts[0].role == "macro.rates-review"
    assert caller.receipts[0].model == "managed-model"
    assert config.model_dump() == before


def test_real_gateway_uses_explicit_binding_and_remaining_deadline(monkeypatch):
    monkeypatch.setenv("TEST_CALL_KEY", "test-only")
    now = [1.0]
    ledger = BudgetLedger(BudgetLimits(timeout_seconds=10), clock=lambda: now[0])
    binding = AgentRoleConfig(provider="fake", model="bound-model", max_output_tokens=20)
    captured = []

    def sink(receipt):
        if receipt.status == "STARTED":
            now[0] += 3

    def transport(url, headers, body, timeout):
        captured.append((body["model"], timeout))
        return {"choices": [{"message": {"content": '{"value": 6}'}}]}

    config = _config()
    caller = StructuredCaller(config, LLMGateway(config, transport), ledger, sink=sink)
    assert caller.call("macro.new-role", _messages(), Answer, binding=binding).value == 6
    assert captured == [("bound-model", 7)]
    assert caller.receipts[0].role == "macro.new-role"


def test_parent_concurrency_is_shared_across_children_with_global_sequences():
    root = BudgetLedger(BudgetLimits(max_parallel_calls=2))
    config = _config()
    binding = config.roles["researcher"]
    release = Event()
    both_started = Event()
    lock = Lock()
    active = 0
    peak = 0
    role_active = {}

    class ConcurrentGateway:
        def complete(self, role, messages):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                role_active[role] = role_active.get(role, 0) + 1
                assert role_active[role] == 1
                if active == 2:
                    both_started.set()
            try:
                assert release.wait(5)
                return LLMResponse(
                    content='{"value": 1}', provider="fake", model="model", input_tokens=1, output_tokens=1
                )
            finally:
                with lock:
                    role_active[role] -= 1
                    active -= 1

    gateway = ConcurrentGateway()
    callers = [
        StructuredCaller(config, gateway, root.child(BudgetLimits(max_parallel_calls=1))) for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                callers[index % 2].call, f"macro.child{index % 2}", _messages(), Answer, binding=binding
            )
            for index in range(8)
        ]
        try:
            assert both_started.wait(5)
            assert root.usage()["calls_used"] == 2
            assert sum(len(caller.receipts) for caller in callers) == 2
        finally:
            release.set()
        assert all(future.result(timeout=5).value == 1 for future in futures)
    receipts = [receipt for caller in callers for receipt in caller.receipts]
    assert sorted(receipt.sequence for receipt in receipts) == list(range(1, 9))
    assert all(receipt.status == "COMPLETED" for receipt in receipts)
    assert peak == 2
    assert root.usage()["calls_used"] == 8
    assert root.usage()["tokens_used"] == 16
    assert root.usage()["tokens_reserved"] == 0


def test_receipts_keep_reservation_order_when_responses_finish_out_of_order():
    first_entered = Event()
    release_first = Event()
    events = []

    class OrderedGateway:
        def complete(self, role, messages):
            if messages[0].content == "first":
                first_entered.set()
                assert release_first.wait(5)
            return LLMResponse(content="plain", provider="fake", model="model")

    caller = StructuredCaller(
        _config(), OrderedGateway(), BudgetLedger(BudgetLimits(max_parallel_calls=2)), sink=events.append
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(caller.complete, "researcher", [LLMMessage(role="user", content="first")])
        try:
            assert first_entered.wait(5)
            snapshot = caller.receipts
            second = pool.submit(caller.complete, "researcher", [LLMMessage(role="user", content="second")])
            assert second.result(timeout=5).content == "plain"
        finally:
            release_first.set()
        first.result(timeout=5)
    assert [receipt.messages[0].content for receipt in caller.receipts] == ["first", "second"]
    assert [(receipt.sequence, receipt.status) for receipt in events] == [
        (1, "STARTED"),
        (2, "STARTED"),
        (2, "COMPLETED"),
        (1, "COMPLETED"),
    ]
    assert snapshot[0].status == "STARTED"


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_interruption_blocks_queued_calls_without_releasing_running_slot(interrupt):
    now = [0.0]
    root = BudgetLedger(BudgetLimits(timeout_seconds=1, max_parallel_calls=1), clock=lambda: now[0])
    child = root.child(BudgetLimits(max_parallel_calls=2))
    entered = Event()
    queued = Event()
    release = Event()
    sends = []

    class BlockingGateway:
        def complete(self, role, messages):
            sends.append(role)
            entered.set()
            assert release.wait(5)
            return LLMResponse(
                content='{"value": 7}', provider="fake", model="model", input_tokens=5, output_tokens=3
            )

    caller = StructuredCaller(_config(), BlockingGateway(), child)

    def waiting_call():
        queued.set()
        return caller.call("researcher", _messages(), Answer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(caller.call, "researcher", _messages(), Answer)
        try:
            assert entered.wait(5)
            waiting = pool.submit(waiting_call)
            assert queued.wait(5)
            if interrupt == "cancel":
                child.cancel()
                error = "cancelled"
            else:
                now[0] = 1.0
                error = "deadline"
            with pytest.raises(QuantSystemError, match=error):
                waiting.result(timeout=5)
            assert not running.done()
            assert not root._semaphore.acquire(blocking=False)
            assert sends == ["researcher"]
            assert root.usage()["calls_used"] == 1
        finally:
            release.set()
        with pytest.raises(QuantSystemError, match=error):
            running.result(timeout=5)
    receipt = caller.receipts[0]
    assert receipt.status == "FAILED"
    assert receipt.response is not None
    assert receipt.used_tokens == root.usage()["tokens_used"] == 8
    assert root.usage()["tokens_reserved"] == 0
    assert root._semaphore.acquire(blocking=False)
    root._semaphore.release()
    with pytest.raises(QuantSystemError, match=error):
        caller.complete("researcher", _messages())
    assert len(sends) == 1


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_interruption_during_started_sink_prevents_gateway_send(interrupt):
    now = [0.0]
    ledger = BudgetLedger(BudgetLimits(timeout_seconds=1), clock=lambda: now[0])
    gateway = FakeGateway()

    def sink(receipt):
        if receipt.status == "STARTED":
            if interrupt == "cancel":
                ledger.cancel()
            else:
                now[0] = 1.0

    caller = StructuredCaller(_config(), gateway, ledger, sink=sink)
    with pytest.raises(QuantSystemError, match="cancelled|deadline"):
        caller.call("researcher", _messages(), Answer)
    assert not gateway.calls
    receipt = caller.receipts[0]
    assert receipt.status == "FAILED"
    assert receipt.used_tokens == ledger.usage()["tokens_used"] == receipt.reservation_tokens
    assert ledger.usage()["tokens_reserved"] == 0


def test_terminal_sink_failure_is_unknown_and_does_not_trigger_repair():
    persisted = []
    gateway = FakeGateway()
    ledger = BudgetLedger(BudgetLimits())

    def sink(receipt):
        if receipt.status != "STARTED":
            raise OSError("cannot persist result")
        persisted.append(receipt)

    caller = StructuredCaller(_config(), gateway, ledger, sink=sink)
    with pytest.raises(QuantSystemError, match="audit sink failed"):
        caller.call("researcher", _messages(), Answer)
    assert [receipt.status for receipt in persisted] == ["STARTED"]
    assert len(gateway.calls) == 1
    assert caller.receipts[0].status == "UNKNOWN"
    assert caller.receipts[0].response is not None
    assert ledger.usage()["tokens_used"] == 8
    assert ledger.usage()["tokens_reserved"] == 0


@pytest.mark.parametrize("repair_limit, expected_calls", [(0, 1), (1, 2), (10, 2)])
def test_invalid_repair_never_causes_a_third_send(repair_limit, expected_calls):
    invalid = LLMResponse(content="not JSON", provider="fake", model="model")
    gateway = FakeGateway(invalid, invalid, invalid)
    ledger = BudgetLedger(BudgetLimits())
    caller = StructuredCaller(_config(), gateway, ledger)
    with pytest.raises(QuantSystemError, match="not valid JSON"):
        caller.call("researcher", _messages(), Answer, repair_limit=repair_limit)
    assert len(gateway.calls) == ledger.usage()["calls_used"] == expected_calls
    assert all(receipt.status == "INVALID" and receipt.parse_error for receipt in caller.receipts)
    assert ledger.usage()["tokens_reserved"] == 0
    if expected_calls == 2:
        assert caller.receipts[1].repair_of == caller.receipts[0].call_id


def test_repair_obeys_root_call_budget_and_preserves_original_error():
    ledger = BudgetLedger(BudgetLimits(max_calls=1))
    gateway = FakeGateway(LLMResponse(content="not JSON", provider="fake", model="model"))
    caller = StructuredCaller(_config(), gateway, ledger)
    with pytest.raises(QuantSystemError, match="call budget"):
        caller.call("researcher", _messages(), Answer)
    assert len(gateway.calls) == 1
    assert caller.receipts[0].status == "INVALID"
    assert caller.receipts[0].parse_error == "response is not valid JSON"


def test_provider_failure_during_repair_has_own_failed_receipt_without_retry():
    gateway = FakeGateway(
        LLMResponse(content="not JSON", provider="fake", model="model"),
        OSError("provider unavailable"),
    )
    ledger = BudgetLedger(BudgetLimits())
    caller = StructuredCaller(_config(), gateway, ledger)
    with pytest.raises(QuantSystemError, match="provider unavailable"):
        caller.call("researcher", _messages(), Answer)
    original, repair = caller.receipts
    assert original.status == "INVALID"
    assert original.parse_error
    assert repair.status == "FAILED"
    assert repair.repair_of == original.call_id
    assert repair.used_tokens == repair.reservation_tokens
    assert len(gateway.calls) == 2
    assert ledger.usage()["tokens_reserved"] == 0


def test_unknown_usage_exhausts_reserved_budget_before_another_send():
    gateway = FakeGateway(LLMResponse(content="plain", provider="fake", model="model"))
    ledger = BudgetLedger(BudgetLimits(max_tokens=500))
    caller = StructuredCaller(_config(), gateway, ledger)
    caller.complete("researcher", _messages())
    with pytest.raises(QuantSystemError, match="token budget"):
        caller.complete("researcher", _messages())
    assert len(gateway.calls) == 1
    assert ledger.usage()["tokens_used"] == caller.receipts[0].reservation_tokens
    assert not ledger.can_afford(1, caller.receipts[0].reservation_tokens)


@pytest.mark.parametrize("content", ['{"value": 4}', '```json\n{"value": 4}\n```', '```\n{"value": 4}\n```'])
def test_static_parse_accepts_json_or_supported_fences(content):
    assert StructuredCaller.parse(content, Answer).value == 4
    assert StructuredCaller.parse("3", int) == 3


@pytest.mark.parametrize(
    "content",
    ['```python\n{"value": 4}\n```', '```json\n{"value": 4}', 'explanation {"value": 4}', '{"wrong": 4}'],
)
def test_static_parse_rejects_invalid_json_fences_and_schema(content):
    with pytest.raises(QuantSystemError):
        StructuredCaller.parse(content, Answer)


def test_call_receipt_accepts_route_mapping_and_roundtrips_json():
    receipt = CallReceipt.model_validate(
        {
            "call_id": "call-test",
            "sequence": 1,
            "role": "macro.rates",
            "purpose": "audit",
            "route": {"provider": "fake", "model": "bound-model"},
            "status": "UNKNOWN",
            "messages": [message.model_dump() for message in _messages()],
            "reservation_tokens": 100,
        }
    )
    assert receipt.provider == "fake"
    assert receipt.model == "bound-model"
    assert receipt.route == {"provider": "fake", "model": "bound-model"}
    assert receipt.trace() == {
        "role": "macro.rates",
        "provider": "fake",
        "model": "bound-model",
        "input_tokens": None,
        "output_tokens": None,
        "raw_request_id": None,
    }
    assert CallReceipt.model_validate_json(receipt.model_dump_json()) == receipt


@pytest.mark.parametrize("usage", [(1000, 1), (1000, None)])
def test_actual_usage_overrun_is_audited_and_blocks_later_send(usage):
    response = LLMResponse(
        content='{"value": 7}', provider="fake", model="model", input_tokens=usage[0], output_tokens=usage[1]
    )
    gateway = FakeGateway(response)
    ledger = BudgetLedger(BudgetLimits(max_tokens=500))
    caller = StructuredCaller(_config(), gateway, ledger)
    with pytest.raises(QuantSystemError, match="token budget"):
        caller.call("researcher", _messages(), Answer)
    receipt = caller.receipts[0]
    assert receipt.status == "FAILED"
    assert receipt.response == response
    assert receipt.used_tokens == ledger.usage()["tokens_used"] == sum(value or 0 for value in usage)
    assert ledger.usage()["tokens_reserved"] == 0
    with pytest.raises(QuantSystemError, match="token budget"):
        caller.complete("researcher", _messages())
    assert len(gateway.calls) == 1


def test_process_interruption_keeps_unknown_receipt_and_settles_usage():
    gateway = FakeGateway(KeyboardInterrupt())
    ledger = BudgetLedger(BudgetLimits())
    caller = StructuredCaller(_config(), gateway, ledger)
    with pytest.raises(KeyboardInterrupt):
        caller.call("researcher", _messages(), Answer)
    receipt = caller.receipts[0]
    assert receipt.status == "UNKNOWN"
    assert receipt.error == "KeyboardInterrupt"
    assert receipt.used_tokens == receipt.reservation_tokens
    assert ledger.usage()["tokens_reserved"] == 0
    assert ledger.usage()["tokens_used"] == receipt.reservation_tokens
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("request_kind", ["empty", "unknown-role", "unknown-provider"])
def test_invalid_request_is_rejected_before_reservation_or_send(request_kind):
    gateway = FakeGateway()
    ledger = BudgetLedger(BudgetLimits())
    caller = StructuredCaller(_config(), gateway, ledger)
    role = "missing" if request_kind == "unknown-role" else "researcher"
    binding = (
        AgentRoleConfig(provider="missing", model="model") if request_kind == "unknown-provider" else None
    )
    messages = [] if request_kind == "empty" else _messages()
    with pytest.raises(QuantSystemError):
        caller.call(role, messages, Answer, binding=binding)
    assert not gateway.calls
    assert not caller.receipts
    assert ledger.usage()["calls_used"] == 0
