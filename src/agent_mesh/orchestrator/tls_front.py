"""Single-port TLS front for the orchestrator.

The public port accepts both plaintext HTTP and TLS on the same socket so old
probes (which only speak HTTP and cannot be upgraded while offline) keep
working, while new probes connect with HTTPS. The first byte decides:

* ``0x16`` is a TLS ClientHello -> terminate TLS in-process.
* anything else is plaintext HTTP -> forward as-is.

Both are relayed as a raw byte stream to a plaintext uvicorn bound to
``127.0.0.1`` on an ephemeral port, so SSE and artifact uploads pass through
untouched. The real client address is injected as ``X-Forwarded-For``.

TLS is terminated with ``ssl.MemoryBIO`` rather than ``SSLContext.wrap_socket``:
asyncio owns the accepted socket, so detaching it to hand to OpenSSL races the
transport and drops the ClientHello byte already consumed by the protocol sniff.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from pathlib import Path

logger = logging.getLogger(__name__)

_TLS_HANDSHAKE = 0x16
_PEEK_TIMEOUT_S = 5.0
_HANDSHAKE_TIMEOUT_S = 10.0


def cert_fingerprint(cert_path: str | Path) -> str:
    """SHA-256 fingerprint of a PEM certificate, lowercase hex without colons."""
    import hashlib

    pem = Path(cert_path).read_bytes()
    der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
    return hashlib.sha256(der).hexdigest()


def ensure_self_signed_cert(
    cert_path: str | Path,
    key_path: str | Path,
    hosts: list[str],
) -> None:
    """Create a self-signed cert if one does not already exist.

    ``hosts`` are written as SAN entries (IP addresses detected automatically)
    so probes can pin the certificate rather than trust a public CA.
    """
    cert = Path(cert_path)
    key = Path(key_path)
    if cert.is_file() and key.is_file():
        return
    cert.parent.mkdir(parents=True, exist_ok=True)
    _generate_self_signed(cert, key, hosts)
    logger.info("generated self-signed TLS certificate at %s", cert)


def _generate_self_signed(cert: Path, key: Path, hosts: list[str]) -> None:
    """Write a self-signed certificate using the openssl CLI.

    The container image ships neither a crypto library nor a CA, and probes pin
    the fingerprint rather than trusting a chain, so a self-signed cert is
    enough. SAN entries cover both DNS names and IP addresses.
    """
    import ipaddress
    import subprocess
    import tempfile

    names = hosts or ["agent-mesh"]
    san = []
    for host in names:
        try:
            ipaddress.ip_address(host)
            san.append(f"IP:{host}")
        except ValueError:
            san.append(f"DNS:{host}")
    config = (
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
        "[dn]\nCN=agent-mesh\n[ext]\nsubjectAltName=" + ",".join(san) + "\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as fh:
        fh.write(config)
        cfg_path = fh.name
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(cert),
                "-days", "3650", "-nodes",
                "-config", cfg_path, "-extensions", "ext",
            ],
            check=True, capture_output=True,
        )
    finally:
        Path(cfg_path).unlink(missing_ok=True)


def _server_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def _inject_forwarded_for(header: bytes, peer: str) -> bytes:
    """Insert ``X-Forwarded-For`` into the first block of an HTTP request."""
    sep = header.find(b"\r\n")
    if sep < 0:
        return header
    line = f"X-Forwarded-For: {peer}\r\n".encode("ascii", "replace")
    return header[: sep + 2] + line + header[sep + 2 :]


async def _copy(reader: asyncio.StreamReader, write) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            await write(data)
    except (ConnectionError, asyncio.CancelledError, ssl.SSLError):
        pass


class _MemoryTls:
    """TLS terminator over an asyncio stream pair, using ``ssl.MemoryBIO``."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        incoming: bytes,
        ctx: ssl.SSLContext,
    ):
        self._reader = reader
        self._writer = writer
        self._in_bio = ssl.MemoryBIO()
        self._out_bio = ssl.MemoryBIO()
        if incoming:
            self._in_bio.write(incoming)
        self._sslobj = ctx.wrap_bio(self._in_bio, self._out_bio, server_side=True)

    async def handshake(self) -> None:
        while True:
            try:
                self._sslobj.do_handshake()
                break
            except ssl.SSLWantReadError:
                await self._flush()
                data = await asyncio.wait_for(
                    self._reader.read(16384), timeout=_HANDSHAKE_TIMEOUT_S
                )
                if not data:
                    raise ConnectionError("peer closed during TLS handshake")
                self._in_bio.write(data)
        await self._flush()

    async def _flush(self) -> None:
        data = self._out_bio.read()
        if data:
            self._writer.write(data)
            await self._writer.drain()

    def plaintext_reader(self) -> asyncio.StreamReader:
        reader: asyncio.StreamReader = asyncio.StreamReader()

        async def feed() -> None:
            try:
                while True:
                    try:
                        chunk = self._sslobj.read(16384)
                    except ssl.SSLWantReadError:
                        data = await self._reader.read(16384)
                        if not data:
                            break
                        self._in_bio.write(data)
                        continue
                    if not chunk:
                        break
                    reader.feed_data(chunk)
            except (ConnectionError, ssl.SSLError):
                pass
            reader.feed_eof()

        asyncio.get_running_loop().create_task(feed())
        return reader

    async def write_plaintext(self, data: bytes) -> None:
        self._sslobj.write(data)
        await self._flush()

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass


