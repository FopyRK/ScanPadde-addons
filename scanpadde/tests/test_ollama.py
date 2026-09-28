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
    with pytest.raises(OllamaError):
        _validate_groups({"groups": [{"pages": [1, 3]}, {"pages": [2]}]}, [1, 2, 3])


def test_client_keeps_only_structured_groups_and_no_model_prose(monkeypatch):
    class Response:
        status = 200
        def read(self, _): return json.dumps({"response": json.dumps({"groups": [{"pages": [1, 2], "confidence": "high", "reason": "same_document", "explanation": "never stored"}]})}).encode()
    class Connection:
        class Socket:
            def __init__(self): self.timeout = None
            def settimeout(self, value): self.timeout = value
        def __init__(self, *args, **kwargs): self.sock = self.Socket()
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr("app.ollama.http.client.HTTPConnection", Connection)
    result = OllamaClient(OllamaConfig(True, "http://192.168.10.168:11434", "qwen3")).suggest_groups(pages())
    assert result.groups == [{"pages": [1, 2], "confidence": "high", "reason": "same_document"}]
    assert "never stored" not in json.dumps(result.groups)
