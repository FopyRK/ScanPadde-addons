import hashlib
import json
import ssl
from pathlib import Path
from unittest.mock import patch
import pytest
from PIL import Image
from app.remote_ocr import RemoteOcrClient, RemoteRejected, RemoteUnavailable, ocr_key, validate_response
from app.config import OcrConfig, load_ocr_config
from app import jobs
from app.ingestion import execute
from test_phase2_ocr import page_with_image


def valid_response(key, digest):
    return {"schema_version": "1.0", "job_key": "job", "ocr_key": key, "image_sha256": digest, "engine": "tesseract",
            "engine_version": "tesseract 5", "languages": "deu+eng", "rotation": 0, "text": "Hello",
            "confidence": 95.0, "words": [{"text": "Hello", "confidence": 95.0, "x": 1, "y": 2, "w": 3, "h": 4}], "cache_hit": False, "duration_ms": 1}


def test_validate_response_rejects_mismatch_and_malformed():
    digest = "a" * 64; key = ocr_key(digest, "tesseract", "tesseract 5", "deu+eng", "none-v1", "weak-confidence-v1")
    validate_response(valid_response(key, digest), "job", digest, key, "deu+eng", "none-v1")
    broken = valid_response(key, digest); broken["image_sha256"] = "b" * 64
    with pytest.raises(RemoteRejected): validate_response(broken, "job", digest, key, "deu+eng", "none-v1")
    with pytest.raises(RemoteRejected): validate_response({}, "job", digest, key, "deu+eng", "none-v1")


def test_client_uses_version_then_validates_response(tmp_path):
    image = tmp_path / "page.png"; Image.new("RGB", (4, 4), "white").save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    key = ocr_key(digest, "tesseract", "tesseract 5", "deu+eng", "none-v1", "weak-confidence-v1")
    client = RemoteOcrClient("https://worker", "secret", str(tmp_path / "ca.crt"))
    with patch.object(client, "_request", side_effect=[{"schema_version": "1.0", "engine": "tesseract", "engine_version": "tesseract 5", "languages": "deu+eng"}, valid_response(key, digest)]):
        assert client.recognize("job", image, digest, "deu+eng", "none-v1")["text"] == "Hello"


def test_retryable_503_and_auth_error():
    client = RemoteOcrClient("https://worker", "secret", "/ca.crt")
    with patch.object(client, "_request", side_effect=RemoteUnavailable("http_503")):
        with pytest.raises(RemoteUnavailable): client.version()
    with patch.object(client, "_request", side_effect=RemoteRejected("http_401")):
        with pytest.raises(RemoteRejected): client.version()


def test_remote_offline_never_calls_local_engine(env):
    paths, db = env
    page_id = page_with_image(paths, db)
    jobs.enqueue(db, "remote-job", "ocr_page", page_id, {"page_id": page_id})
    config = OcrConfig(backend="remote", worker_url="https://worker", worker_ca="/ca.crt", worker_token="secret")
    with patch("app.ingestion.RemoteOcrClient.version", side_effect=RemoteUnavailable("offline")), patch("app.ingestion.recognize") as local:
        execute(db, paths, jobs.claim(db), config)
    assert not local.called
    assert db.execute("SELECT state FROM jobs WHERE job_key='remote-job'").fetchone()[0] == "pending"
    assert db.execute("SELECT status FROM pages WHERE id=?", (page_id,)).fetchone()[0] == "pending_ocr"


def test_https_client_uses_the_explicit_ca_and_standard_validation(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"; ca.write_text("placeholder")
    observed = {}

    class Context:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True
        def load_verify_locations(self, *, cafile):
            observed["ca"] = cafile

    class Connection:
        def __init__(self, host, port, timeout, context):
            observed.update(host=host, port=port, timeout=timeout, context=context)
        def close(self):
            pass

    context = Context()
    monkeypatch.setattr("app.remote_ocr.ssl.SSLContext", lambda protocol: context)
    monkeypatch.setattr("app.remote_ocr.http.client.HTTPSConnection", Connection)
    client = RemoteOcrClient("https://192.168.10.168:18080", "secret", str(ca))
    with pytest.raises(AttributeError):
        client._request("/health")
    assert observed["ca"] == str(ca)
    assert observed["host"] == "192.168.10.168"
    assert observed["port"] == 18080
    assert observed["context"].verify_mode == ssl.CERT_REQUIRED
    assert observed["context"].check_hostname is True


def test_client_rejects_plain_http_without_connecting():
    client = RemoteOcrClient("http://192.168.10.168:18080", "secret", "/ca.crt")
    with pytest.raises(RemoteRejected, match="invalid_worker_url"):
        client._request("/health")


def test_ocr_configuration_defaults_to_disabled(tmp_path):
    assert load_ocr_config(tmp_path, tmp_path / "config") == OcrConfig()


def test_remote_configuration_requires_https_token_and_explicit_ca(tmp_path):
    config_dir = tmp_path / "config"; config_dir.mkdir()
    ca = config_dir / "scanpadde-ocr-ca.crt"; ca.write_text("public-ca")
    options = {"ocr_backend": "remote", "ocr_worker_url": "https://192.168.10.168:18080",
               "ocr_worker_ca": "scanpadde-ocr-ca.crt", "ocr_worker_token": "secret"}
    (tmp_path / "options.json").write_text(json.dumps(options))
    config = load_ocr_config(tmp_path, config_dir)
    assert config.backend == "remote"
    assert config.worker_ca == str(ca)
    options["ocr_worker_url"] = "http://192.168.10.168:18080"
    (tmp_path / "options.json").write_text(json.dumps(options))
    assert load_ocr_config(tmp_path, config_dir).backend == "disabled"


@pytest.mark.parametrize("missing", ["ocr_worker_url", "ocr_worker_ca", "ocr_worker_token"])
def test_remote_configuration_disables_when_required_value_is_missing(tmp_path, missing):
    config_dir = tmp_path / "config"; config_dir.mkdir()
    (config_dir / "scanpadde-ocr-ca.crt").write_text("public-ca")
    options = {"ocr_backend": "remote", "ocr_worker_url": "https://worker.example",
               "ocr_worker_ca": "scanpadde-ocr-ca.crt", "ocr_worker_token": "secret"}
    options.pop(missing)
    (tmp_path / "options.json").write_text(json.dumps(options))
    assert load_ocr_config(tmp_path, config_dir).backend == "disabled"


def test_remote_configuration_rejects_ca_outside_app_config(tmp_path):
    config_dir = tmp_path / "config"; config_dir.mkdir()
    outside_ca = tmp_path / "outside-ca.crt"; outside_ca.write_text("public-ca")
    options = {"ocr_backend": "remote", "ocr_worker_url": "https://worker.example",
               "ocr_worker_ca": str(outside_ca), "ocr_worker_token": "secret"}
    (tmp_path / "options.json").write_text(json.dumps(options))
    assert load_ocr_config(tmp_path, config_dir).backend == "disabled"
