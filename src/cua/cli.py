import json
from typing import Annotated

import httpx
import typer
from dotenv import load_dotenv

app = typer.Typer(no_args_is_help=True, add_completion=False)
faults_app = typer.Typer(no_args_is_help=True, help="Inject runtime faults into the mock app.")
app.add_typer(faults_app, name="faults")

PortOpt = Annotated[int, typer.Option(help="Port of the running mock app.")]


@app.callback()
def _main() -> None:
    load_dotenv()


@app.command("serve-mock")
def serve_mock(
    variant: Annotated[str, typer.Option(help="a = base tenant, b = rebranded variant.")] = "a",
    port: PortOpt = 5001,
) -> None:
    """Run the mock legacy credit-union back-office app."""
    from mockbank.app import create_app

    create_app(variant=variant).run(host="127.0.0.1", port=port, threaded=True)


@app.command("schema")
def schema(out: Annotated[str, typer.Option(help="Directory to write JSON Schemas into.")] = "schemas") -> None:
    """Export JSON Schemas for the capability artifact and the run result."""
    from pathlib import Path

    from cua.schema_export import export_schemas

    for path in export_schemas(Path(out)):
        typer.echo(f"wrote {path}")


def _operator_gate(attended: bool, port: int, timeout_s: float):
    """Attended runs get a live operator console; unattended runs return needs_human instead of waiting."""
    if not attended:
        return None, None
    from cua.handoff.console import AttendedGate, OperatorConsole
    from cua.handoff.control import ControlLease

    console = OperatorConsole(port=port)
    console.start()
    typer.secho(f"Operator console: {console.url}", fg="green", err=True)
    return AttendedGate(console, ControlLease(), timeout_s=timeout_s), console


def _load_capability(ref: str):
    from pathlib import Path

    from cua.schema.artifact import Capability

    path = Path(ref)
    if not path.exists() and "@" in ref:
        cap_id, version = ref.split("@", 1)
        path = Path("capabilities") / cap_id / f"{version}.json"
    if not path.exists():
        raise typer.BadParameter(f"capability not found: {ref} (use a file path or id@version)")
    return Capability.model_validate_json(path.read_text(encoding="utf-8"))


def _params(values: list[str]) -> dict[str, str]:
    pairs = {}
    for item in values:
        if "=" not in item:
            raise typer.BadParameter(f"expected name=value, got {item!r}")
        name, value = item.split("=", 1)
        pairs[name] = value
    return pairs


@app.command()
def replay(
    capability: Annotated[str, typer.Argument(help="Artifact path or id@version.")],
    tenant: Annotated[str, typer.Option(help="Tenant id from config/tenants.")],
    param: Annotated[list[str], typer.Option("--param", "-p", help="Input as name=value.")] = [],  # noqa: B006
    attended: Annotated[bool, typer.Option(help="Pause for a human instead of returning needs_human.")] = False,
    allow_irreversible: Annotated[bool, typer.Option(help="Permit irreversible steps if approved.")] = False,
    headed: Annotated[bool, typer.Option(help="Show the browser window.")] = False,
    trace: Annotated[bool, typer.Option(help="Keep a Playwright trace when the run does not succeed.")] = False,
    video: Annotated[bool, typer.Option(help="Record video (unmasked; fake data only).")] = False,
    overlay: Annotated[bool, typer.Option(help="Apply the tenant's overlay, when it has one.")] = True,
    operator_port: Annotated[int, typer.Option(help="Port for the operator console (attended runs).")] = 8765,
    operator_timeout: Annotated[float, typer.Option(help="Seconds to wait for an operator.")] = 300,
    evidence_root: Annotated[str, typer.Option(help="Where run evidence directories are written.")] = "runs",
) -> None:
    """Replay a capability deterministically. No model is used. Exit code reflects the result status."""
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    from cua.config import Workspace
    from cua.replay.engine import ReplayEngine, ReplayOptions
    from cua.secrets import EnvSecretStore

    cap = _load_capability(capability)
    workspace = Workspace()
    tenant_config = workspace.tenant(tenant)
    options = ReplayOptions(attended=attended, allow_irreversible=allow_irreversible, trace=trace, video=video,
                            evidence_root=Path(evidence_root))
    gate, console = _operator_gate(attended, operator_port, operator_timeout)
    tenant_overlay = workspace.overlay(tenant_config.overlay) if overlay and tenant_config.overlay else None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed and not attended)
        result = ReplayEngine(browser, EnvSecretStore(), gate).run(
            cap, tenant_config, workspace.app(tenant_config.app), _params(param), options, overlay=tenant_overlay)
        browser.close()
    if console is not None:
        console.stop()
    typer.echo(result.model_dump_json(indent=2))
    raise typer.Exit(result.exit_code)


