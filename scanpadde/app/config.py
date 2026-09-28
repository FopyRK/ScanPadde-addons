"""Runtime-only OCR configuration; secrets never enter the database or status API."""
import json
import os
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class OcrConfig:
    backend: str = "disabled"
    worker_url: str | None = None
    worker_ca: str | None = None
    worker_token: str | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 90.0


@dataclass(frozen=True)
class OllamaConfig:
    """Optional local-only analysis endpoint.

    The add-on may only contact a loopback or RFC1918 address on Ollama's
    dedicated port.  This keeps the feature from becoming a general outbound
    HTTP client through a Home Assistant option.
    """
    enabled: bool = False
    url: str | None = None
    model: str | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 180.0


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


def _private_ollama_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    target = urlparse(value)
    if target.scheme not in {"http", "https"} or target.username or target.password or target.path not in {"", "/"}:
        return None
    try:
        host = ipaddress.ip_address(target.hostname or "")
    except ValueError:
        return None
    if not (host.is_private or host.is_loopback) or target.port not in {None, 11434}:
        return None
    return f"{target.scheme}://{target.hostname}:{target.port or 11434}"


def load_ollama_config(data_dir: Path) -> OllamaConfig:
    """Read optional local Ollama settings without storing OCR data in config."""
    values = {}
    options = data_dir / "options.json"
    if options.exists():
        try:
            values = json.loads(options.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
    enabled = values.get("ollama_enabled", False) is True
    url = _private_ollama_url(values.get("ollama_url"))
    model = values.get("ollama_model")
    if not isinstance(model, str) or not model.strip() or len(model) > 100:
        model = None
    if not (enabled and url and model):
        return OllamaConfig()
    return OllamaConfig(enabled=True, url=url, model=model.strip())
