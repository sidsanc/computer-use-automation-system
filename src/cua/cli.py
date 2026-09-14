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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        result = ReplayEngine(browser, EnvSecretStore()).run(
            cap, tenant_config, workspace.app(tenant_config.app), _params(param), options)
        browser.close()
    typer.echo(result.model_dump_json(indent=2))
    raise typer.Exit(result.exit_code)


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
