"""The single redaction chokepoint. Everything persisted (event logs, artifacts, intervention
requests, run results) and everything shown to the model goes through a Redactor.

Layers, strongest first: exact secret values, exact PII input values supplied for this run,
values shown next to sensitive field captions, then generic patterns. Pattern matching is
best effort; the exact-value layers are what make guarantees.
"""

import re
from collections.abc import Iterable
from typing import Any

from cua.surface.base import CELL_ROLES, Observation

SENSITIVE_KEYS = re.compile(r"(?:.*[_-])?(?:password|passwd|secret|token|api[_-]?key|authorization|cookie)", re.I)
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[card]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[api-key]"),
)


class Redactor:
    def __init__(
        self,
        *,
        secrets: Iterable[str] = (),
        pii_values: Iterable[str] = (),
        sensitive_captions: Iterable[str] = (),
        sensitive_columns: Iterable[str] = (),
    ) -> None:
        self._secrets = {s for s in secrets if s}
        self._pii = {p for p in pii_values if p}
        self._captions = {self._caption(c) for c in sensitive_captions}
        self._columns = {self._caption(c) for c in sensitive_columns}

    def add_secret(self, value: str) -> None:
        if value:
            self._secrets.add(value)

    def add_pii(self, value: str) -> None:
        if value:
            self._pii.add(value)

    @staticmethod
    def _caption(text: str) -> str:
        return text.strip().rstrip(":").strip().lower()

    def text(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, "[secret]")
        for pii in sorted(self._pii, key=len, reverse=True):
            value = value.replace(pii, "[pii]")
        for pattern, replacement in PATTERNS:
            value = pattern.sub(replacement, value)
        return value

    def value(self, data: Any) -> Any:
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, dict):
            return {
                k: "[redacted]" if isinstance(k, str) and SENSITIVE_KEYS.fullmatch(k) and v else self.value(v)
                for k, v in data.items()
            }
        if isinstance(data, list | tuple):
            return [self.value(v) for v in data]
        return data

    def observation(self, obs: Observation) -> Observation:
        """Mask values captioned as sensitive (e.g. 'Name:'), plus text-level redaction, keeping structure."""
        by_row: dict[tuple, dict[int, str]] = {}
        for n in obs.nodes:
            if n.table is not None and n.role in CELL_ROLES:
                by_row.setdefault((n.frame_path, n.table.table, n.table.row), {})[n.table.col] = n.name
        nodes = []
        for n in obs.nodes:
            name, value = self.text(n.name), self.text(n.value) if n.value is not None else None
            if n.table is not None and n.role in CELL_ROLES:
                left = by_row[(n.frame_path, n.table.table, n.table.row)].get(n.table.col - 1)
                if left is not None and self._caption(left) in self._captions:
                    name = "[pii]"
                column = n.table.column
                if column and self._caption(column) in self._columns and self._caption(name) != self._caption(column):
                    name = "[financial]"  # amounts are regulated data; the cell is still targetable
            if n.name_source == "neighbor_label" and self._caption(n.name) in self._captions and value:
                value = "[pii]"
            table = n.table
            if table is not None and table.row_values:
                # The row context repeats every cell of the row, so it needs the same masking.
                table = table.model_copy(update={"row_values": {
                    column: "[financial]" if self._caption(column) in self._columns else self.text(text)
                    for column, text in table.row_values.items()
                }})
            nodes.append(n.model_copy(update={"name": name, "value": value, "table": table}))
        frames = [f.model_copy(update={"url": self.text(f.url)}) for f in obs.frames]
        return obs.model_copy(update={"nodes": nodes, "frames": frames, "title": self.text(obs.title)})
