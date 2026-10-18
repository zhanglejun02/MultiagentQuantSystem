from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from threading import Barrier

import pytest

from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMMessage, LLMResponse
from quant_system.artifacts.envelope import ArtifactEnvelope, hash_json
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, QuantSystemError
from quant_system.research.runtime.calls import BudgetLedger, BudgetLimits, CallReceipt, StructuredCaller
from quant_system.research.runtime.models import (
    EvidenceRef,
    PlanTask,
    ResearchPlan,
    ResearchRun,
    RunRequest,
    TaskResult,
)
from quant_system.research.runtime.storage import ArtifactRunRepository

NOW = datetime(2026, 10, 17, 10, tzinfo=UTC)


def _run(run_id="root", *, key="request-key", parent=None, root=None, **updates):
    return ResearchRun(
        run_id=run_id,
        root_run_id=root or run_id,
        parent_run_id=parent,
        task_type="sample.inspect",
        module_id="sample",
        request=RunRequest(task_type="sample.inspect", payload={"nested": {"value": 1}}, idempotency_key=key),
        created_at=NOW,
        updated_at=NOW,
        **updates,
    )


def _repository(store, registry):
    return ArtifactRunRepository(store, registry, clock=lambda: NOW)


def _receipt(call_id="call", sequence=1, **updates):
    return CallReceipt(
        call_id=call_id,
        sequence=sequence,
        role="researcher",
        provider="fake",
        model="model",
        status="STARTED",
        messages=[LLMMessage(role="user", content="Raw request")],
        reservation_tokens=100,
        created_at=NOW,
    ).model_copy(update=updates, deep=True)


def test_run_revisions_reopen_and_deep_snapshots(store, registry):
    repository = _repository(store, registry)
    original = _run()
    saved = repository.save(original)
    assert saved.revision == 1
    assert original.revision == 0
    original.request.payload["nested"]["value"] = 99
    saved.children.append("not-persisted")
    assert repository.get("root").request.payload["nested"]["value"] == 1
    assert repository.get("root").children == []
    latest = repository.save(repository.get("root").model_copy(update={"status": "RUNNING"}))
    assert latest.revision == 2
    artifacts = sorted(
        registry.find_by_type(ArtifactType.RESEARCH_RUN), key=lambda item: item.metadata["revision"]
    )
    assert artifacts[0].artifact_id in artifacts[1].parent_artifact_ids
    assert "/research/runtime/runs/" in artifacts[0].uri
    assert store.get_json(artifacts[0].uri)["status"] == "QUEUED"
    assert store.get_json(artifacts[1].uri)["status"] == "RUNNING"
    reopened = _repository(store, registry)
    assert reopened.get("root") == latest
    snapshot = reopened.get("root")
    snapshot.request.payload.clear()
    assert reopened.get("root").request.payload
    with pytest.raises(ArtifactNotFoundError):
        reopened.get("missing")


def test_idempotency_hash_is_committed_and_conflicts_are_explicit(store, registry):
    repository = _repository(store, registry)
    saved = repository.save(_run())
    digest = hash_json(saved.request.model_dump(mode="json", exclude={"idempotency_key"}))
    artifact = registry.find_by_type(ArtifactType.RESEARCH_RUN)[0]
    assert artifact.metadata["request_hash"] == digest
    assert store.get_json(artifact.uri)["request_hash"] == digest
    for current in (repository, _repository(store, registry)):
        assert current.find_idempotent("request-key", digest) == saved
        assert current.find_idempotent("missing", digest) is None
        with pytest.raises(QuantSystemError, match="conflict"):
            current.find_idempotent("request-key", "different")


