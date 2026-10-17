"""Validation for tenant-supplied outbound URLs.

Callers must still enforce outbound network policy at the container/network
boundary; DNS validation alone cannot prevent a malicious DNS rebinding host.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeOutboundURL(ValueError):
    pass


async def validate_public_url(url: str, *, require_https: bool = False) -> str:
    parsed = urlsplit(url)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        raise UnsafeOutboundURL("URL must use a public HTTP(S) hostname.")
    if parsed.username or parsed.password or parsed.fragment:
        raise UnsafeOutboundURL("URL credentials and fragments are not allowed.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise UnsafeOutboundURL("Private and local hostnames are not allowed.")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos}
    except (OSError, ValueError) as exc:
        raise UnsafeOutboundURL("URL host could not be resolved safely.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeOutboundURL("URL resolves to a private or reserved network address.")
    return url
