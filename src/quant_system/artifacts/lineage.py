"""Lineage traversal over the artifact registry."""

from __future__ import annotations

from collections import deque

from quant_system.artifacts.envelope import ArtifactEnvelope
from quant_system.artifacts.registry import ArtifactRegistry


class ArtifactLineageService:
    def __init__(self, registry: ArtifactRegistry) -> None:
        self._registry = registry

    def parents(self, artifact_id: str) -> list[ArtifactEnvelope]:
        return self._registry.parents(artifact_id)

    def children(self, artifact_id: str) -> list[ArtifactEnvelope]:
        return self._registry.children(artifact_id)

    def lineage(self, artifact_id: str) -> dict[str, list[ArtifactEnvelope]]:
        """Full ancestry (upstream) and descendants (downstream), BFS order."""
        return {
            "ancestors": self._walk(artifact_id, upstream=True),
            "descendants": self._walk(artifact_id, upstream=False),
        }

    def _walk(self, artifact_id: str, upstream: bool) -> list[ArtifactEnvelope]:
        seen: set[str] = {artifact_id}
        out: list[ArtifactEnvelope] = []
        queue: deque[str] = deque([artifact_id])
        while queue:
            current = queue.popleft()
            neighbors = (
                self._registry.parents(current) if upstream else self._registry.children(current)
            )
            for env in neighbors:
                if env.artifact_id not in seen:
                    seen.add(env.artifact_id)
                    out.append(env)
                    queue.append(env.artifact_id)
        return out

    def verify_lineage(self, artifact_id: str) -> bool:
        """All declared parents must exist; raises ArtifactNotFoundError otherwise."""
        env = self._registry.get(artifact_id)
        for pid in env.parent_artifact_ids:
            self._registry.get(pid)
        return True