class TlsFront:
    """Accept both HTTP and TLS on one port and relay to a plaintext backend."""

    def __init__(
        self, host: str, port: int, backend_port: int, cert_path: str, key_path: str
    ):
        self.host = host
        self.port = port
        self.backend_port = backend_port
        self._ctx = _server_ssl_context(cert_path, key_path)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info(
            "TLS front listening on %s -> 127.0.0.1:%d", sockets, self.backend_port
        )

    async def serve(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_host = peer[0] if peer else ""
        try:
            first = await asyncio.wait_for(reader.read(1), timeout=_PEEK_TIMEOUT_S)
        except (asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        if not first:
            writer.close()
            return
        if first[0] == _TLS_HANDSHAKE:
            await self._handle_tls(reader, writer, first, peer_host)
        else:
            await self._handle_plain(reader, writer, first, peer_host)

    async def _handle_plain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        first: bytes,
        peer_host: str,
    ) -> None:
        try:
            header = first + await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=15
            )
            backend_reader, backend_writer = await asyncio.open_connection(
                "127.0.0.1", self.backend_port
            )
        except (asyncio.TimeoutError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        backend_writer.write(_inject_forwarded_for(header, peer_host))
        await asyncio.gather(
            _copy(reader, _socket_write(backend_writer)),
            _copy(backend_reader, _socket_write(writer)),
        )
        writer.close()

    async def _handle_tls(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        first: bytes,
        peer_host: str,
    ) -> None:
        tls = _MemoryTls(reader, writer, first, self._ctx)
        try:
            await tls.handshake()
            plain = tls.plaintext_reader()
            header = await asyncio.wait_for(plain.readuntil(b"\r\n\r\n"), timeout=15)
            backend_reader, backend_writer = await asyncio.open_connection(
                "127.0.0.1", self.backend_port
            )
        except (
            asyncio.TimeoutError,
            asyncio.LimitOverrunError,
            ConnectionError,
            ssl.SSLError,
        ) as exc:
            logger.debug("TLS connection from %s dropped: %s", peer_host, exc)
            writer.close()
            return
        backend_writer.write(_inject_forwarded_for(header, peer_host))
        await asyncio.gather(
            _copy(plain, _socket_write(backend_writer)),
            _copy(backend_reader, tls.write_plaintext),
        )
        tls.close()


def _socket_write(writer: asyncio.StreamWriter):
    async def write(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    return write


def reserve_backend_port() -> int:
    """Reserve an ephemeral localhost port for the plaintext backend."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
