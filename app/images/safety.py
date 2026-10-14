"""SSRF guard for image fetches: http(s) only, public IPs only (checked before every hop
and again at connect time), at most ``MAX_REDIRECTS`` redirects, no cookies or referrer."""

import ipaddress
import socket
from collections.abc import Callable, Iterable

import httpcore
import httpx

MAX_REDIRECTS = 3

Resolver = Callable[[str], list[str]]


class Blocked(Exception):
    """A URL we refuse to fetch (``reason`` is a short code for stats)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise Blocked("dns_error", host) from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class Guard:
    def __init__(
        self, resolver: Resolver = system_resolver, allow_hosts: Iterable[str] = ()
    ) -> None:
        self.resolver = resolver
        self.allow_hosts = {h.strip().lower() for h in allow_hosts if h.strip()}

    def addresses(self, host: str) -> list[str]:
        """The host's addresses, all public (or the host is allow-listed); else Blocked."""
        host = host.strip("[]").lower()
        if host in self.allow_hosts:
            return self.resolver(host)
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            addresses = self.resolver(host)
        if not addresses:
            raise Blocked("dns_error", host)
        private = [a for a in addresses if not is_public(a)]
        if private:
            raise Blocked("private_address", f"{host} -> {private[0]}")
        return addresses

    def check_url(self, url: httpx.URL) -> None:
        if url.scheme not in ("http", "https"):
            raise Blocked("bad_scheme", url.scheme)
        if not url.host:
            raise Blocked("bad_url", str(url))
        self.addresses(url.host)


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """Connects to an address the guard just validated (no second DNS lookup, so a
    rebinding answer cannot sneak in); TLS still verifies the original hostname."""

    def __init__(self, guard: Guard) -> None:
        self.guard = guard
        self.inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = self.guard.addresses(host)
        return await self.inner.connect_tcp(
            addresses[0], port, timeout, local_address, socket_options
        )

    async def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover
        raise Blocked("bad_scheme", "unix socket")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def guarded_transport(guard: Guard) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(),
        network_backend=_GuardedBackend(guard),
    )
    return transport


async def guarded_get_stream(client: httpx.AsyncClient, guard: Guard, url: str):
    """Follow up to ``MAX_REDIRECTS`` redirects by hand, checking every hop; returns an open
    streaming response (caller closes it)."""
    current = httpx.URL(url)
    for _ in range(MAX_REDIRECTS + 1):
        guard.check_url(current)
        request = client.build_request("GET", current)
        request.headers.pop("cookie", None)
        request.headers.pop("referer", None)
        response = await client.send(request, stream=True, follow_redirects=False)
        if response.is_redirect:
            location = response.headers.get("location", "")
            await response.aclose()
            if not location:
                raise Blocked("bad_redirect")
            current = current.join(location)
            continue
        return response
    raise Blocked("too_many_redirects")