def test_events_are_per_run_ordered_append_only_snapshots(store, registry):
    repository = _repository(store, registry)
    repository.save(_run())
    repository.save(_run("child", parent="root", root="root"))
    payload = {"nested": {"value": 1}}
    first = repository.event("root", "started", "collect", payload)
    second = repository.event("root", "progress", "analyze")
    child = repository.event("child", "started", "collect")
    payload["nested"]["value"] = 99
    first.data.clear()
    assert (first.sequence, second.sequence, child.sequence) == (1, 2, 1)
    assert second.at == NOW
    for current in (repository, _repository(store, registry)):
        assert [event.sequence for event in current.events("root")] == [1, 2]
        assert current.events("root")[0].data == {"nested": {"value": 1}}
        assert current.events("root", offset=1, limit=1) == [second]
        assert current.events("child") == [child]
        snapshot = current.events("root")
        snapshot[0].data.clear()
        assert current.events("root")[0].data
    artifacts = registry.find_by_type(ArtifactType.RESEARCH_EVENT)
    assert len(artifacts) == 3
    root_artifact = registry.find_by_type(ArtifactType.RESEARCH_RUN)[0]
    root_events = [item for item in artifacts if item.metadata["run_id"] == "root"]
    assert all(root_artifact.artifact_id in item.parent_artifact_ids for item in root_events)
    assert _repository(store, registry).event("root", "done", "done").sequence == 3
    with pytest.raises(ArtifactNotFoundError):
        repository.event("missing", "started", "collect")


def test_call_revisions_preserve_intent_and_root_global_order(store, registry):
    repository = _repository(store, registry)
    repository.save(_run())
    repository.save(_run("child", parent="root", root="root"))
    repository.save(_run("sibling", parent="root", root="root"))
    intent = _receipt("child-call", 2)
    repository.record_call("child", intent)
    repository.record_call("root", _receipt("root-call", 1))
    repository.record_call("sibling", _receipt("sibling-call", 3))
    completed = intent.model_copy(
        update={
            "status": "COMPLETED",
            "response": LLMResponse(content="Raw response", provider="fake", model="model"),
            "used_tokens": 8,
        }
    )
    repository.record_call("child", completed)
    intent.messages.clear()
    completed.messages.clear()
    for current in (repository, _repository(store, registry)):
        assert [item.call_id for item in current.calls("root")] == ["root-call"]
        assert [item.sequence for item in current.calls("root", include_children=True)] == [1, 2, 3]
        page = current.calls("root", include_children=True, offset=1, limit=1)
        assert page[0].call_id == "child-call"
        assert page[0].status == "COMPLETED"
        assert page[0].messages[0].content == "Raw request"
        page[0].messages.clear()
        assert current.calls("child")[0].messages
    artifacts = registry.find_by_type(ArtifactType.RESEARCH_CALL)
    assert len(artifacts) == 4
    versions = sorted(
        [item for item in artifacts if item.metadata["call_id"] == "child-call"],
        key=lambda item: item.metadata["revision"],
    )
    assert versions[0].artifact_id in versions[1].parent_artifact_ids
    assert store.get_json(versions[0].uri)["receipt"]["status"] == "STARTED"
    assert store.get_json(versions[1].uri)["receipt"]["status"] == "COMPLETED"
    child_artifact = next(
        item
        for item in registry.find_by_type(ArtifactType.RESEARCH_RUN)
        if item.metadata["run_id"] == "child"
    )
    assert all(child_artifact.artifact_id in item.parent_artifact_ids for item in versions)


def test_call_identity_and_terminal_receipts_are_immutable(store, registry):
    repository = _repository(store, registry)
    repository.save(_run())
    repository.save(_run("child", parent="root", root="root"))
    intent = _receipt()
    repository.record_call("root", intent)
    with pytest.raises(QuantSystemError, match="sequence"):
        repository.record_call("root", _receipt(sequence=2))
    with pytest.raises(QuantSystemError, match="sequence"):
        repository.record_call("child", _receipt("other", 1))
    with pytest.raises(QuantSystemError, match="identity"):
        repository.record_call("root", intent.model_copy(update={"model": "other"}))
    complete = intent.model_copy(update={"status": "FAILED", "error": "raw failure"})
    repository.record_call("root", complete)
    repository.record_call("root", complete)
    assert len(registry.find_by_type(ArtifactType.RESEARCH_CALL)) == 2
    with pytest.raises(QuantSystemError, match="immutable"):
        repository.record_call("root", intent)
    with pytest.raises(ArtifactNotFoundError):
        repository.record_call("missing", _receipt())


