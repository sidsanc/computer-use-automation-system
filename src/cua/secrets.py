import os
from typing import Protocol


class MissingSecretError(KeyError):
    pass


class SecretStore(Protocol):
    def get(self, name: str) -> str: ...


class EnvSecretStore:
    """Reads CUA_SECRET_<NAME>. A vault-backed store would implement the same one-method protocol."""

    def get(self, name: str) -> str:
        key = f"CUA_SECRET_{name.upper()}"
        value = os.environ.get(key)
        if not value:
            raise MissingSecretError(f"secret '{name}' is not configured (set {key})")
        return value


class DictSecretStore:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        if name not in self._values:
            raise MissingSecretError(f"secret '{name}' is not configured")
        return self._values[name]