@app.command()
def discover(
    goal_file: Annotated[str, typer.Argument(help="Goal spec YAML (goal + typed input/output contract).")],
    tenant: Annotated[str, typer.Option(help="Tenant id from config/tenants.")],
    param: Annotated[list[str], typer.Option("--param", "-p", help="Discovery input as name=value.")] = [],  # noqa: B006
    model: Annotated[str, typer.Option(help="Claude model id.")] = "claude-sonnet-5",
    effort: Annotated[str, typer.Option(help="low | medium | high | xhigh | max")] = "high",
    verify_param: Annotated[list[str], typer.Option(help="Inputs for the verification replay (default: same).")] = [],  # noqa: B006
    attended: Annotated[bool, typer.Option(help="Open an operator console for approvals and takeovers.")] = False,
    operator_port: Annotated[int, typer.Option(help="Port for the operator console.")] = 8765,
    operator_timeout: Annotated[float, typer.Option(help="Seconds to wait for an operator.")] = 300,
    headed: Annotated[bool, typer.Option(help="Show the browser window.")] = False,
    video: Annotated[bool, typer.Option(help="Record video (unmasked; fake data only).")] = False,
    evidence_root: Annotated[str, typer.Option()] = "runs",
) -> None:
    """Let Claude accomplish a goal on the live app, record it as a capability, then replay it without the model."""
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    from cua.config import Workspace
    from cua.discovery.agent import DiscoveryAgent, DiscoveryOptions
    from cua.discovery.goal import GoalSpec
    from cua.discovery.llm import AnthropicModelClient
    from cua.replay.engine import ReplayEngine, ReplayOptions
    from cua.secrets import EnvSecretStore

    goal = GoalSpec.load(Path(goal_file))
    workspace = Workspace()
    tenant_config = workspace.tenant(tenant)
    app_profile = workspace.app(tenant_config.app)
    inputs = _params(param)
    gate, console = _operator_gate(attended, operator_port, operator_timeout)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed and not attended)
        agent = DiscoveryAgent(browser, AnthropicModelClient(model, effort), EnvSecretStore(), gate)
        result = agent.run(goal, tenant_config, app_profile, inputs,
                           DiscoveryOptions(evidence_root=Path(evidence_root), video=video))
        summary = {"discovery": {k: v for k, v in vars(result).items() if k != "capability"}}
        if result.capability is not None:
            verification = ReplayEngine(browser, EnvSecretStore()).run(
                result.capability, tenant_config, app_profile, _params(verify_param) or inputs,
                ReplayOptions(evidence_root=Path(evidence_root)))
            summary["verification_replay"] = json.loads(verification.model_dump_json())
        browser.close()
    if console is not None:
        console.stop()
    typer.echo(json.dumps(summary, indent=2))
    raise typer.Exit(0 if result.status == "recorded" else 20 if result.status == "needs_human" else 40)


def _faults_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/__admin/faults"


@faults_app.command("set")
def faults_set(
    name: str,
    value: Annotated[str | None, typer.Argument(help="Seconds for slow_load.")] = None,
    port: PortOpt = 5001,
) -> None:
    resp = httpx.post(_faults_url(port), json={"name": name, "value": value})
    resp.raise_for_status()
    typer.echo(resp.json())


@faults_app.command("clear")
def faults_clear(port: PortOpt = 5001) -> None:
    resp = httpx.post(_faults_url(port), json={"clear": True})
    resp.raise_for_status()
    typer.echo(resp.json())


@faults_app.command("show")
def faults_show(port: PortOpt = 5001) -> None:
    resp = httpx.get(_faults_url(port))
    resp.raise_for_status()
    typer.echo(resp.json())