def test_plan_and_planner_receipts_are_canonical_and_not_run_calls(store, registry):
    repository = _repository(store, registry)
    plan = ResearchPlan(
        plan_id="canonical-plan",
        source="MODEL",
        tasks=[PlanTask(task_id="inspect", task_type="sample.inspect", payload={"value": [1]})],
    )
    receipt = _receipt(
        status="COMPLETED",
        response=LLMResponse(
            content="Raw planner JSON", provider="fake", model="model", raw_request_id="planner-request"
        ),
    )
    artifact_id = repository.save_plan(plan, receipts=[receipt])
    assert repository.save_plan(plan, receipts=[receipt]) == artifact_id
    artifact = registry.get(artifact_id)
    assert artifact.artifact_type == ArtifactType.RESEARCH_PLAN
    assert "/research/runtime/plans/" in artifact.uri
    assert artifact.metadata["plan_id"] == plan.plan_id
    payload = store.get_json(artifact.uri)
    assert payload["plan"] == plan.model_dump(mode="json")
    assert payload["receipts"][0]["response"]["raw_request_id"] == "planner-request"
    receipt.messages.clear()
    plan.tasks[0].payload["value"].append(2)
    for current in (repository, _repository(store, registry)):
        assert current.get_plan(artifact_id).plan_id == "canonical-plan"
        assert current.plan_artifact_id("canonical-plan") == artifact_id
        assert current.plan_artifact_id(artifact_id) == artifact_id
        archived = current.plan_receipts(artifact_id)
        assert archived == current.plan_receipts("canonical-plan")
        assert archived[0].call_id == "call"
        assert archived[0].response.raw_request_id == "planner-request"
        archived[0].messages.clear()
        assert current.plan_receipts("canonical-plan")[0].messages
        snapshot = current.get_plan("canonical-plan")
        assert snapshot.tasks[0].payload["value"] == [1]
        snapshot.tasks.clear()
        assert current.get_plan("canonical-plan").tasks
        with pytest.raises(ArtifactNotFoundError):
            current.get_plan("missing")
        with pytest.raises(ArtifactNotFoundError):
            current.plan_receipts("missing")
        with pytest.raises(ArtifactNotFoundError):
            current.plan_artifact_id("missing")
    assert registry.find_by_type(ArtifactType.RESEARCH_CALL) == []
    assert len(registry.find_by_type(ArtifactType.RESEARCH_PLAN)) == 1
    with pytest.raises(QuantSystemError, match="conflict"):
        repository.save_plan(plan)


@pytest.mark.parametrize("method", ["get_plan", "plan_receipts", "plan_artifact_id"])
def test_plan_reads_recheck_archived_hash(store, registry, monkeypatch, method):
    repository = _repository(store, registry)
    artifact_id = repository.save_plan(_plan(), receipts=[_receipt()])
    artifact = registry.get(artifact_id)
    get_bytes = store.get_bytes
    monkeypatch.setattr(store, "get_bytes", lambda uri: b"{}" if uri == artifact.uri else get_bytes(uri))
    with pytest.raises(QuantSystemError, match="hash mismatch"):
        getattr(repository, method)(artifact_id)


def test_root_plan_request_hash_preserves_all_canonical_fields(store, registry):
    repository = _repository(store, registry)
    plan = ResearchPlan(
        plan_id="supplied-plan", tasks=[PlanTask(task_id="inspect", task_type="sample.inspect")]
    )
    repository.save_plan(plan)
    request = RunRequest(plan=plan, idempotency_key="plan-key")
    run = _run(key=None).model_copy(update={"request": request})
    saved = repository.save(run)
    canonical = request.model_dump(mode="json", exclude={"idempotency_key"})
    digest = hash_json(canonical)
    for current in (repository, _repository(store, registry)):
        assert current.get("root").request.model_dump(mode="json", exclude={"idempotency_key"}) == canonical
        assert current.find_idempotent("plan-key", digest) == saved
        changed = request.model_copy(update={"plan": plan.model_copy(update={"plan_id": "different"})})
        with pytest.raises(QuantSystemError, match="conflict"):
            current.find_idempotent(
                "plan-key", hash_json(changed.model_dump(mode="json", exclude={"idempotency_key"}))
            )


