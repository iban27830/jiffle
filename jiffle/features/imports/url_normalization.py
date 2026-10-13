import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")
_BLOCKED_HOSTS = {"localhost", "host.docker.internal", "gateway.docker.internal"}


def _is_disallowed_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def normalize_source_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A valid HTTP or HTTPS URL is required.")
    hostname = parsed.hostname.lower().rstrip(".")
    if (
        hostname in _BLOCKED_HOSTS
        or hostname.endswith(_BLOCKED_HOST_SUFFIXES)
        or _is_disallowed_ip(hostname)
    ):
        raise ValueError("Local or private network URLs are not allowed.")
    port = f":{parsed.port}" if parsed.port else ""
    netloc_host = f"[{hostname}]" if ":" in hostname else hostname
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), netloc_host + port, path, parsed.query, ""))


def ensure_public_url(raw_url: str) -> str:
    normalized = normalize_source_url(raw_url)
    hostname = urlsplit(normalized).hostname
    if not hostname:
        raise ValueError("A valid HTTP or HTTPS URL is required.")
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return normalized
    for info in infos:
        sockaddr = info[4]
        if sockaddr and _is_disallowed_ip(str(sockaddr[0])):
            raise ValueError("Local or private network URLs are not allowed.")
    return normalized
