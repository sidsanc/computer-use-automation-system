"""Model access for discovery only. Nothing under cua.replay may import this module."""

from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelTurn:
    content: list[Any]  # provider blocks, appended back to history unchanged
    tool_calls: list[ToolCall]
    text: str
    stop_reason: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    model_id: str

    def next_turn(self, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn: ...


class AnthropicModelClient:
    """Claude via the Messages API with a hand-driven tool loop.

    One action per turn (parallel tool use disabled): every action changes the page, so the
    next decision must see the new state. Refusals fall back server-side to Anthropic's
    recommended model; the served model is recorded in provenance.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL, effort: str = "high") -> None:
        import anthropic

        self._client = anthropic.Anthropic()
        self.model_id = model_id
        self.effort = effort

    def next_turn(self, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn:
        response = self._client.beta.messages.create(
            model=self.model_id,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            tools=tools,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            messages=messages,
        )
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in response.content if b.type == "text")
        usage = response.usage
        return ModelTurn(
            content=[b.model_dump(exclude_none=True) for b in response.content],
            tool_calls=calls,
            text=text,
            stop_reason=response.stop_reason or "",
            model=response.model,
            input_tokens=(usage.input_tokens or 0) + (usage.cache_creation_input_tokens or 0),
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            extra={"request_id": getattr(response, "_request_id", None)},
        )
