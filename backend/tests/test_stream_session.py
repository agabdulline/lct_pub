"""Живая лента не держит транзакцию БД, пока открыт стрим (инцидент на проде 26.09).

Сессия зависимости с `yield` живёт до конца ответа, а SSE открыт часами: после первого `SELECT runs` соединение
висело «idle in transaction», и `ALTER TABLE runs` в миграции ждал блокировку, а за ним вставали все запросы к `runs`.
Стрим бесконечный: api — настоящий uvicorn в потоке, клиент — минимальный GET по сокету, как у браузера и curl.
"""

import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

IDLE_IN_TRANSACTION = """
SELECT count(*) FROM pg_stat_activity
WHERE datname = current_database() AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()
"""


def idle_in_transaction(db_url) -> int:
    engine = create_engine(db_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(text(IDLE_IN_TRANSACTION)).scalar_one()
    finally:
        engine.dispose()


@pytest.fixture
def server(seeded):
    from app.main import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield port
    srv.should_exit = True
    thread.join(timeout=10)


def open_stream(port: int, query: str) -> socket.socket:
    """Минимальный GET, как у curl и браузера: поток остаётся открытым, пока сокет не закрыт."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(f"GET /api/v1/stream{query} HTTP/1.1\r\nHost: test\r\nAccept: text/event-stream\r\n\r\n".encode())
    received = b""
    while b": connected" not in received:
        chunk = sock.recv(4096)
        assert chunk, f"стрим закрылся: {received!r}"
        received += chunk
    assert received.startswith(b"HTTP/1.1 200"), received[:80]
    return sock


def test_stream_does_not_hold_transaction(server, seeded):
    for query in ("?run=0", ""):  # база и «последний прогон» (без прогонов — тоже база)
        sock = open_stream(server, query)
        try:
            time.sleep(0.5)
            assert idle_in_transaction(seeded) == 0, "открытый стрим держит транзакцию БД"
        finally:
            sock.close()
