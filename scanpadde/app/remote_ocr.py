"""Small, blocking remote OCR client with deliberately narrow retry semantics."""
import hashlib
import json
import mimetypes
import socket
import ssl
import uuid
import http.client
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = "1.0"
ROTATION_POLICY_VERSION = "weak-confidence-v1"


class RemoteOcrError(Exception):
    retryable = False


class RemoteUnavailable(RemoteOcrError):
    retryable = True


class RemoteRejected(RemoteOcrError):
    pass


def ocr_key(image_sha256, engine, engine_version, languages, preprocessing_version, rotation_policy_version):
    fields = (image_sha256, engine, engine_version, languages, preprocessing_version, rotation_policy_version)
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


def _multipart(fields, image: Path):
    boundary = "----scanpadde-" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    mime = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"page.png\"\r\nContent-Type: {mime}\r\n\r\n".encode())
    body.extend(image.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return bytes(body), boundary


@dataclass
class RemoteOcrClient:
    url: str
    token: str
    ca_path: str
    connect_timeout: float = 5.0
    read_timeout: float = 90.0

    def _request(self, path, data=None, headers=None):
        target = urlsplit(self.url)
        if target.scheme != "https" or not target.hostname:
            raise RemoteRejected("invalid_worker_url")
        try:
            # Do not add the platform trust store: this endpoint must chain to the
            # explicitly configured ScanPadde internal CA only.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=self.ca_path)
        except (OSError, ssl.SSLError) as exc:
            raise RemoteRejected("invalid_worker_ca") from exc
        # PROTOCOL_TLS_CLIENT enables CERT_REQUIRED and hostname/SAN checking.
        connection = http.client.HTTPSConnection(target.hostname, target.port, timeout=self.connect_timeout,
                                                 context=context)
        try:
            connection.request("POST" if data is not None else "GET", (target.path.rstrip("/") + path), body=data,
                               headers={"Authorization": "Bearer " + self.token, **(headers or {})})
            # `request` completes the TCP connect under connect_timeout; OCR response headers/body may take longer.
            if connection.sock:
                connection.sock.settimeout(self.read_timeout)
            response = connection.getresponse()
            if response.fp and response.fp.raw and response.fp.raw._sock:
                response.fp.raw._sock.settimeout(self.read_timeout)
            payload = response.read()
            if response.status in (502, 503, 504):
                raise RemoteUnavailable(f"http_{response.status}")
            if response.status < 200 or response.status >= 300:
                raise RemoteRejected(f"http_{response.status}")
            return json.loads(payload.decode("utf-8"))
        except (OSError, http.client.HTTPException, socket.timeout, TimeoutError, ValueError) as exc:
            raise RemoteUnavailable(type(exc).__name__) from exc
        finally:
            connection.close()

    def version(self):
        result = self._request("/version")
        if result.get("schema_version") != SCHEMA_VERSION or result.get("engine") != "tesseract" or result.get("languages") != "deu+eng" or not isinstance(result.get("engine_version"), str):
            raise RemoteRejected("unsupported_worker_version")
        return result

    def recognize(self, job_key, image, image_sha256, languages, preprocessing_version):
        worker = self.version()
        expected_key = ocr_key(image_sha256, worker["engine"], worker["engine_version"], languages,
                               preprocessing_version, ROTATION_POLICY_VERSION)
        data, boundary = _multipart({"job_key": job_key, "image_sha256": image_sha256,
                                    "languages": languages, "preprocessing_version": preprocessing_version,
                                    "rotation_policy_version": ROTATION_POLICY_VERSION}, image)
        result = self._request("/v1/ocr", data, {"Content-Type": "multipart/form-data; boundary=" + boundary})
        validate_response(result, job_key, image_sha256, expected_key, languages, preprocessing_version)
        return result


def validate_response(result, job_key, image_sha256, expected_key, languages, preprocessing_version):
    required = {"schema_version", "job_key", "ocr_key", "image_sha256", "engine", "engine_version", "languages", "rotation", "text", "confidence", "words", "cache_hit", "duration_ms"}
    if not isinstance(result, dict) or not required.issubset(result) or result["schema_version"] != SCHEMA_VERSION:
        raise RemoteRejected("malformed_or_unsupported_response")
    if result["job_key"] != job_key or result["image_sha256"] != image_sha256 or result["ocr_key"] != expected_key:
        raise RemoteRejected("remote_response_mismatch")
    if result["engine"] != "tesseract" or result["languages"] != languages or not isinstance(result["engine_version"], str):
        raise RemoteRejected("remote_response_configuration_mismatch")
    if result["rotation"] not in (0, 90, 180, 270) or not isinstance(result["text"], str) or result["confidence"] is not None and not isinstance(result["confidence"], (int, float)):
        raise RemoteRejected("remote_response_invalid_result")
    if not isinstance(result["words"], list) or not isinstance(result["duration_ms"], int) or result["duration_ms"] < 0:
        raise RemoteRejected("remote_response_invalid_result")
    for word in result["words"]:
        if not isinstance(word, dict) or set(word) != {"text", "confidence", "x", "y", "w", "h"} or not isinstance(word["text"], str) or not isinstance(word["confidence"], (int, float)) or any(not isinstance(word[k], int) or word[k] < 0 for k in ("x", "y", "w", "h")):
            raise RemoteRejected("remote_response_invalid_words")
