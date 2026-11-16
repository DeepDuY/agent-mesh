"""The public port speaks both plaintext HTTP and TLS, on one socket."""

from __future__ import annotations

import asyncio
import hashlib
import ssl

import pytest

from agent_mesh.orchestrator.tls_front import (
    TlsFront,
    cert_fingerprint,
    ensure_self_signed_cert,
    reserve_backend_port,
)


async def _ok_backend(reader, writer):
    await reader.read(4096)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
    await writer.drain()
    writer.close()


@pytest.fixture
def certs(tmp_path):
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    ensure_self_signed_cert(cert, key, ["127.0.0.1", "localhost"])
    return cert, key


def test_self_signed_cert_is_created_once(tmp_path):
    cert, key = tmp_path / "c.crt", tmp_path / "c.key"
    ensure_self_signed_cert(cert, key, ["10.0.0.1", "mesh.local"])
    first = cert.read_bytes()
    ensure_self_signed_cert(cert, key, ["other"])
    assert cert.read_bytes() == first
    assert len(cert_fingerprint(cert)) == 64


def test_front_serves_http_and_https_on_one_port(certs):
    cert, key = certs

    async def run():
        backend_port = reserve_backend_port()
        backend = await asyncio.start_server(_ok_backend, "127.0.0.1", backend_port)
        front = TlsFront("127.0.0.1", 0, backend_port, str(cert), str(key))
        await front.start()
        port = front._server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /api/healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            plain = await asyncio.wait_for(reader.read(200), timeout=5)
            writer.close()

            ctx = ssl._create_unverified_context()
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
            digest = hashlib.sha256(
                writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            ).hexdigest()
            writer.write(b"POST /api/edge/poll_for_task HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            secure = await asyncio.wait_for(reader.read(200), timeout=5)
            writer.close()
        finally:
            front.close()
            backend.close()
        return plain, secure, digest

    plain, secure, digest = asyncio.run(run())
    assert plain.startswith(b"HTTP/1.1 200")
    assert secure.startswith(b"HTTP/1.1 200")
    assert digest == cert_fingerprint(cert)
