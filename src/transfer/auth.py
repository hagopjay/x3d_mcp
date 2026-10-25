"""Bearer-token checks and Host/Origin guards shared by the blob routes and the MCP tools."""

import hashlib
import hmac

from .errors import ConfigError


def _digest(value: str) -> bytes:
    return hashlib.sha256((value or "").encode("utf-8", "surrogatepass")).digest()


def bearer_ok(authorization: str | None, token: str) -> bool:
    """Whether an Authorization header carries the bearer token.

    Both sides are hashed first so the comparison is constant-time whatever the
    lengths; the Basic scheme with the right token is refused."""
    if not token:
        return False
    scheme, _, value = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(_digest(value.strip()), _digest(token))


def request_of(ctx):
    """The Starlette request behind an MCP call, or None under stdio and outside a request."""
    if ctx is None:
        return None
    try:
        return ctx.request_context.request
    except Exception:
        return None


def tool_gate(ctx, cfg) -> str | None:
    """None when the caller may use blob parameters and tools, else the refusal message.

    Under stdio the process belongs to the user, so there is nothing to check. Over HTTP
    /mcp itself has no authentication, so the blob token is required on the MCP connection
    too (unless X3D_MCP_BLOB_TOOLS_OPEN=1): otherwise anyone who can reach /mcp could fill
    the store."""
    if not cfg.enabled:
        return ("blob store is not enabled on this server "
                "(over HTTP it needs X3D_MCP_BLOB_TOKEN; X3D_MCP_BLOB=off switches it off)")
    if not cfg.http:
        return None
    req = request_of(ctx)
    if req is None:
        return "blob access needs an HTTP request carrying the Authorization: Bearer header"
    if cfg.tools_open or bearer_ok(req.headers.get("authorization"), cfg.token):
        return None
    return ("blob parameters and tools need the Authorization: Bearer header on the MCP "
            "connection (the value of X3D_MCP_BLOB_TOKEN)")


def explicit_security(cfg):
    """Host/Origin settings built from X3D_MCP_ALLOWED_HOSTS/_ORIGINS, or None when neither is set.

    Origins alone are refused (ConfigError): the SDK's check rejects every request whose Host
    is not listed, so an empty host list would answer 421 to every client."""
    from mcp.server.transport_security import TransportSecuritySettings
    if not (cfg.allowed_hosts or cfg.allowed_origins):
        return None
    if not cfg.allowed_hosts:
        raise ConfigError("X3D_MCP_ALLOWED_ORIGINS needs X3D_MCP_ALLOWED_HOSTS as well: the "
                          "SDK's check refuses every request whose Host is not listed")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(cfg.allowed_hosts),
        allowed_origins=list(cfg.allowed_origins),
    )


def transport_security(mcp, cfg):
    """The settings for Host/Origin checks: the server's own when it has them (a loopback
    bind enables them automatically), else built from X3D_MCP_ALLOWED_HOSTS/_ORIGINS, else None."""
    return explicit_security(cfg) or getattr(mcp.settings, "transport_security", None)