def test_root_planning_reference_is_immutable_and_bound_to_plan_artifact(store, registry, monkeypatch):
    repository = _repository(store, registry)
    plan = _plan()
    artifact_id = repository.save_plan(plan)
    request = RunRequest(plan=plan)
    run = _run(key=None).model_copy(update={"request": request, "usage": {"planning_plan_id": plan.plan_id}})
    saved = repository.save(run)
    assert repository.save(saved.model_copy(update={"stage": "running"})).revision == 2
    for envelope in registry.find_by_type(ArtifactType.RESEARCH_RUN):
        assert artifact_id in envelope.parent_artifact_ids
    for usage in ({}, {"planning_plan_id": "other-plan"}):
        with pytest.raises(QuantSystemError, match="identity conflict"):
            repository.save(saved.model_copy(update={"usage": usage}))
    reopened = _repository(store, registry)
    assert reopened.get("root").usage["planning_plan_id"] == plan.plan_id
    find = registry.find_by_type

    def missing_plan_parent(kind):
        return [
            envelope.model_copy(
                update={
                    "parent_artifact_ids": tuple(
                        parent for parent in envelope.parent_artifact_ids if parent != artifact_id
                    ),
                }
            )
            if kind == ArtifactType.RESEARCH_RUN
            else envelope
            for envelope in find(kind)
        ]

    monkeypatch.setattr(registry, "find_by_type", missing_plan_parent)
    with pytest.raises(QuantSystemError, match="planning reference|lineage"):
        _repository(store, registry)


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING"])
def test_load_and_get_are_pure_and_recover_is_explicit_idempotent(store, registry, monkeypatch, status):
    repository = _repository(store, registry)
    active = repository.save(_run(status=status))
    terminal = repository.save(_run("done", key=None, status="COMPLETED"))
    intent = _receipt(error="original error", parse_error="original parse error")
    repository.record_call("root", intent)
    repository.record_call("done", _receipt("completed", 1, status="FAILED", error="provider failure"))
    register = registry.register

    def forbidden(*args, **kwargs):
        raise AssertionError("constructor and reads must not write")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "register", forbidden)
        patch.setattr(store, "put_content_addressed_json", forbidden)
        reopened = _repository(store, registry)
        assert reopened.get("root") == active
        assert reopened.list(roots_only=False)
        assert reopened.calls("root")[0] == intent
        assert reopened.events("root") == []
    reopened.recover()
    interrupted = reopened.get("root")
    assert interrupted.status == "INTERRUPTED"
    assert interrupted.stage == "interrupted"
    assert interrupted.revision == active.revision + 1
    assert interrupted.updated_at == NOW
    assert reopened.get("done") == terminal
    recovered = reopened.calls("root")[0]
    assert recovered == intent.model_copy(update={"status": "UNKNOWN"})
    assert reopened.calls("done")[0].error == "provider failure"
    committed = len(registry.list_all())
    reopened.recover()
    assert len(registry.list_all()) == committed
    final = _repository(store, registry)
    final.recover()
    assert len(registry.list_all()) == committed
    assert final.get("root") == interrupted
    assert final.calls("root") == [recovered]
    assert registry.register == register


def test_recover_closes_started_calls_even_after_terminal_run_commit(store, registry):
    repository = _repository(store, registry)
    saved = repository.save(_run(status="COMPLETED"))
    receipt = _receipt()
    repository.record_call("root", receipt)
    repository.recover()
    assert repository.get("root") == saved
    assert repository.calls("root")[0] == receipt.model_copy(update={"status": "UNKNOWN"})


