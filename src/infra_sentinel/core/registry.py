"""Capability registry for resource modules currently known to Infra Sentinel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceKind:
    id: str
    resource_id: str
    metric_prefix: str
    title_key: str


class SourceRegistry:
    def __init__(self, source_kinds: tuple[SourceKind, ...]) -> None:
        self._source_kinds = {item.id: item for item in source_kinds}

    def require(self, source_kind: str) -> SourceKind:
        return self._source_kinds[source_kind]

    def capabilities(self) -> list[dict[str, str]]:
        return [
            {
                "id": item.id,
                "resource_id": item.resource_id,
                "metric_prefix": item.metric_prefix,
                "title_key": item.title_key,
            }
            for item in self._source_kinds.values()
        ]


DEFAULT_SOURCE_REGISTRY = SourceRegistry((
    SourceKind("network.mihomo", "network", "network.", "resource.network"),
    SourceKind("network.linux-vps", "network", "network.", "resource.network"),
    SourceKind("network.xray", "network", "network.", "resource.network"),
    SourceKind("ai.opencode", "ai_usage", "ai.", "resource.ai_usage"),
    SourceKind("ai.codex", "ai_usage", "ai.", "resource.ai_usage"),
    SourceKind("ai.infer-runtime", "ai_usage", "ai.", "resource.ai_usage"),
    SourceKind("upstream.statuspage", "upstream_status", "upstream.", "resource.upstream_status"),
    SourceKind("system.host", "system", "system.", "resource.system"),
))
