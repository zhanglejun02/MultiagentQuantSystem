from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from datetime import datetime
from threading import RLock
from typing import Any

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes, hash_json
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import ArtifactStore
from quant_system.domain.enums import ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, QuantSystemError
from quant_system.domain.types import utc_now
from quant_system.research.runtime.calls import BudgetLimits, CallReceipt
from quant_system.research.runtime.models import (
    ResearchEvent,
    ResearchPlan,
    ResearchRun,
    RunRequest,
    TaskResult,
)


def request_hash(request: RunRequest) -> str:
    return hash_json(request.model_dump(mode="json", exclude={"idempotency_key"}))


class ArtifactRunRepository:
    def __init__(
        self, store: ArtifactStore, registry: ArtifactRegistry, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.store = store
        self.registry = registry
        self._clock = clock
        self._lock = RLock()
        self._runs: dict[str, tuple[ResearchRun, ArtifactEnvelope]] = {}
        self._events: dict[tuple[str, int], ArtifactEnvelope] = {}
        self._calls: dict[tuple[str, str], tuple[int, ArtifactEnvelope]] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._plans: dict[str, tuple[ResearchPlan, ArtifactEnvelope]] = {}
        self._plan_ids: dict[str, str] = {}
        self._load()

    def _read(self, envelope: ArtifactEnvelope) -> dict[str, Any]:
        raw = self.store.get_bytes(envelope.uri)
        if hash_bytes(raw) != envelope.content_hash:
            raise QuantSystemError(f"research artifact hash mismatch: {envelope.artifact_id}")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise QuantSystemError(f"invalid research artifact JSON: {envelope.artifact_id}") from exc
        if not isinstance(payload, dict):
            raise QuantSystemError(f"invalid research artifact object: {envelope.artifact_id}")
        return payload

    def _write(
        self,
        kind: ArtifactType,
        payload: dict[str, Any],
        metadata: dict[str, Any],
        parents: tuple[str, ...] = (),
    ) -> ArtifactEnvelope:
        namespace = {
            ArtifactType.RESEARCH_RUN: "runs",
            ArtifactType.RESEARCH_EVENT: "events",
            ArtifactType.RESEARCH_CALL: "calls",
            ArtifactType.RESEARCH_PLAN: "plans",
        }[kind]
        uri, digest = self.store.put_content_addressed_json(f"research/runtime/{namespace}", payload)
        envelope = ArtifactEnvelope(
            artifact_type=kind,
            created_at=self._clock(),
            producer="research.runtime",
            content_hash=digest,
            uri=uri,
            metadata=metadata,
            parent_artifact_ids=tuple(sorted(set(parents))),
        )
        self.registry.register(envelope)
        return envelope

    def _load(self) -> None:
        try:
            self._load_records()
        except (KeyError, TypeError, ValueError) as exc:
            raise QuantSystemError("invalid research artifact identity or schema") from exc

    def _load_records(self) -> None:
        with self._lock:
            runs: dict[str, tuple[ResearchRun, ArtifactEnvelope]] = {}
            events: dict[tuple[str, int], ArtifactEnvelope] = {}
            calls: dict[tuple[str, str], tuple[int, ArtifactEnvelope]] = {}
            commits: dict[tuple[str, int], tuple[ResearchRun, ArtifactEnvelope]] = {}
            idempotency: dict[str, tuple[str, str]] = {}
            for envelope in self.registry.find_by_type(ArtifactType.RESEARCH_RUN):
                payload = self._read(envelope)
                digest = payload.pop("request_hash")
                run = ResearchRun.model_validate(payload)
                self._positive(run.revision, "run revision")
                if digest != request_hash(run.request) or envelope.metadata != self._run_metadata(run):
                    raise QuantSystemError(f"research run identity mismatch: {envelope.artifact_id}")
                run_commit = (run.run_id, run.revision)
                if run_commit in commits:
                    raise QuantSystemError(f"ambiguous research revision: {run_commit}")
                commits[run_commit] = (run, envelope)
                previous = runs.get(run.run_id)
                if previous is None or run.revision > previous[0].revision:
                    runs[run.run_id] = (run, envelope)
            run_artifacts: dict[str, set[str]] = {}
            for (run_id, revision), (run, envelope) in commits.items():
                self._validate_parent(run, runs)
                run_artifacts.setdefault(run_id, set()).add(envelope.artifact_id)
                if revision > 1:
                    prior_run = commits.get((run_id, revision - 1))
                    if prior_run is None or prior_run[1].artifact_id not in envelope.parent_artifact_ids:
                        raise QuantSystemError(f"research run revision lineage mismatch: {run_id}")
                    if self._run_identity(prior_run[0]) != self._run_identity(run):
                        raise QuantSystemError(f"research run identity mismatch: {run_id}")
            for envelope in self.registry.find_by_type(ArtifactType.RESEARCH_EVENT):
                event = ResearchEvent.model_validate(self._read(envelope))
                event_key = (event.run_id, event.sequence)
                self._positive(event.sequence, "event sequence")
                if event.run_id not in runs or envelope.metadata != self._event_metadata(event):
                    raise QuantSystemError(f"research event identity mismatch: {envelope.artifact_id}")
                if event_key in events:
                    raise QuantSystemError(f"ambiguous research event sequence: {event_key}")
                if not run_artifacts[event.run_id].intersection(envelope.parent_artifact_ids):
                    raise QuantSystemError("research event run lineage mismatch")
                events[event_key] = envelope
            for (owner, sequence), envelope in events.items():
                if sequence > 1:
                    prior_event = events.get((owner, sequence - 1))
                    if prior_event is None or prior_event.artifact_id not in envelope.parent_artifact_ids:
                        raise QuantSystemError("research event sequence lineage mismatch")
            call_commits: dict[tuple[str, str, int], tuple[CallReceipt, ArtifactEnvelope]] = {}
            sequences: dict[tuple[str, int], tuple[str, str]] = {}
            for envelope in self.registry.find_by_type(ArtifactType.RESEARCH_CALL):
                payload = self._read(envelope)
                receipt = CallReceipt.model_validate(payload["receipt"])
                call_key = (payload["run_id"], receipt.call_id)
                version = payload["revision"]
                self._positive(version, "call revision")
                self._positive(receipt.sequence, "call sequence")
                if call_key[0] not in runs or envelope.metadata != self._call_metadata(
                    runs[call_key[0]][0], receipt, version
                ):
                    raise QuantSystemError(f"research call identity mismatch: {envelope.artifact_id}")
                call_commit = (*call_key, version)
                if call_commit in call_commits:
                    raise QuantSystemError(f"ambiguous research call revision: {call_commit}")
                if not run_artifacts[call_key[0]].intersection(envelope.parent_artifact_ids):
                    raise QuantSystemError("research call run lineage mismatch")
                call_commits[call_commit] = (receipt, envelope)
                call_sequence = (runs[call_key[0]][0].root_run_id, receipt.sequence)
                if call_sequence in sequences and sequences[call_sequence] != call_key:
                    raise QuantSystemError(f"conflicting research call sequence: {call_sequence}")
                sequences[call_sequence] = call_key
                previous_call = calls.get(call_key)
                if previous_call is None or version > previous_call[0]:
                    calls[call_key] = (version, envelope)
            for (owner, call_id, version), (receipt, envelope) in call_commits.items():
                if version > 1:
                    prior_call = call_commits.get((owner, call_id, version - 1))
                    if prior_call is None or prior_call[1].artifact_id not in envelope.parent_artifact_ids:
                        raise QuantSystemError("research call revision lineage mismatch")
                    if self._call_identity(prior_call[0]) != self._call_identity(receipt):
                        raise QuantSystemError("research call identity mismatch")
                    if prior_call[0].status != "STARTED":
                        raise QuantSystemError("terminal research call receipt is immutable")
            for run, _ in runs.values():
                idempotency_key = run.request.idempotency_key
                if run.parent_run_id is None and idempotency_key is not None:
                    if idempotency_key in idempotency:
                        raise QuantSystemError(f"idempotency conflict: {idempotency_key}")
                    idempotency[idempotency_key] = (run.run_id, request_hash(run.request))
            plans: dict[str, tuple[ResearchPlan, ArtifactEnvelope]] = {}
            plan_ids: dict[str, str] = {}
            for envelope in self.registry.find_by_type(ArtifactType.RESEARCH_PLAN):
                payload = self._read(envelope)
                plan = ResearchPlan.model_validate(payload["plan"])
                receipts = [CallReceipt.model_validate(item) for item in payload["receipts"]]
                if envelope.metadata != self._plan_metadata(plan, receipts):
                    raise QuantSystemError(f"research plan identity mismatch: {envelope.artifact_id}")
                if plan.plan_id in plans:
                    raise QuantSystemError(f"ambiguous research plan commit: {plan.plan_id}")
                plans[plan.plan_id] = (plan, envelope)
                plan_ids[envelope.artifact_id] = plan.plan_id
            for run, envelope in commits.values():
                planning_plan_id = run.usage.get("planning_plan_id")
                if planning_plan_id is None:
                    continue
                registered = plans.get(planning_plan_id)
                if (
                    run.parent_run_id is not None
                    or run.request.plan is None
                    or registered is None
                    or planning_plan_id != run.request.plan.plan_id
                    or hash_json(run.request.plan.model_dump(mode="json"))
                    != hash_json(registered[0].model_dump(mode="json"))
                    or registered[1].artifact_id not in envelope.parent_artifact_ids
                ):
                    raise QuantSystemError("research planning reference or lineage mismatch")
            self._runs, self._events, self._calls = runs, events, calls
            self._idempotency = idempotency
            self._plans, self._plan_ids = plans, plan_ids

    @staticmethod
    def _validate_parent(run: ResearchRun, runs: dict[str, tuple[ResearchRun, ArtifactEnvelope]]) -> None:
        if run.parent_run_id is None:
            if run.root_run_id != run.run_id:
                raise QuantSystemError(f"research root identity mismatch: {run.run_id}")
            return
        ancestor: str | None = run.parent_run_id
        visited = {run.run_id}
        while ancestor is not None:
            if ancestor in visited:
                raise QuantSystemError(f"research parent identity cycle: {run.run_id}")
            visited.add(ancestor)
            if ancestor not in runs:
                raise ArtifactNotFoundError(f"research parent run not found: {ancestor}")
            parent = runs[ancestor][0]
            if parent.root_run_id != run.root_run_id:
                raise QuantSystemError(f"research root identity mismatch: {run.run_id}")
            ancestor = parent.parent_run_id

    @staticmethod
    def _run_identity(run: ResearchRun) -> dict[str, Any]:
        identity = run.model_dump(
            mode="json",
            include={
                "run_id",
                "root_run_id",
                "parent_run_id",
                "task_id",
                "task_type",
                "module_id",
                "task_version",
                "result_schema_version",
                "created_at",
                "request",
                "execution_plan",
            },
        )
        identity["planning_plan_id"] = run.usage.get("planning_plan_id")
        return identity

    @staticmethod
    def _run_metadata(run: ResearchRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "root_run_id": run.root_run_id,
            "parent_run_id": run.parent_run_id,
            "task_id": run.task_id,
            "task_type": run.task_type,
            "module_id": run.module_id,
            "task_version": run.task_version,
            "result_schema_version": run.result_schema_version,
            "status": run.status,
            "revision": run.revision,
            "request_hash": request_hash(run.request),
            "idempotency_key": run.request.idempotency_key,
        }

    def get(self, run_id: str) -> ResearchRun:
        with self._lock:
            if run_id not in self._runs:
                raise ArtifactNotFoundError(run_id)
            return self._read_run(self._runs[run_id][1])

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        parent_run_id: str | None = None,
        roots_only: bool = True,
    ) -> builtins.list[ResearchRun]:
        self._page(offset, limit)
        with self._lock:
            runs = [
                run
                for run, _ in self._runs.values()
                if (
                    run.parent_run_id == parent_run_id
                    if parent_run_id is not None
                    else not roots_only or run.parent_run_id is None
                )
            ]
            runs.sort(key=lambda run: (run.created_at, run.run_id), reverse=True)
            return [self.get(run.run_id) for run in runs[offset : offset + limit]]

    def find_idempotent(self, key: str, request_hash: str) -> ResearchRun | None:
        with self._lock:
            previous = self._idempotency.get(key)
            if previous is None:
                return None
            if previous[1] != request_hash:
                raise QuantSystemError(f"idempotency conflict: {key}")
            return self.get(previous[0])

    def load(self) -> builtins.list[ResearchRun]:
        with self._lock:
            return [
                self._read_run(envelope)
                for _, envelope in sorted(
                    self._runs.values(), key=lambda item: (item[0].created_at, item[0].run_id)
                )
            ]

    def _read_run(self, envelope: ArtifactEnvelope) -> ResearchRun:
        payload = self._read(envelope)
        digest = payload.pop("request_hash")
        run = ResearchRun.model_validate(payload)
        if digest != request_hash(run.request) or envelope.metadata != self._run_metadata(run):
            raise QuantSystemError(f"research run identity mismatch: {envelope.artifact_id}")
        return run.model_copy(deep=True)

    def recover(self, *, interrupt: bool = True) -> None:
        self._load()
        if not interrupt:
            return
        with self._lock:
            for (run_id, _), (_, envelope) in list(self._calls.items()):
                receipt = CallReceipt.model_validate(self._read(envelope)["receipt"])
                if receipt.status == "STARTED":
                    self.record_call(run_id, receipt.model_copy(update={"status": "UNKNOWN"}, deep=True))
            for run, _ in list(self._runs.values()):
                if run.status in {"QUEUED", "RUNNING"}:
                    self.save(run.model_copy(update={"status": "INTERRUPTED", "stage": "interrupted"}))

    @staticmethod
    def _plan_metadata(plan: ResearchPlan, receipts: builtins.list[CallReceipt]) -> dict[str, Any]:
        return {
            "plan_id": plan.plan_id,
            "schema_version": plan.schema_version,
            "source": plan.source,
            "planner_call_ids": [receipt.call_id for receipt in receipts],
        }

    def save_plan(self, plan: ResearchPlan, receipts: builtins.list[CallReceipt] | None = None) -> str:
        with self._lock:
            plan = plan.model_copy(deep=True)
            receipts = [receipt.model_copy(deep=True) for receipt in receipts or ()]
            payload = {
                "plan": plan.model_dump(mode="json"),
                "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
            }
            previous = self._plans.get(plan.plan_id)
            if previous is not None:
                if hash_json(self._read(previous[1])) != hash_json(payload):
                    raise QuantSystemError(f"research plan conflict: {plan.plan_id}")
                return previous[1].artifact_id
            envelope = self._write(ArtifactType.RESEARCH_PLAN, payload, self._plan_metadata(plan, receipts))
            self._plans[plan.plan_id] = (plan, envelope)
            self._plan_ids[envelope.artifact_id] = plan.plan_id
            return envelope.artifact_id

    def _read_plan(self, plan_id: str) -> tuple[ResearchPlan, builtins.list[CallReceipt], ArtifactEnvelope]:
        with self._lock:
            identity = self._plan_ids.get(plan_id, plan_id)
            if identity not in self._plans:
                raise ArtifactNotFoundError(plan_id)
            envelope = self._plans[identity][1]
            payload = self._read(envelope)
            plan = ResearchPlan.model_validate(payload["plan"])
            receipts = [CallReceipt.model_validate(item) for item in payload["receipts"]]
            if plan.plan_id != identity or envelope.metadata != self._plan_metadata(plan, receipts):
                raise QuantSystemError(f"research plan identity mismatch: {envelope.artifact_id}")
            return plan, receipts, envelope

    def get_plan(self, plan_id: str) -> ResearchPlan:
        return self._read_plan(plan_id)[0].model_copy(deep=True)

    def plan_receipts(self, plan_id: str) -> builtins.list[CallReceipt]:
        return [receipt.model_copy(deep=True) for receipt in self._read_plan(plan_id)[1]]

    def plan_artifact_id(self, plan_id: str) -> str:
        return self._read_plan(plan_id)[2].artifact_id

    def begin_plan_audit(self, plan_id: str, question: str, budget: BudgetLimits) -> None:
        now = self._clock()
        self.save(
            ResearchRun(
                run_id=plan_id,
                root_run_id=plan_id,
                task_type="runtime.planning",
                module_id="runtime",
                status="RUNNING",
                stage="planning",
                created_at=now,
                updated_at=now,
                request=RunRequest(
                    task_type="runtime.planning", payload={"question": question}, budget=budget
                ),
                usage={"max_calls": budget.max_calls, "max_tokens": budget.max_tokens},
            )
        )

    def finish_plan_audit(
        self,
        plan_id: str,
        plan: ResearchPlan | None,
        receipts: builtins.list[CallReceipt],
        error: str | None = None,
    ) -> None:
        run = self.get(plan_id)
        result = None
        if plan is not None:
            artifact_id = self.save_plan(plan, receipts)
            result = TaskResult(
                outcome="PROPOSED",
                summary=plan.question,
                payload={"plan_id": plan.plan_id, "plan_artifact_id": artifact_id},
            )
        usage = {
            **run.usage,
            "calls_used": len(receipts),
            "tokens_reserved": 0,
            "tokens_used": sum(item.used_tokens for item in receipts),
        }
        self.save(
            run.model_copy(
                update={
                    "status": "COMPLETED" if plan is not None else "FAILED",
                    "stage": "planned" if plan is not None else "planning_failed",
                    "result": result,
                    "error": error,
                    "usage": usage,
                }
            )
        )

    def save(self, run: ResearchRun) -> ResearchRun:
        with self._lock:
            run = run.model_copy(deep=True)
            self._validate_parent(run, self._runs)
            previous = self._runs.get(run.run_id)
            revision = previous[0].revision + 1 if previous else 1
            key = run.request.idempotency_key if run.parent_run_id is None else None
            digest = request_hash(run.request)
            if previous and self._run_identity(previous[0]) != self._run_identity(run):
                raise QuantSystemError(f"research run identity conflict: {run.run_id}")
            if key in self._idempotency and self._idempotency[key] != (run.run_id, digest):
                raise QuantSystemError(f"idempotency conflict: {key}")
            saved = run.model_copy(update={"revision": revision, "updated_at": self._clock()}, deep=True)
            parents = [previous[1].artifact_id] if previous else []
            if run.parent_run_id and run.parent_run_id in self._runs:
                parents.append(self._runs[run.parent_run_id][1].artifact_id)
            planning_plan_id = run.usage.get("planning_plan_id")
            if planning_plan_id is not None and (
                run.parent_run_id is not None
                or run.request.plan is None
                or planning_plan_id != run.request.plan.plan_id
            ):
                raise QuantSystemError("research planning reference must match its root request")
            if run.parent_run_id is None and run.request.plan is not None:
                try:
                    stored_plan = self.get_plan(run.request.plan.plan_id)
                except ArtifactNotFoundError:
                    if planning_plan_id is not None:
                        raise
                else:
                    if hash_json(stored_plan.model_dump(mode="json")) != hash_json(
                        run.request.plan.model_dump(mode="json")
                    ):
                        raise QuantSystemError("research plan does not match the registered plan")
                    parents.append(self.plan_artifact_id(stored_plan.plan_id))
            for child_id in run.children:
                if child_id in self._runs:
                    parents.append(self._runs[child_id][1].artifact_id)
            manifest = run.usage.get("evidence_manifest", {})
            if isinstance(manifest, dict):
                for reference in manifest.get("references", []):
                    artifact_id = reference["artifact_id"]
                    self.registry.get(artifact_id)
                    parents.append(artifact_id)
            if run.result and run.result.payload.get("plan_artifact_id"):
                plan_artifact_id = run.result.payload["plan_artifact_id"]
                self.registry.get(plan_artifact_id)
                parents.append(plan_artifact_id)
            if run.result:
                references = [reference.artifact_id for reference in run.result.evidence]
                if run.result.report_artifact_id is not None:
                    references.append(run.result.report_artifact_id)
                for artifact_id in references:
                    try:
                        self.registry.get(artifact_id)
                    except ArtifactNotFoundError:
                        continue
                    parents.append(artifact_id)
            envelope = self._write(
                ArtifactType.RESEARCH_RUN,
                {**saved.model_dump(mode="json"), "request_hash": digest},
                self._run_metadata(saved),
                tuple(parents),
            )
            self._runs[run.run_id] = (saved, envelope)
            if key is not None:
                self._idempotency[key] = (run.run_id, digest)
            return saved.model_copy(deep=True)

    @staticmethod
    def _page(offset: int, limit: int) -> None:
        if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 0:
            raise ValueError("pagination offset and limit must be nonnegative")

    @staticmethod
    def _positive(value: int, name: str) -> None:
        if type(value) is not int or value < 1:
            raise QuantSystemError(f"{name} must be a positive integer")

    def _belongs(self, owner: str, run_id: str) -> bool:
        visited = set()
        while owner in self._runs:
            if owner in visited:
                raise QuantSystemError("research run parent cycle")
            visited.add(owner)
            if owner == run_id:
                return True
            parent = self._runs[owner][0].parent_run_id
            if parent is None:
                break
            owner = parent
        return False

    @staticmethod
    def _event_metadata(event: ResearchEvent) -> dict[str, Any]:
        return {
            "run_id": event.run_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "stage": event.stage,
        }

    def event(
        self,
        run_id: ResearchEvent | str,
        event_type: str | None = None,
        stage: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> ResearchEvent:
        with self._lock:
            if isinstance(run_id, ResearchEvent):
                event = ResearchEvent.model_validate(run_id.model_dump()).model_copy(deep=True)
            else:
                self.get(run_id)
                sequence = max((index for child, index in self._events if child == run_id), default=0) + 1
                if event_type is None or stage is None:
                    raise QuantSystemError("event_type and stage are required")
                event = ResearchEvent(
                    run_id=run_id,
                    sequence=sequence,
                    event_type=event_type,
                    stage=stage,
                    at=self._clock(),
                    data=data or {},
                ).model_copy(deep=True)
            self.get(event.run_id)
            self._positive(event.sequence, "event sequence")
            key = (event.run_id, event.sequence)
            if key in self._events:
                if ResearchEvent.model_validate(self._read(self._events[key])) != event:
                    raise QuantSystemError("research event sequence is immutable")
                return event.model_copy(deep=True)
            parents = [self._runs[event.run_id][1].artifact_id]
            previous = [index for owner, index in self._events if owner == event.run_id]
            if event.sequence != max(previous, default=0) + 1:
                raise QuantSystemError("research event sequence must be contiguous")
            if previous:
                parents.append(self._events[(event.run_id, max(previous))].artifact_id)
            envelope = self._write(
                ArtifactType.RESEARCH_EVENT,
                event.model_dump(mode="json"),
                self._event_metadata(event),
                tuple(parents),
            )
            self._events[key] = envelope
            return event.model_copy(deep=True)

    def events(
        self,
        run_id: str,
        offset: int = 0,
        limit: int = 100,
        *,
        include_children: bool = False,
    ) -> builtins.list[ResearchEvent]:
        self._page(offset, limit)
        with self._lock:
            self.get(run_id)
            envelopes = [
                envelope
                for (owner, _), envelope in self._events.items()
                if owner == run_id or include_children and self._belongs(owner, run_id)
            ]
            envelopes.sort(
                key=lambda envelope: (
                    envelope.created_at,
                    envelope.metadata["run_id"],
                    envelope.metadata["sequence"],
                )
            )
            return [
                ResearchEvent.model_validate(self._read(envelope)).model_copy(deep=True)
                for envelope in envelopes[offset : offset + limit]
            ]

    @staticmethod
    def _call_identity(receipt: CallReceipt) -> dict[str, Any]:
        return receipt.model_dump(
            mode="json", exclude={"status", "response", "error", "parse_error", "used_tokens"}
        )

    @staticmethod
    def _call_metadata(run: ResearchRun, receipt: CallReceipt, revision: int) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "root_run_id": run.root_run_id,
            "call_id": receipt.call_id,
            "sequence": receipt.sequence,
            "revision": revision,
            "status": receipt.status,
        }

    def record_call(self, run_id: str, receipt: CallReceipt) -> None:
        with self._lock:
            run = self.get(run_id)
            receipt = receipt.model_copy(deep=True)
            self._positive(receipt.sequence, "call sequence")
            key = (run_id, receipt.call_id)
            previous = self._calls.get(key)
            if previous:
                old = CallReceipt.model_validate(self._read(previous[1])["receipt"])
                if old.sequence != receipt.sequence:
                    raise QuantSystemError("research call sequence is immutable")
                if self._call_identity(old) != self._call_identity(receipt):
                    raise QuantSystemError("research call identity is immutable")
                if old == receipt:
                    return
                if old.status != "STARTED":
                    raise QuantSystemError("terminal research call receipt is immutable")
            if any(
                (other_run, other_id) != key
                and self._runs[other_run][0].root_run_id == run.root_run_id
                and int(envelope.metadata["sequence"]) == receipt.sequence
                for (other_run, other_id), (_, envelope) in self._calls.items()
            ):
                raise QuantSystemError("duplicate research call sequence")
            version = previous[0] + 1 if previous else 1
            parents = [self._runs[run_id][1].artifact_id]
            if previous:
                parents.append(previous[1].artifact_id)
            envelope = self._write(
                ArtifactType.RESEARCH_CALL,
                {"run_id": run_id, "revision": version, "receipt": receipt.model_dump(mode="json")},
                self._call_metadata(run, receipt, version),
                tuple(parents),
            )
            self._calls[key] = (version, envelope)

    def call(self, run_id: str, receipt: CallReceipt) -> None:
        self.record_call(run_id, receipt)

    def calls(
        self, run_id: str, offset: int = 0, limit: int = 100, *, include_children: bool = False
    ) -> builtins.list[CallReceipt]:
        self._page(offset, limit)
        with self._lock:
            self.get(run_id)
            envelopes = [
                envelope
                for (owner, _), (_, envelope) in self._calls.items()
                if owner == run_id or include_children and self._belongs(owner, run_id)
            ]
            envelopes.sort(key=lambda envelope: (envelope.metadata["sequence"], envelope.metadata["call_id"]))
            return [
                CallReceipt.model_validate(self._read(envelope)["receipt"]).model_copy(deep=True)
                for envelope in envelopes[offset : offset + limit]
            ]