def _plan():
    return ResearchPlan(plan_id="plan", tasks=[PlanTask(task_id="inspect", task_type="sample.inspect")])


@pytest.mark.parametrize("kind", ["run", "event", "call", "plan"])
def test_failed_register_does_not_publish_memory_or_advance_counters(store, registry, monkeypatch, kind):
    repository = _repository(store, registry)
    initial = repository.save(_run())
    if kind == "call":
        repository.record_call("root", _receipt())
    count = len(registry.list_all())

    def commit():
        if kind == "run":
            return repository.save(initial.model_copy(update={"status": "RUNNING"}))
        if kind == "event":
            return repository.event("root", "started", "collect")
        if kind == "call":
            return repository.record_call("root", _receipt(status="FAILED", error="raw failure"))
        return repository.save_plan(_plan())

    def fail(envelope):
        raise QuantSystemError("registry unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "register", fail)
        with pytest.raises(QuantSystemError, match="registry unavailable"):
            commit()
        assert repository.get("root") == initial
        assert repository.events("root") == []
        if kind == "call":
            assert repository.calls("root")[0].status == "STARTED"
        with pytest.raises(ArtifactNotFoundError):
            repository.get_plan("plan")
    assert len(registry.list_all()) == count
    reopened = _repository(store, registry)
    assert reopened.get("root") == initial
    assert reopened.events("root") == []
    commit()
    assert len(registry.list_all()) == count + 1
    if kind == "run":
        assert repository.get("root").revision == 2
    elif kind == "event":
        assert repository.events("root")[0].sequence == 1
    elif kind == "call":
        assert repository.calls("root")[0].status == "FAILED"
        assert (
            max(item.metadata["revision"] for item in registry.find_by_type(ArtifactType.RESEARCH_CALL)) == 2
        )
    else:
        assert repository.get_plan("plan") == _plan()
    assert _repository(store, registry)


def test_failed_first_commit_does_not_claim_idempotency_key(store, registry, monkeypatch):
    repository = _repository(store, registry)
    run = _run()
    digest = hash_json(run.request.model_dump(mode="json", exclude={"idempotency_key"}))
    with monkeypatch.context() as patch:
        patch.setattr(
            registry, "register", lambda envelope: (_ for _ in ()).throw(QuantSystemError("register failed"))
        )
        with pytest.raises(QuantSystemError, match="register failed"):
            repository.save(run)
    assert repository.list() == []
    assert repository.find_idempotent("request-key", digest) is None
    with pytest.raises(ArtifactNotFoundError):
        repository.get("root")
    assert _repository(store, registry).list() == []
    assert repository.save(run).revision == 1


def _commit_kind(repository, kind):
    repository.save(_run())
    if kind == ArtifactType.RESEARCH_RUN:
        repository.save(repository.get("root").model_copy(update={"status": "RUNNING"}))
    elif kind == ArtifactType.RESEARCH_EVENT:
        repository.event("root", "started", "collect")
    elif kind == ArtifactType.RESEARCH_CALL:
        repository.record_call("root", _receipt())
    else:
        repository.save_plan(_plan(), receipts=[_receipt()])


KINDS = [
    ArtifactType.RESEARCH_RUN,
    ArtifactType.RESEARCH_EVENT,
    ArtifactType.RESEARCH_CALL,
    ArtifactType.RESEARCH_PLAN,
]


@pytest.mark.parametrize("kind", KINDS)
def test_load_verifies_all_content_hashes_including_old_run_revisions(store, registry, monkeypatch, kind):
    repository = _repository(store, registry)
    _commit_kind(repository, kind)
    artifact = registry.find_by_type(kind)[0]
    get_bytes = store.get_bytes

    def corrupt(uri):
        return b"{}" if uri == artifact.uri else get_bytes(uri)

    monkeypatch.setattr(store, "get_bytes", corrupt)
    with pytest.raises(QuantSystemError, match="hash mismatch"):
        _repository(store, registry)


@pytest.mark.parametrize("kind", KINDS)
def test_load_rejects_metadata_identity_mismatch(store, registry, monkeypatch, kind):
    repository = _repository(store, registry)
    _commit_kind(repository, kind)
    artifact = registry.find_by_type(kind)[0]
    changed = artifact.model_copy(update={"metadata": {**artifact.metadata, "run_id": "forged"}})
    find = registry.find_by_type
    monkeypatch.setattr(
        registry, "find_by_type", lambda current: [changed] if current == kind else find(current)
    )
    with pytest.raises(QuantSystemError, match="identity mismatch"):
        _repository(store, registry)


@pytest.mark.parametrize("kind", KINDS)
def test_duplicate_identical_commits_are_ambiguous_not_silently_selected(store, registry, kind):
    repository = _repository(store, registry)
    _commit_kind(repository, kind)
    artifact = registry.find_by_type(kind)[0]
    registry.register(artifact.model_copy(update={"artifact_id": "duplicate"}))
    with pytest.raises(QuantSystemError, match="ambiguous"):
        _repository(store, registry)


@pytest.mark.parametrize("problem", ["revision", "request_hash", "lineage"])
def test_load_rejects_forged_run_revisions_and_request_hashes(store, registry, problem):
    repository = _repository(store, registry)
    run = repository.save(_run())
    artifact = registry.find_by_type(ArtifactType.RESEARCH_RUN)[0]
    payload = store.get_json(artifact.uri)
    metadata = dict(artifact.metadata)
    if problem == "request_hash":
        payload["request_hash"] = metadata["request_hash"] = "0" * 64
        parents = (artifact.artifact_id,)
    else:
        payload["revision"] = metadata["revision"] = 0 if problem == "revision" else run.revision + 1
        parents = ()
    uri, digest = store.put_content_addressed_json("test/forged", payload)
    registry.register(
        artifact.model_copy(
            update={
                "artifact_id": "forged",
                "uri": uri,
                "content_hash": digest,
                "metadata": metadata,
                "parent_artifact_ids": parents,
            }
        )
    )
    with pytest.raises(QuantSystemError, match="identity|revision|lineage"):
        _repository(store, registry)


@pytest.mark.parametrize("kind", [ArtifactType.RESEARCH_EVENT, ArtifactType.RESEARCH_CALL])
def test_load_rejects_missing_owning_run_lineage(store, registry, monkeypatch, kind):
    repository = _repository(store, registry)
    _commit_kind(repository, kind)
    artifact = registry.find_by_type(kind)[0]
    changed = artifact.model_copy(update={"parent_artifact_ids": ()})
    find = registry.find_by_type
    monkeypatch.setattr(
        registry, "find_by_type", lambda current: [changed] if current == kind else find(current)
    )
    with pytest.raises(QuantSystemError, match="lineage"):
        _repository(store, registry)


@pytest.mark.parametrize("kind", [ArtifactType.RESEARCH_EVENT, ArtifactType.RESEARCH_CALL])
def test_load_rejects_incomplete_event_or_call_history(store, registry, monkeypatch, kind):
    repository = _repository(store, registry)
    _commit_kind(repository, kind)
    if kind == ArtifactType.RESEARCH_EVENT:
        repository.event("root", "next", "next")
        attribute = "sequence"
    else:
        repository.record_call("root", _receipt(status="FAILED", error="raw failure"))
        attribute = "revision"
    artifacts = registry.find_by_type(kind)
    latest = next(item for item in artifacts if item.metadata[attribute] == 2)
    find = registry.find_by_type
    monkeypatch.setattr(
        registry, "find_by_type", lambda current: [latest] if current == kind else find(current)
    )
    with pytest.raises(QuantSystemError, match="lineage"):
        _repository(store, registry)


@pytest.mark.parametrize("parent,root", [(None, "other"), ("missing", "root"), ("root", "other")])
def test_invalid_run_root_or_parent_is_not_committed(store, registry, parent, root):
    repository = _repository(store, registry)
    repository.save(_run())
    with pytest.raises(QuantSystemError, match="identity|parent|root"):
        repository.save(_run("bad", key=None, parent=parent, root=root))
    assert len(registry.find_by_type(ArtifactType.RESEARCH_RUN)) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_valid_hash_but_invalid_json_payload_fails_as_domain_error(store, registry, kind):
    uri, digest = store.put_content_addressed_json("test/invalid", {"unexpected": []})
    registry.register(
        ArtifactEnvelope(
            artifact_type=kind,
            producer="test",
            uri=uri,
            content_hash=digest,
        )
    )
    with pytest.raises(QuantSystemError, match="invalid|identity"):
        _repository(store, registry)


@pytest.mark.parametrize("kind", [ArtifactType.RESEARCH_RUN, ArtifactType.RESEARCH_CALL])
def test_recover_register_failure_is_retryable_without_replaying_or_losing_data(
    store, registry, monkeypatch, kind
):
    repository = _repository(store, registry)
    original = repository.save(_run(status="RUNNING", error="existing run error"))
    intent = _receipt(error="raw error", parse_error="raw parse error")
    repository.record_call("root", intent)
    register = registry.register

    def fail(envelope):
        if envelope.artifact_type == kind:
            raise QuantSystemError("recover register failed")
        return register(envelope)

    with monkeypatch.context() as patch:
        patch.setattr(registry, "register", fail)
        with pytest.raises(QuantSystemError, match="recover register failed"):
            repository.recover()
        assert repository.get("root") == original
        expected = "STARTED" if kind == ArtifactType.RESEARCH_CALL else "UNKNOWN"
        assert repository.calls("root")[0].status == expected
    reopened = _repository(store, registry)
    reopened.recover()
    assert reopened.get("root").status == "INTERRUPTED"
    assert reopened.get("root").error == "existing run error"
    assert reopened.calls("root")[0] == intent.model_copy(update={"status": "UNKNOWN"})
    count = len(registry.list_all())
    reopened.recover()
    assert len(registry.list_all()) == count


def test_list_parent_filters_pagination_and_deep_snapshots(store, registry):
    repository = _repository(store, registry)
    repository.save(_run("root-a", key="a"))
    repository.save(_run("root-b", key="b"))
    repository.save(_run("child", parent="root-a", root="root-a"))
    for current in (repository, _repository(store, registry)):
        assert [run.run_id for run in current.list()] == ["root-b", "root-a"]
        assert [run.run_id for run in current.list(parent_run_id="root-a")] == ["child"]
        assert current.list(parent_run_id="missing") == []
        assert [run.run_id for run in current.list(roots_only=False, offset=1, limit=1)] == ["root-a"]
        assert current.list(limit=0) == []
        current.list()[0].request.payload.clear()
        assert current.get("root-b").request.payload
        for query in (current.list, partial(current.events, "root-a"), partial(current.calls, "root-a")):
            with pytest.raises(ValueError, match="nonnegative"):
                query(offset=-1)
            with pytest.raises(ValueError, match="nonnegative"):
                query(limit=-1)


def test_run_lineage_uses_only_registered_actual_report_and_evidence_ids(store, registry):
    repository = _repository(store, registry)

    def evidence(name, kind):
        uri, digest = store.put_content_addressed_json("test/evidence", {"name": name})
        return registry.register(
            ArtifactEnvelope(artifact_type=kind, producer="test", uri=uri, content_hash=digest)
        )

    source = evidence("source", ArtifactType.RESEARCH_EVIDENCE)
    report = evidence("report", ArtifactType.RESEARCH_REPORT)
    result = TaskResult(
        outcome="OK",
        summary="Done",
        report_artifact_id=report.artifact_id,
        evidence=[
            EvidenceRef(artifact_id=source.artifact_id, content_hash=source.content_hash, available_at=NOW),
            EvidenceRef(artifact_id="unregistered", content_hash="0" * 64, available_at=NOW),
        ],
    )
    repository.save(_run())
    previous = registry.find_by_type(ArtifactType.RESEARCH_RUN)[0]
    saved = repository.save(repository.get("root").model_copy(update={"result": result}))
    latest = next(
        item for item in registry.find_by_type(ArtifactType.RESEARCH_RUN) if item.metadata["revision"] == 2
    )
    assert set(latest.parent_artifact_ids) == {previous.artifact_id, source.artifact_id, report.artifact_id}
    assert latest.metadata["task_type"] == saved.task_type
    assert latest.metadata["status"] == saved.status
    assert latest.metadata["task_version"] == saved.task_version
    assert _repository(store, registry).get("root").result == result


def _config():
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                "fake": {
                    "kind": "openai",
                    "base_url": "https://example.invalid",
                    "api_key_env": "TEST_STORAGE_KEY",
                }
            },
            "roles": {"researcher": {"provider": "fake", "model": "model", "max_output_tokens": 100}},
        }
    )


