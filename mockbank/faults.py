import threading

ONE_SHOT = {"session_expired", "maintenance_notice", "unknown_dialog"}
STICKY = {"server_error", "slow_load", "slow_commit"}


class FaultState:
    """Runtime faults toggled by tests and the `cua faults` CLI.

    One-shot faults fire on the next eligible request and then clear themselves,
    which mirrors transient conditions a replay should recover from exactly once.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._one_shot: set[str] = set()
        self.server_error = False
        self.slow_load_seconds = 0.0
        # The write lands, but the response is slow enough that the caller stops waiting:
        # the ambiguous write that matters in a core banking system.
        self.slow_commit_seconds = 0.0

    def set(self, name: str, value: str | None = None) -> None:
        with self._lock:
            if name in ONE_SHOT:
                self._one_shot.add(name)
            elif name == "server_error":
                self.server_error = True
            elif name == "slow_load":
                self.slow_load_seconds = float(value or 4)
            elif name == "slow_commit":
                self.slow_commit_seconds = float(value or 14)
            else:
                raise ValueError(f"unknown fault: {name}")

    def clear(self) -> None:
        with self._lock:
            self._one_shot.clear()
            self.server_error = False
            self.slow_load_seconds = 0.0
            self.slow_commit_seconds = 0.0

    def consume(self, name: str) -> bool:
        with self._lock:
            if name in self._one_shot:
                self._one_shot.remove(name)
                return True
            return False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pending_one_shot": sorted(self._one_shot),
                "server_error": self.server_error,
                "slow_load_seconds": self.slow_load_seconds,
                "slow_commit_seconds": self.slow_commit_seconds,
            }
