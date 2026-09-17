"""Observable facts about the screen. Used for pre/postconditions, success checks and handler detectors.

All are semantic (text, roles, field values, routes) rather than pixel-based, so they survive
branding and resolution differences between tenants. String fields may reference caller inputs
with ``{{inputs.name}}``.
"""

from typing import Annotated, Literal

from pydantic import Field

from cua.schema.base import IDENT, Strict
from cua.schema.targets import Target


class InputRef(Strict):
    input: str = Field(pattern=IDENT)


class SecretRef(Strict):
    """Resolved from the secret store at the moment of typing; never logged, stored or shown to a model."""

    secret: str = Field(pattern=IDENT)


class LiteralValue(Strict):
    literal: str


Value = InputRef | SecretRef | LiteralValue


class TextVisible(Strict):
    kind: Literal["text_visible"] = "text_visible"
    text: str = Field(min_length=1)
    frame_path: tuple[str, ...] | None = None  # None = any frame
    match: Literal["contains", "exact", "regex"] = "contains"


class ElementPresent(Strict):
    kind: Literal["element_present"] = "element_present"
    target: Target


class FieldValue(Strict):
    kind: Literal["field_value"] = "field_value"
    target: Target
    equals: Value


class FrameUrl(Strict):
    """Path of a frame's URL; ``*`` matches one path segment."""

    kind: Literal["frame_url"] = "frame_url"
    frame_path: tuple[str, ...] = ()
    path: str = Field(pattern=r"^/")


class DialogPresent(Strict):
    kind: Literal["dialog_present"] = "dialog_present"
    name: str | None = None


Condition = Annotated[
    TextVisible | ElementPresent | FieldValue | FrameUrl | DialogPresent, Field(discriminator="kind")
]
