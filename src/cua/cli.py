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
