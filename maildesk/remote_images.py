"""Fetch external raster images for the open message."""

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.request
from collections.abc import Callable, Iterable
from contextlib import closing, suppress
from dataclasses import dataclass
from itertools import islice
from threading import BoundedSemaphore, Event, Thread, Timer
from urllib.parse import quote, urljoin, urlsplit


MAX_IMAGES = 20
_IMAGE_BYTES = 4 * 1024 * 1024
_BATCH_BYTES = 16 * 1024 * 1024
_BATCH_SECONDS = 30
_IMAGE_SECONDS = 7
_MAX_REDIRECTS = 3
_DNS_SLOTS = BoundedSemaphore(4)
_BENCHMARK_V4 = ipaddress.ip_network("198.18.0.0/15")
_BENCHMARK_V6 = ipaddress.ip_network("2001:2::/48")
_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_FALLBACK_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


@dataclass
class _Budget:
    remaining: int


def _time_left(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _resolve(host, port, deadline):
    # getaddrinfo has no timeout. A stalled system resolver must not hold up a
    # mail worker or grow an unbounded queue of resolver threads.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise OSError("解析服务繁忙")
    ready = Event()
    addresses = []

    def resolve():
        try:
            addresses.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                                proto=socket.IPPROTO_TCP))
        except (OSError, UnicodeError):
            pass
        finally:
            _DNS_SLOTS.release()
            ready.set()

    Thread(target=resolve, daemon=True).start()
    if not ready.wait(_time_left(deadline)):
        raise TimeoutError
    return addresses


