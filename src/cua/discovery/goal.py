from pathlib import Path

import yaml
from pydantic import Field

from cua.schema.artifact import InputSpec, OutputSpec
from cua.schema.base import Strict


class GoalSpec(Strict):
    """What the calling side wants discovered: the goal in natural language plus the contract.

    Values used during the discovery run are supplied separately and never stored here.
    """

    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    title: str
    goal: str
    entry: str = Field(default="/teller", pattern=r"^/")
    inputs: dict[str, InputSpec] = {}
    outputs: dict[str, OutputSpec] = {}

    @classmethod
    def load(cls, path: Path) -> "GoalSpec":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
