"""Per-run evidence directory: an append-only JSONL event log plus attachments.

Every text or JSON write is redacted here. Binary attachments (screenshots, traces, video)
cannot be redacted after the fact, so callers must produce them masked; the log records
which masking was applied.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from cua.policy.redact import Redactor

RunKind = Literal["discovery", "replay"]


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    return value


class EvidenceLog:
    def __init__(self, root: Path, run_id: str, kind: RunKind, redactor: Redactor) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = run_id
        self.redactor = redactor
        self.dir = root / f"{stamp}_{kind}_{run_id}"
        self.dir.mkdir(parents=True, exist_ok=False)
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._seq = 0

    def event(self, type: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        record = {"seq": self._seq, "ts": datetime.now(UTC).isoformat(), "type": type}
        record |= self.redactor.value(_jsonable(fields))
        self._events.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._events.flush()
        return record

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.redactor.value(_jsonable(data)), indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.text(text), encoding="utf-8")
        return path

    def attach_masked_bytes(self, name: str, data: bytes, masking: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.event("attachment", name=name, bytes=len(data), masking=masking)
        return path

    def relative(self, path: Path) -> str:
        return path.relative_to(self.dir).as_posix()

    def close(self) -> None:
        self._events.close()

    def __enter__(self) -> "EvidenceLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
