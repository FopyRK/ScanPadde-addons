"""Runtime-only OCR configuration; secrets never enter the database or status API."""
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OcrConfig:
    backend: str = "disabled"
    worker_url: str | None = None
    worker_ca: str | None = None
    worker_token: str | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 90.0


def _ca_from_app_config(value: object, config_dir: Path) -> str | None:
    """Resolve a CA only within the Supervisor-provided app config mount."""
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    root = config_dir.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    return str(resolved)


def load_ocr_config(data_dir: Path, config_dir: Path = Path("/config")) -> OcrConfig:
    """Read HA options at runtime without exposing option values in process state."""
    values = {}
    options = data_dir / "options.json"
    if options.exists():
        try:
            values = json.loads(options.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
    backend = os.getenv("SCANPADDE_OCR_BACKEND", values.get("ocr_backend", "disabled"))
    url = os.getenv("SCANPADDE_OCR_WORKER_URL", values.get("ocr_worker_url"))
    # The CA option is deliberately relative to the official addon_config mount.
    # Environment overrides remain limited to development; production run.sh sets none.
    ca_value = os.getenv("SCANPADDE_OCR_WORKER_CA", values.get("ocr_worker_ca"))
    ca = _ca_from_app_config(ca_value, config_dir)
    token = os.getenv("SCANPADDE_OCR_WORKER_TOKEN", values.get("ocr_worker_token"))
    if backend not in {"disabled", "local", "remote"}:
        backend = "disabled"
    if backend == "remote" and (not isinstance(url, str) or not url.startswith("https://")
                                or ca is None
                                or not isinstance(token, str) or not token):
        backend = "disabled"
    return OcrConfig(backend=backend, worker_url=url.rstrip("/") if isinstance(url, str) else None,
                     worker_ca=ca if isinstance(ca, str) else None,
                     worker_token=token if isinstance(token, str) else None)