def _public_address(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        # Accept native global unicast; exclude transition addresses that
        # encapsulate a different IPv4 destination.
        if ip not in ipaddress.IPv6Network("2000::/3") or ip.sixtofour or ip.teredo:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _synthetic_dns(host, addresses):
    # A local TUN proxy may use benchmark addresses as opaque DNS tokens.
    # Never connect to those addresses, even when they look like public IPs.
    name = host.lower().rstrip(".")
    if (":" in name or not name.rpartition(".")[2][:1].isalpha() or
            name.endswith((".arpa", ".internal", ".localdomain", ".lan", ".home", ".corp", ".onion"))):
        return False
    ips = [ipaddress.ip_address(entry[4][0]) for entry in addresses]
    return (any(ip in _BENCHMARK_V4 for ip in ips)
            and all(ip in _BENCHMARK_V4 or ip in _BENCHMARK_V6 for ip in ips))


def _interrupt(sock):
    if sock is not None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def _local_proxy_tunnel(host, port, deadline):
    proxies = urllib.request.getproxies()
    if urllib.request.proxy_bypass_environment(host, proxies):
        raise ValueError("此主机已跳过代理")
    setting = proxies.get("https" if port == 443 else "http", "")
    proxy = urlsplit(setting if "://" in setting else "http://" + setting)
    if proxy.scheme != "http" or not proxy.hostname or proxy.username is not None:
        raise ValueError("未设置本机 HTTP 代理")
    # Only a trusted proxy explicitly configured on this computer may resolve
    # a synthetic hostname. Mail content cannot choose the proxy address.
    local = "127.0.0.1" if proxy.hostname == "localhost" else proxy.hostname
    if not ipaddress.ip_address(local).is_loopback:
        raise ValueError("不支持此图片代理")
    with closing(http.client.HTTPConnection(local, proxy.port or 80, timeout=_time_left(deadline))) as conn:
        conn.set_tunnel(host, port)
        guard = Timer(_time_left(deadline), lambda: _interrupt(conn.sock))
        guard.daemon = True
        guard.start()
        try:
            conn.connect()
            sock, conn.sock = conn.sock, None
            return sock
        finally:
            guard.cancel()


def _open_socket(host, port, tls, deadline):
    addresses = _resolve(host, port, deadline)
    if _synthetic_dns(host, addresses):
        sock = _local_proxy_tunnel(host, port, deadline)
        try:
            sock.settimeout(_time_left(deadline))
            return tls.wrap_socket(sock, server_hostname=host) if tls is not None else sock
        except OSError:
            sock.close()
            raise
    if not addresses or any(not _public_address(sockaddr[0])
                            for _, _, _, _, sockaddr in addresses):
        raise ValueError("仅支持公网图片地址")
    for family, kind, protocol, _, sockaddr in addresses:
        sock = socket.socket(family, kind, protocol)
        try:
            # Connect directly to the vetted numeric address; TLS still
            # authenticates the original host name.
            sock.settimeout(min(2.5, _time_left(deadline)))
            sock.connect(sockaddr)
            sock.settimeout(_time_left(deadline))
            if tls is not None:
                sock = tls.wrap_socket(sock, server_hostname=host)
            return sock
        except OSError:
            sock.close()
    raise OSError("图片连接失败")


def _destination(url):
    if (not isinstance(url, str) or len(url) > 8192 or "\\" in url
            or any(ord(char) < 32 or ord(char) == 127 for char in url)):
        raise ValueError("无效的图片链接")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
        raise ValueError("不支持的图片链接")
    host = parts.hostname.encode("idna").decode("ascii")
    name = host.rstrip(".").lower()
    if "%" in host or name.endswith((".local", ".localhost")) or ("." not in name and ":" not in name):
        raise ValueError("不支持的图片主机")
    port = 443 if parts.scheme == "https" else 80
    if parts.port is not None and parts.port != port:
        raise ValueError("不支持的图片服务端口")
    target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        target += "?" + quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    return host, port, target


def _raster_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _read_image(response, budget, deadline):
    mime = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
    if mime not in _TYPES and mime not in _FALLBACK_TYPES:
        return None
    if response.getheader("Content-Encoding", "identity").strip().lower() != "identity":
        return None
    limit = min(_IMAGE_BYTES, budget.remaining)
    length = response.getheader("Content-Length")
    if length is not None and not 0 < int(length) <= limit:
        return None
    data = bytearray(response.read(min(12, limit + 1)))
    budget.remaining -= len(data)
    kind = _raster_type(data)
    if not kind or len(data) > limit or not (mime == kind or mime in _FALLBACK_TYPES):
        return None
    while True:
        _time_left(deadline)
        chunk = response.read(min(65536, limit + 1 - len(data)))
        budget.remaining -= len(chunk)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            return None
    return bytes(data) if length is None or int(length) == len(data) else None


def _download(url, tls, budget, deadline):
    for _ in range(_MAX_REDIRECTS + 1):
        host, port, target = _destination(url)
        with _open_socket(host, port, tls if port == 443 else None, deadline) as sock:
            # A peer that trickles headers or a chunk body can evade an idle
            # timeout. Interrupt the actual connection on the absolute deadline.
            guard = Timer(_time_left(deadline), _interrupt, args=(sock,))
            guard.daemon = True
            guard.start()
            try:
                with closing(http.client.HTTPConnection(host, port)) as conn:
                    conn.sock = sock
                    conn.auto_open = False
                    conn.request("GET", target, headers={"Host": f"[{host}]" if ":" in host else host,
                                                         "Accept": "image/png,image/jpeg,image/gif,image/webp",
                                                         "Connection": "close"})
                    with conn.getresponse() as response:
                        if response.status == 200:
                            return _read_image(response, budget, deadline)
                        location = response.getheader("Location")
                        if response.status not in (301, 302, 303, 307, 308) or not location:
                            return None
                        url = urljoin(url, location)
                        if port == 443 and urlsplit(url).scheme != "https":
                            return None
            finally:
                guard.cancel()
    return None


def download_images(urls: Iterable[str], *, cancelled: Callable[[], bool] | None = None) -> dict[str, bytes]:
    """Fetch up to MAX_IMAGES public URLs. Keys refer to the original sources."""
    results = {}
    budget = _Budget(_BATCH_BYTES)
    end = time.monotonic() + _BATCH_SECONDS
    tls = ssl.create_default_context()
    for url in dict.fromkeys(islice(urls, MAX_IMAGES)):
        if (cancelled and cancelled()) or budget.remaining <= 0 or time.monotonic() >= end:
            break
        try:
            data = _download(url, tls, budget, min(end, time.monotonic() + _IMAGE_SECONDS))
        except (OSError, ValueError, http.client.HTTPException, UnicodeError):
            continue
        if data:
            results[url] = data
    return results
