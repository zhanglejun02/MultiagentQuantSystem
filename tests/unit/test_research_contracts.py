from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from quant_system.research.runtime.models import EvidenceRef, ResearchPlan, RunRequest, TaskDescriptor
from quant_system.research.runtime.ports import ModuleRegistry, ResearchTask


def test_nonfinancial_module_registers_without_changing_global_roles() -> None:
    class Request(BaseModel):
        text: str

    registry = ModuleRegistry()
    task = ResearchTask(
        TaskDescriptor(task_type="documents.inspect", module_id="documents", title="Inspect"),
        Request,
        lambda *_: None,
    )
    registry.register(task)
    assert registry.get("documents.inspect") is task
    assert "text" in registry.describe()[0].request_schema["properties"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(task)


def test_evidence_requires_hash_and_aware_available_time() -> None:
    reference = EvidenceRef(artifact_id="a", content_hash="a" * 64, available_at=datetime.now(UTC))
    assert reference.kind == "FACT"
    with pytest.raises(ValidationError):
        EvidenceRef(artifact_id="a", content_hash="missing", available_at=datetime.now(UTC))
    with pytest.raises(ValidationError):
        EvidenceRef(artifact_id="a", content_hash="a" * 64, available_at=datetime(2026, 1, 1))


def test_run_entry_is_explicit_and_historical_cutoff_is_mandatory() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        RunRequest()
    with pytest.raises(ValidationError, match="knowledge cutoff"):
        RunRequest(task_type="documents.inspect", mode="HISTORICAL")
    with pytest.raises(ValidationError, match="explicit knowledge cutoff"):
        ResearchPlan(tasks=[{"task_id": "one", "task_type": "documents.inspect"}], mode="HISTORICAL")
