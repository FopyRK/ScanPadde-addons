"""Test-only process fault injection; inert unless explicitly enabled."""
import os


def checkpoint(name: str) -> None:
    """Terminate only an explicitly opted-in test process at a named boundary."""
    if os.environ.get("SCANPADDE_TEST_FAULT") == f"crash:{name}":
        os._exit(75)


class SyntheticOcrError(RuntimeError):
    """Test-only, deterministic OCR failure; never enabled by normal runtime."""


def fail(name: str) -> None:
    if os.environ.get("SCANPADDE_TEST_FAULT") == f"error:{name}":
        raise SyntheticOcrError(name)
