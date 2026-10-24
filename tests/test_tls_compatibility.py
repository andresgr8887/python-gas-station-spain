"""Exercise the Ministry's TLS 1.2 RSA-only negotiation without external I/O."""

import asyncio
import ssl
import subprocess

import aiohttp
import pytest

from gas_station_spain_api import gas_station as api


def test_tls_policy_preserves_verification_and_default_ciphers():
    default = ssl.create_default_context()
    context = api._build_ssl_context()
    defaults = {item["name"] for item in default.get_ciphers()}
    actual = {item["name"] for item in context.get_ciphers()}
    assert actual == defaults | {"AES256-GCM-SHA384"}
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version == default.minimum_version
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.security_level == default.security_level


@pytest.fixture
def server_certificate(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.mark.asyncio
async def test_rsa_only_server_reproduces_reset_and_fixed_client_succeeds(
    server_certificate,
):
    cert, key = server_certificate
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.maximum_version = ssl.TLSVersion.TLSv1_2
    server_context.set_ciphers("AES256-GCM-SHA384")

    async def respond(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]

    async def request(context, host="localhost"):
        async with (
            aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=context),
                timeout=aiohttp.ClientTimeout(total=3),
            ) as session,
            session.get(f"https://{host}:{port}/") as response,
        ):
            assert response.status == 200
            return await response.json()

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def expected_handshake_failure(loop, context):
        # The negative cases deliberately make the server reject a handshake.
        if isinstance(context.get("exception"), (ssl.SSLError, ConnectionResetError)):
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(expected_handshake_failure)
    try:
        original = ssl.create_default_context(cafile=str(cert))
        with pytest.raises(aiohttp.ClientConnectionError):
            await request(original)
        fixed = api._build_ssl_context()
        fixed.load_verify_locations(cafile=str(cert))
        assert await request(fixed) == {}
        # Compatibility must not turn into a certificate-validation bypass.
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            await request(fixed, "127.0.0.1")
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            await request(api._build_ssl_context())
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
