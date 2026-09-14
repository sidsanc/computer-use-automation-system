"""Configuration: app profiles (per vendor product) and tenants (per institution)."""

from functools import cached_property
from pathlib import Path

import yaml
from pydantic import Field, HttpUrl

from cua.policy.gate import Policy, PolicyNarrowing, ProductPolicy
from cua.schema.base import IDENT, Strict

CONFIG_DIR = Path("config")


class AppProfile(Strict):
    product: str = Field(pattern=IDENT)
    name: str
    policy: ProductPolicy
    sensitive_captions: tuple[str, ...] = ()


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
