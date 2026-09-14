"""Configuration: app profiles (per vendor product) and tenants (per institution)."""

from functools import cached_property
from pathlib import Path

import yaml
from pydantic import Field, HttpUrl

from cua.policy.gate import Policy, PolicyNarrowing, ProductPolicy
from cua.schema.artifact import Handler, Step
from cua.schema.base import IDENT, Strict
from cua.schema.conditions import Condition

CONFIG_DIR = Path("config")


class LoginFlow(Strict):
    """Sign-on is app-level, run by the harness: credentials never enter a model's context or an artifact."""

    path: str = Field(pattern=r"^/")
    secrets: tuple[str, ...]
    steps: tuple[Step, ...] = Field(min_length=1)
    success: tuple[Condition, ...] = Field(min_length=1)


class Fingerprint(Strict):
    """How to read the product version off the screen, to check a capability's version range."""

    text_regex: str
    frame_path: tuple[str, ...] | None = None


class AppProfile(Strict):
    product: str = Field(pattern=IDENT)
    name: str
    policy: ProductPolicy
    sensitive_captions: tuple[str, ...] = ()
    login: LoginFlow
    fingerprint: Fingerprint | None = None
    handlers: tuple[Handler, ...] = Field(
        default=(), description="Known runtime states shared by every capability of this product."
    )


class TenantConfig(Strict):
    tenant_id: str = Field(pattern=IDENT)
    display_name: str
    app: str = Field(pattern=IDENT)
    base_url: HttpUrl
    policy: PolicyNarrowing = PolicyNarrowing()
    overlay: str | None = None

    @property
    def base(self) -> str:
        return str(self.base_url).rstrip("/")


class Workspace:
    """Resolves tenants to their app profile and effective policy from a config directory."""

    def __init__(self, root: Path = CONFIG_DIR) -> None:
        self.root = root

    def tenant(self, tenant_id: str) -> TenantConfig:
        return TenantConfig.model_validate(_load(self.root / "tenants" / f"{tenant_id}.yaml"))

    def app(self, product: str) -> AppProfile:
        return AppProfile.model_validate(_load(self.root / "apps" / f"{product}.yaml"))

    def policy_for(self, tenant: TenantConfig) -> Policy:
        return self.app(tenant.app).policy.for_tenant(tenant.base, tenant.policy)

    @cached_property
    def tenant_ids(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "tenants").glob("*.yaml"))


def _load(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
