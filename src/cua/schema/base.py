import re

from pydantic import BaseModel, ConfigDict

INPUT_TEMPLATE_RE = re.compile(r"\{\{\s*inputs\.([a-z][a-z0-9_]*)\s*\}\}")
IDENT = r"^[a-z][a-z0-9_]*$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def render_template(text: str, inputs: dict[str, str]) -> str:
    return INPUT_TEMPLATE_RE.sub(lambda m: inputs[m.group(1)], text)
