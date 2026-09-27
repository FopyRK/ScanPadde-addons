import pytest
from app.paths import Paths
from app.db import initialize, connect

@pytest.fixture
def env(tmp_path):
    paths = Paths(tmp_path / "share/scanpadde", tmp_path / "data")
    paths.initialize()
    db_path = paths.data / "scanpadde.db"
    initialize(db_path)
    db = connect(db_path)
    yield paths, db
    db.close()

@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    import socket
    original_connect = socket.socket.connect
    def denied(sock, address):
        # asyncio's Windows socketpair uses loopback for its internal wakeup pipe.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        raise AssertionError("external network prohibited in tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