def test_structured_caller_commits_intent_before_gateway_and_persists_raw_completion(store, registry):
    repository = _repository(store, registry)
    repository.save(_run())
    sent = []

    class Gateway:
        def complete(self, role, messages):
            reopened = _repository(store, registry)
            intent = reopened.calls("root")[0]
            assert intent.status == "STARTED"
            assert intent.messages == messages
            assert len(registry.find_by_type(ArtifactType.RESEARCH_CALL)) == 1
            sent.append(intent.call_id)
            return LLMResponse(content="Raw result", provider="fake", model="model", raw_request_id="raw-id")

    caller = StructuredCaller(
        _config(),
        Gateway(),
        BudgetLedger(BudgetLimits()),
        sink=lambda receipt: repository.record_call("root", receipt),
    )
    caller.complete("researcher", [LLMMessage(role="user", content="Raw prompt")])
    receipt = _repository(store, registry).calls("root")[0]
    assert sent == [receipt.call_id]
    assert receipt.status == "COMPLETED"
    assert receipt.response.raw_request_id == "raw-id"
    assert receipt.messages[0].content == "Raw prompt"
    assert len(registry.find_by_type(ArtifactType.RESEARCH_CALL)) == 2


def test_failed_intent_sink_prevents_gateway_send(store, registry, monkeypatch):
    repository = _repository(store, registry)
    repository.save(_run())

    class Gateway:
        def complete(self, role, messages):
            raise AssertionError("must not send before durable intent")

    caller = StructuredCaller(
        _config(),
        Gateway(),
        BudgetLedger(BudgetLimits()),
        sink=lambda receipt: repository.record_call("root", receipt),
    )

    def fail(envelope):
        raise QuantSystemError("registry offline")

    monkeypatch.setattr(registry, "register", fail)
    with pytest.raises(QuantSystemError, match="sink failed"):
        caller.complete("researcher", [LLMMessage(role="user", content="Raw prompt")])
    assert repository.calls("root") == []
    assert _repository(store, registry).calls("root") == []


def test_threaded_commits_have_contiguous_revisions_and_event_sequences(store, registry):
    repository = _repository(store, registry)
    saved = repository.save(_run())
    barrier = Barrier(8, timeout=5)

    def commit(index):
        barrier.wait()
        run = repository.save(saved.model_copy(update={"stage": f"stage-{index}"}))
        event = repository.event("root", "progress", f"stage-{index}")
        repository.record_call("root", _receipt(f"call-{index}", index + 1))
        return run.revision, event.sequence

    with ThreadPoolExecutor(max_workers=8) as pool:
        committed = list(pool.map(commit, range(8)))
    assert sorted(revision for revision, _ in committed) == list(range(2, 10))
    assert sorted(sequence for _, sequence in committed) == list(range(1, 9))
    reopened = _repository(store, registry)
    assert reopened.get("root").revision == 9
    assert [event.sequence for event in reopened.events("root")] == list(range(1, 9))
    assert [receipt.sequence for receipt in reopened.calls("root")] == list(range(1, 9))
