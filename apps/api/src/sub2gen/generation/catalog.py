"""Provider-neutral model descriptors and compatibility aliases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sub2gen_provider_sdk import GenerationKind


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    provider_id: str
    resolved_model: str
    kind: GenerationKind
    billing_pool: str
    capability: str
    credential_kinds: frozenset[str]
    execution_location: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if "/" not in self.model_id:
            raise ValueError("canonical model IDs must be provider-namespaced")
        if not self.credential_kinds:
            raise ValueError("a model must declare at least one credential kind")


class ModelRegistry:
    def __init__(self, descriptors: tuple[ModelDescriptor, ...]) -> None:
        self._descriptors: dict[str, ModelDescriptor] = {}
        self._aliases: dict[str, str] = {}
        for descriptor in descriptors:
            if descriptor.model_id in self._descriptors or descriptor.model_id in self._aliases:
                raise ValueError(f"duplicate model ID: {descriptor.model_id}")
            self._descriptors[descriptor.model_id] = descriptor
            for alias in descriptor.aliases:
                if alias in self._descriptors or alias in self._aliases:
                    raise ValueError(f"duplicate model alias: {alias}")
                self._aliases[alias] = descriptor.model_id

    def resolve(self, requested_model: str) -> ModelDescriptor:
        canonical = self._aliases.get(requested_model, requested_model)
        try:
            return self._descriptors[canonical]
        except KeyError as exc:
            raise KeyError(f"unknown model: {requested_model}") from exc

    def list(self) -> tuple[ModelDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    @classmethod
    def for_platform(cls, flow_models: Mapping[str, Mapping[str, object]]) -> "ModelRegistry":
        descriptors: list[ModelDescriptor] = []
        for legacy_id, config in sorted(flow_models.items()):
            kind = GenerationKind.VIDEO if config.get("type") == "video" else GenerationKind.IMAGE
            descriptors.append(
                ModelDescriptor(
                    model_id=f"google-flow/{legacy_id}",
                    provider_id="google-flow",
                    resolved_model=legacy_id,
                    kind=kind,
                    billing_pool="google-flow:subscription",
                    capability=f"{kind.value}.generate:google-flow",
                    credential_kinds=frozenset({"session_token"}),
                    execution_location="server",
                    aliases=(legacy_id,),
                )
            )
        descriptors.extend(
            (
                ModelDescriptor(
                    model_id="chatgpt/gpt-image-web",
                    provider_id="chatgpt-web",
                    resolved_model="chatgpt/gpt-image-web",
                    kind=GenerationKind.IMAGE,
                    billing_pool="chatgpt:web-subscription",
                    capability="image.generate:chatgpt-web",
                    credential_kinds=frozenset({"browser_session"}),
                    execution_location="local-worker",
                    aliases=("gpt-image-web",),
                ),
                ModelDescriptor(
                    model_id="chatgpt/gpt-image-codex",
                    provider_id="chatgpt-codex",
                    resolved_model="chatgpt/gpt-image-codex",
                    kind=GenerationKind.IMAGE,
                    billing_pool="chatgpt:codex-subscription",
                    capability="image.generate:chatgpt-codex",
                    credential_kinds=frozenset({"oauth"}),
                    execution_location="local-worker",
                ),
            )
        )
        return cls(tuple(descriptors))
