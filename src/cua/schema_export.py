import json
from pathlib import Path

from cua.schema.artifact import Capability
from cua.schema.results import RunResult


def export_schemas(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, model in (("capability.schema.json", Capability), ("run-result.schema.json", RunResult)):
        path = directory / filename
        path.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
