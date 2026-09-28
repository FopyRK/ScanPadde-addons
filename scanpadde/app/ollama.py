"""Narrow, local-only Ollama client for non-authoritative page grouping hints."""
import hashlib
import http.client
import json
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import OllamaConfig


class OllamaError(RuntimeError):
    pass


@dataclass(frozen=True)
class OllamaSuggestion:
    groups: list[dict]
    input_digest: str


def _page_summary(page: dict) -> dict:
    features = page["features"]
    def values(key):
        # A short best candidate is enough for a grouping hint from a small
        # local model. The keys are intentionally compact because they repeat
        # for every page of a scanner batch.
        return [item.get("value", "")[:32] for item in features.get(key, [])[:1]]
    return {
        "p": page["page_number"],
        "s": values("supplier_candidates"),
        "n": values("invoice_number_candidates") + values("document_number_candidates"),
        "c": values("page_number_candidates"),
        "t": values("probable_document_type"),
        "b": features.get("blankness"),
    }


def _input_digest(pages: list[dict], model: str) -> str:
    material = json.dumps({"model": model, "pages": [_page_summary(page) for page in pages]},
                          ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _validate_groups(value: object, page_numbers: list[int]) -> list[dict]:
    if isinstance(value, dict) and isinstance(value.get("starts"), list):
        starts = value["starts"]
        if (not starts or any(type(page) is not int for page in starts)
                or starts != sorted(set(starts)) or starts[0] != page_numbers[0]
                or any(page not in page_numbers for page in starts)):
            raise OllamaError("invalid_model_response")
        positions = [page_numbers.index(page) for page in starts] + [len(page_numbers)]
        return [{"pages": page_numbers[positions[index]:positions[index + 1]],
                 "confidence": "low", "reason": "uncertain"}
                for index in range(len(starts))]
    if not isinstance(value, dict) or not isinstance(value.get("groups"), list):
        raise OllamaError("invalid_model_response")
    groups, seen, expected = [], [], page_numbers
    for item in value["groups"]:
        if not isinstance(item, dict) or not isinstance(item.get("pages"), list):
            raise OllamaError("invalid_model_response")
        pages = item["pages"]
        if not pages or any(type(page) is not int for page in pages) or pages != sorted(pages):
            raise OllamaError("invalid_model_response")
        # Document scanning is ordered.  Never let a language model create a
        # non-contiguous group merely because a supplier name appears again.
        if pages != list(range(pages[0], pages[-1] + 1)):
            raise OllamaError("invalid_model_response")
        confidence = item.get("confidence")
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        reason = item.get("reason")
        if reason not in {"same_document", "new_document", "blank_reverse", "uncertain"}:
            reason = "uncertain"
        groups.append({"pages": pages, "confidence": confidence, "reason": reason})
        seen.extend(pages)
    if seen != expected:
        raise OllamaError("invalid_model_response")
    return groups


class OllamaClient:
    def __init__(self, config: OllamaConfig):
        if not config.enabled or not config.url or not config.model:
            raise OllamaError("ollama_not_configured")
        self.config = config

    def suggest_groups(self, pages: list[dict]) -> OllamaSuggestion:
        if not pages:
            raise OllamaError("no_ocr_pages")
        # Small local models can answer compact page-start prompts reliably,
        # but a whole scanner batch can still make one inference exceed the
        # bounded response time.  Keep every request independently useful and
        # join the ordered, contiguous partitions afterwards.
        chunk_size = 12
        groups: list[dict] = []
        for start in range(0, len(pages), chunk_size):
            groups.extend(self._suggest_chunk(pages[start:start + chunk_size]))
        return OllamaSuggestion(groups=groups, input_digest=_input_digest(pages, self.config.model))

    def _suggest_chunk(self, pages: list[dict]) -> list[dict]:
        summaries = [_page_summary(page) for page in pages]
        prompt = (
            "Return JSON only: {\"starts\":[1,3]}. starts lists every first page of a contiguous document, "
            "including the first listed page. Add a start when supplier, document number, or page counter changes; "
            "a blank reverse is normally not a start. Do not return text or explanations.\n"
            + json.dumps({"pages": summaries}, ensure_ascii=False)
        )
        target = urlparse(self.config.url)
        connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        try:
            connection = connection_type(target.hostname, target.port, timeout=self.config.connect_timeout)
            body = json.dumps({"model": self.config.model, "prompt": prompt, "stream": False,
                               "format": "json", "options": {"temperature": 0, "num_predict": 64}}).encode("utf-8")
            connection.request("POST", "/api/generate", body=body,
                               headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
            # HTTPConnection's constructor timeout also governs response reads.
            # A local model can legitimately need longer than the short connect
            # timeout, so preserve the fast connection failure while allowing the
            # configured, bounded inference time for the response.
            if connection.sock is not None:
                connection.sock.settimeout(self.config.read_timeout)
            response = connection.getresponse()
            raw = response.read(200000)
            if response.status != 200:
                raise OllamaError("ollama_unavailable")
            payload = json.loads(raw.decode("utf-8"))
            parsed = json.loads(payload.get("response", ""))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise OllamaError("ollama_unavailable") from exc
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass
        return _validate_groups(parsed, [page["page_number"] for page in pages])
