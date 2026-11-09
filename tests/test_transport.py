"""Tests for MCP transport selection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp.server.transport_security import TransportSecurityMiddleware

from arxiv_mcp_server import server as server_module
from arxiv_mcp_server.config import Settings


@pytest.mark.asyncio
async def test_main_defaults_to_stdio_transport():
    """The default transport remains stdio for existing MCP clients."""
    with (
        patch.object(server_module, "settings", Settings()),
        patch.object(server_module, "_run_stdio", new_callable=AsyncMock) as mock_stdio,
        patch.object(
            server_module, "_run_streamable_http", new_callable=AsyncMock
        ) as mock_http,
    ):
        await server_module.main()

    mock_stdio.assert_awaited_once()
    mock_http.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "streamable-http", "streamable_http"])
async def test_main_runs_streamable_http_transport(transport):
    """HTTP transport aliases select the Streamable HTTP runner."""
    settings = Settings(TRANSPORT=transport)
    with (
        patch.object(server_module, "settings", settings),
        patch.object(server_module, "_run_stdio", new_callable=AsyncMock) as mock_stdio,
        patch.object(
            server_module, "_run_streamable_http", new_callable=AsyncMock
        ) as mock_http,
    ):
        await server_module.main()

    mock_stdio.assert_not_awaited()
    mock_http.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_rejects_unknown_transport():
    """Invalid transport values fail loudly instead of silently starting stdio."""
    with patch.object(server_module, "settings", Settings(TRANSPORT="websocket")):
        with pytest.raises(ValueError, match="Unsupported transport"):
            await server_module.main()


@pytest.mark.asyncio
async def test_streamable_http_uses_configured_host_port():
    """The HTTP runner builds a Starlette app and starts uvicorn with settings."""
    settings = Settings(TRANSPORT="http", HOST="127.0.0.1", PORT=8765)

    session_manager = MagicMock()
    session_context = AsyncMock()
    session_manager.run.return_value = session_context
    session_manager.handle_request = AsyncMock()

    uvicorn_server = MagicMock()
    uvicorn_server.serve = AsyncMock()

    with (
        patch.object(server_module, "settings", settings),
        patch.object(
            server_module, "StreamableHTTPSessionManager", return_value=session_manager
        ) as manager_class,
        patch.object(server_module.uvicorn, "Config") as config_class,
        patch.object(
            server_module.uvicorn, "Server", return_value=uvicorn_server
        ) as server_class,
    ):
        await server_module._run_streamable_http()

    _, manager_kwargs = manager_class.call_args
    assert manager_kwargs["app"] is server_module.server
    assert manager_kwargs["event_store"] is None
    assert manager_kwargs["json_response"] is False
    security_settings = manager_kwargs["security_settings"]
    assert security_settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in security_settings.allowed_hosts
    assert "localhost:*" in security_settings.allowed_hosts
    config_class.assert_called_once()
    _, config_kwargs = config_class.call_args
    assert config_kwargs["host"] == "127.0.0.1"
    assert config_kwargs["port"] == 8765
    server_class.assert_called_once_with(config_class.return_value)
    session_manager.run.assert_called_once()
    uvicorn_server.serve.assert_awaited_once()


def test_transport_security_allows_loopback_and_configured_hosts():
    """HTTP transport enables DNS rebinding protection with explicit allowlists."""
    settings = Settings(
        HOST="0.0.0.0",
        PORT=9000,
        ALLOWED_HOSTS="arxiv.example.com, arxiv.example.com:443",
        ALLOWED_ORIGINS="https://arxiv.example.com",
    )
    with patch.object(server_module, "settings", settings):
        security_settings = server_module._transport_security_settings()

    assert security_settings.enable_dns_rebinding_protection is True
    assert "0.0.0.0:9000" in security_settings.allowed_hosts
    assert "127.0.0.1:*" in security_settings.allowed_hosts
    assert "localhost:*" in security_settings.allowed_hosts
    assert "arxiv.example.com" in security_settings.allowed_hosts
    assert "arxiv.example.com:443" in security_settings.allowed_hosts
    assert "http://127.0.0.1:9000" in security_settings.allowed_origins
    assert "http://localhost:9000" in security_settings.allowed_origins
    assert "https://arxiv.example.com" in security_settings.allowed_origins


def test_transport_security_accepts_reverse_proxy_loopback_ports():
    """A loopback proxy may preserve its own port in the Host header."""
    settings = Settings(HOST="127.0.0.1", PORT=8000)
    with patch.object(server_module, "settings", settings):
        middleware = TransportSecurityMiddleware(
            server_module._transport_security_settings()
        )

    assert middleware._validate_host("localhost:8443")
    assert middleware._validate_host("127.0.0.1:9000")
    assert middleware._validate_host("[::1]:3000")
    assert not middleware._validate_host("localhost.attacker.example:8443")
    assert middleware._validate_origin("http://localhost:8443")
    assert middleware._validate_origin("https://127.0.0.1:9000")
    assert not middleware._validate_origin("https://localhost.attacker.example:8443")


def test_http_defaults_bind_to_localhost():
    """HTTP mode should not expose an unauthenticated MCP server by default."""
    settings = Settings()
    assert settings.TRANSPORT == "stdio"
    assert settings.HOST == "127.0.0.1"
    assert settings.PORT == 8000


def test_http_app_exposes_healthz():
    """HTTP deploys can probe /healthz without speaking MCP."""
    from starlette.testclient import TestClient

    session_manager = MagicMock()
    session_manager.handle_request = AsyncMock()
    app = server_module._build_http_app(session_manager)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/healthz", "/missing"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "OPTIONS"])
@pytest.mark.parametrize(
    "authorization", [None, "Bearer wrong", "Basic test-key", "Bearer"]
)
def test_http_auth_rejects_before_routing(path, method, authorization):
    from starlette.testclient import TestClient

    manager = MagicMock()
    manager.handle_request = AsyncMock()
    with patch.object(server_module, "settings", Settings(BEARER_KEY="test-key")):
        app = server_module._build_http_app(manager)
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        response = client.request(method, path, headers=headers, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "test-key" not in response.text
    manager.handle_request.assert_not_awaited()


def test_bearer_key_from_environment_is_redacted(monkeypatch):
    monkeypatch.setenv("BEARER_KEY", "secret-from-env")
    settings = Settings()
    assert settings.BEARER_KEY.get_secret_value() == "secret-from-env"
    assert "secret-from-env" not in repr(settings)
    assert "secret-from-env" not in settings.model_dump_json()


@pytest.mark.parametrize("key", [" ", "key with space", "key\n", "密钥"])
def test_http_rejects_unusable_key(key):
    with patch.object(server_module, "settings", Settings(BEARER_KEY=key)):
        with pytest.raises(ValueError, match="BEARER_KEY"):
            server_module._build_http_app(MagicMock())


def test_http_rejects_duplicate_authorization_and_query_key():
    from starlette.testclient import TestClient

    with patch.object(server_module, "settings", Settings(BEARER_KEY="test-key")):
        app = server_module._build_http_app(MagicMock())
    with TestClient(app) as client:
        assert client.get("/healthz?access_token=test-key").status_code == 401
        assert (
            client.get(
                "/healthz",
                headers=[
                    ("Authorization", "Bearer test-key"),
                    ("Authorization", "Bearer wrong"),
                ],
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/healthz",
                headers={
                    "Authorization": "bEaReR test-key",
                },
            ).status_code
            == 200
        )


def test_authenticated_mcp_initialize_and_list_tools():
    """Exercise real MCP sessions through the authentication middleware."""
    from contextlib import asynccontextmanager
    from starlette.testclient import TestClient
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(app=server_module.server, json_response=True)
    with patch.object(server_module, "settings", Settings(BEARER_KEY="test-key")):
        app = server_module._build_http_app(manager)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app.router.lifespan_context = lifespan
    headers = {
        "Authorization": "Bearer test-key",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert "serverInfo" in response.json()["result"]
        headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        response = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        assert response.status_code == 202
        response = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
        )
        assert response.status_code == 200
        assert any(
            t["name"] == "search_papers" for t in response.json()["result"]["tools"]
        )
        # An established session must not bypass per-request authentication.
        del headers["Authorization"]
        assert (
            client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/list",
                },
            ).status_code
            == 401
        )
