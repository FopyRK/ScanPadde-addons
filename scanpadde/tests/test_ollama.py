import json
from unittest.mock import patch

import pytest

from app.config import OllamaConfig
from app.ollama import OllamaClient, OllamaError, _validate_groups


def pages():
    return [
        {"page_number": 1, "text": "Supplier GmbH Invoice A1 Page 1 of 2", "features": {"blankness": "content", "supplier_candidates": [], "invoice_number_candidates": [], "document_number_candidates": [], "page_number_candidates": [], "probable_document_type": []}},
        {"page_number": 2, "text": "Supplier GmbH Invoice A1 Page 2 of 2", "features": {"blankness": "content", "supplier_candidates": [], "invoice_number_candidates": [], "document_number_candidates": [], "page_number_candidates": [], "probable_document_type": []}},
    ]


def test_model_groups_must_partition_contiguous_ordered_pages():
    assert _validate_groups({"groups": [{"pages": [1, 2], "confidence": "high", "reason": "same_document"}]}, [1, 2])[0]["pages"] == [1, 2]
    with pytest.raises(OllamaError):
        _validate_groups({"groups": [{"pages": [1]}, {"pages": [3]}]}, [1, 2, 3])


def test_compact_start_pages_expand_to_contiguous_groups():
    assert _validate_groups({"starts": [1, 3]}, [1, 2, 3, 4]) == [
        {"pages": [1, 2], "confidence": "low", "reason": "uncertain"},
        {"pages": [3, 4], "confidence": "low", "reason": "uncertain"},
    ]
    with pytest.raises(OllamaError):
        _validate_groups({"groups": [{"pages": [1, 3]}, {"pages": [2]}]}, [1, 2, 3])


def test_client_keeps_only_structured_groups_and_no_model_prose(monkeypatch):
    class Response:
        status = 200
        def read(self, _): return json.dumps({"response": json.dumps({"starts": [1]})}).encode()
    captured = {}
    class Connection:
        class Socket:
            def __init__(self): self.timeout = None
            def settimeout(self, value): self.timeout = value
        def __init__(self, *args, **kwargs): self.sock = self.Socket()
        def request(self, *args, **kwargs): captured.update(body=json.loads(kwargs["body"]))
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr("app.ollama.http.client.HTTPConnection", Connection)
    result = OllamaClient(OllamaConfig(True, "http://192.168.10.168:11434", "qwen3")).suggest_groups(pages())
    assert result.groups == [{"pages": [1, 2], "confidence": "low", "reason": "uncertain"}]
    assert captured["body"]["options"]["num_predict"] == 64


def test_client_chunks_large_batches(monkeypatch):
    calls = []

    def suggest_chunk(_, chunk):
        calls.append([page["page_number"] for page in chunk])
        return [{"pages": calls[-1], "confidence": "low", "reason": "uncertain"}]

    monkeypatch.setattr(OllamaClient, "_suggest_chunk", suggest_chunk)
    many_pages = [{"page_number": number, "features": pages()[0]["features"]}
                  for number in range(1, 15)]
    result = OllamaClient(OllamaConfig(True, "http://local", "model")).suggest_groups(many_pages)
    assert len(calls) == 2
    assert [group["pages"] for group in result.groups] == [list(range(1, 13)), [13, 14]]
