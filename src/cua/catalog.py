"""The agent-facing side of an artifact: a catalogue of callable capabilities.

This is the other half of the through-line. Discovery writes artifacts; replay executes them; this
module is how an AI agent *finds* one and *calls* it — by name, with typed arguments, getting typed
outputs or a business-outcome code back. It deliberately contains no model code: the same catalogue
serves a Claude tool loop, an MCP server or a plain HTTP caller.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cua.config import AppProfile, TenantConfig, Workspace
from cua.schema.artifact import Capability
from cua.schema.results import RunResult


@dataclass(frozen=True)
class CapabilityEntry:
    capability: Capability
    path: Path

    @property
    def name(self) -> str:
        return f"{self.capability.id}@{self.capability.version}"

    def tool_name(self) -> str:
        """A tool name an LLM can call: providers reject dots and '@'."""
        return self.capability.id.replace(".", "_")

    def as_tool(self, app: AppProfile | None = None) -> dict[str, Any]:
        contract = self.capability.contract()
        # Product-level outcomes ("not on file", "access denied") apply to every capability of that
        # app, so a caller must be told about them too.
        outcomes_map = dict(contract["business_outcomes"])
        if app is not None:
            outcomes_map = {h.code: h.message for h in app.handlers if h.kind == "business_outcome"} | outcomes_map
        outcomes = ", ".join(f"{code} ({message})" for code, message in sorted(outcomes_map.items()))
        description = (
            f"{contract['title']}. {contract['description']} "
            f"Risk: {contract['risk']}. Returns: {', '.join(contract['outputs']['properties']) or 'nothing'}."
        )
        if outcomes:
            description += f" May instead report a business outcome: {outcomes}."
        if contract["requires_human"]:
            description += " Requires a human operator for part of the flow."
        return {"name": self.tool_name(), "description": description, "input_schema": contract["inputs"],
                "_business_outcomes": outcomes_map}


class Catalog:
    """Capabilities on disk, exposed as contracts an agent can read and invoke."""

    def __init__(self, capabilities_root: Path = Path("capabilities"), workspace: Workspace | None = None) -> None:
        self.root = capabilities_root
        self.workspace = workspace or Workspace()

    def entries(self, latest_only: bool = True) -> list[CapabilityEntry]:
        found: dict[str, CapabilityEntry] = {}
        for path in sorted(self.root.glob("*/*.json")):
            capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
            entry = CapabilityEntry(capability, path)
            key = capability.id if latest_only else entry.name
            current = found.get(key)
            if current is None or _semver(capability.version) > _semver(current.capability.version):
                found[key] = entry
        return sorted(found.values(), key=lambda e: e.name)

    def _app_for(self, entry: CapabilityEntry) -> AppProfile | None:
        try:
            return self.workspace.app(entry.capability.app.product)
        except FileNotFoundError:
            return None

    def contracts(self) -> list[dict[str, Any]]:
        out = []
        for entry in self.entries():
            app = self._app_for(entry)
            contract = entry.capability.contract() | {"artifact": entry.path.as_posix()}
            contract["business_outcomes"] = entry.as_tool(app)["_business_outcomes"]
            out.append(contract)
        return out

    def tools(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in e.as_tool(self._app_for(e)).items() if not k.startswith("_")}
                for e in self.entries()]

    def find(self, name: str) -> CapabilityEntry:
        """Accept an id, an id@version, or the tool name an LLM was given; bare names get the latest."""
        entries = self.entries(latest_only=False)
        exact = next((e for e in entries if e.name == name), None)
        if exact is not None:
            return exact
        matching = [e for e in self.entries() if name in (e.capability.id, e.tool_name())]
        if matching:
            return matching[0]
        known = sorted({e.capability.id for e in entries})
        raise KeyError(f"no capability named {name!r}; known capabilities: {known}")

    def target(self, tenant_id: str) -> tuple[TenantConfig, AppProfile]:
        tenant = self.workspace.tenant(tenant_id)
        return tenant, self.workspace.app(tenant.app)

    def overlay_for(self, tenant: TenantConfig):
        return self.workspace.overlay(tenant.overlay) if tenant.overlay else None

    @staticmethod
    def agent_view(result: RunResult) -> dict[str, Any]:
        """What the calling agent gets back: an answer, a business outcome, or why it could not proceed."""
        view: dict[str, Any] = {"status": result.status, "capability": result.capability.id,
                                "version": result.capability.version, "tenant": result.tenant,
                                "run_id": result.run_id, "evidence_dir": result.evidence_dir}
        if result.status == "success":
            view["outputs"] = result.outputs
        elif result.outcome is not None:
            view["outcome"] = {"code": result.outcome.code, "message": result.outcome.message}
        elif result.policy is not None:
            view["blocked"] = {"rule": result.policy.rule, "reason": result.policy.reason}
        elif result.intervention is not None:
            view["needs_human"] = {"intervention_id": result.intervention.intervention_id,
                                   "reason": result.intervention.reason}
        elif result.failure is not None:
            view["error"] = {"category": result.failure.category, "step": result.failure.step_id,
                             "expected": result.failure.expected, "observed": result.failure.observed}
        if result.side_effects.possible:
            view["side_effects_possible"] = list(result.side_effects.possible)
        return view


def _semver(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


AGENT_SYSTEM = """\
You are an AI agent serving a credit union's back office. You cannot operate the applications \
yourself: you call capabilities, each of which is a recorded, reviewed flow that runs deterministically.

- Choose the single capability that answers the request and call it with typed arguments.
- A capability may answer with a business outcome instead of data (for example "no member on file"). \
That is a real answer: report it plainly, do not retry.
- If a call is blocked by policy or needs a human, say so and stop; do not look for another way around it.
- When you have the result, answer the person in one or two sentences."""


def run_agent_request(catalog: "Catalog", model: Any, request: str, invoke, max_turns: int = 4) -> dict[str, Any]:
    """One turn of the production loop: an agent picks a capability, calls it, and answers.

    `model` is any client with `next_turn(system, tools, messages)`; `invoke` runs the chosen
    capability and returns a RunResult. Neither is imported here, so this is provider-agnostic and
    testable without an API key.
    """
    tools = catalog.tools()
    messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
    calls: list[dict[str, Any]] = []
    for _ in range(max_turns):
        turn = model.next_turn(AGENT_SYSTEM, tools, messages)
        messages.append({"role": "assistant", "content": turn.content})
        if not turn.tool_calls:
            return {"answer": turn.text, "invocations": calls}
        results = []
        for call in turn.tool_calls:
            try:
                entry = catalog.find(call.name)
                arguments = {k: str(v) for k, v in call.input.items()}
                view = Catalog.agent_view(invoke(entry, arguments))
            except KeyError as exc:
                view = {"status": "error", "error": str(exc)}
            calls.append({"capability": call.name, "arguments": call.input, "result": view})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": _json(view)})
        messages.append({"role": "user", "content": results})
    return {"answer": "The agent did not reach an answer within its turn budget.", "invocations": calls}


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
