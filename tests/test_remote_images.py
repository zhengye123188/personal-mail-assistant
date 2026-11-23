import io
import socket
import time
import unittest
from unittest.mock import MagicMock, patch

from maildesk import remote_images


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 24


class RemoteImagesTests(unittest.TestCase):
    def setUp(self):
        self.replies = []
        self.peers = []
        self.addresses = {"images.example": "93.184.216.34", "cdn.example": "93.184.216.35"}
        self.resolver = self.start_patch("_resolve", side_effect=self.resolve)
        self.socket = self.start_patch("socket.socket", side_effect=self.socket_for_reply)
        self.context = self.start_patch("ssl.create_default_context").return_value
        self.context.wrap_socket.side_effect = lambda sock, server_hostname: sock
        self.proxies = self.start_patch("urllib.request.getproxies", return_value={})

    def start_patch(self, name, **kwargs):
        mocked = patch(f"maildesk.remote_images.{name}", **kwargs)
        self.addCleanup(mocked.stop)
        return mocked.start()

    def resolve(self, host, port, deadline):
        address = self.addresses[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    def socket_for_reply(self, *_):
        peer = MagicMock()
        peer.__enter__.return_value = peer
        peer.makefile.return_value = io.BytesIO(self.replies.pop(0))
        self.peers.append(peer)
        return peer

    def reply(self, content_type, body, length=None):
        size = len(body) if length is None else length
        self.replies.append(f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                            f"Content-Length: {size}\r\n\r\n".encode() + body)

    def test_relative_redirect_and_cross_host_tls_pin_without_forwarding_identity(self):
        self.replies.extend([b"HTTP/1.1 302 Found\r\nLocation: /rotated\r\n\r\n",
                             b"HTTP/1.1 301 Moved\r\nLocation: https://cdn.example/photo.png\r\n\r\n"])
        self.reply("image/png", PNG)
        url = "https://images.example/read"
        self.assertEqual(remote_images.download_images([url, url]), {url: PNG})
        self.assertEqual(self.resolver.call_count, 3)
        self.assertEqual([call.kwargs["server_hostname"] for call in self.context.wrap_socket.call_args_list],
                         ["images.example", "images.example", "cdn.example"])
        self.peers[-1].connect.assert_called_once_with(("93.184.216.35", 443))
        request = self.peers[-1].sendall.call_args.args[0]
        self.assertIn(b"GET /photo.png HTTP/1.1", request)
        self.assertIn(b"Host: cdn.example\r\n", request)
        for header in (b"Referer:", b"Cookie:", b"Authorization:"):
            self.assertNotIn(header, request)

    def test_only_public_web_targets_even_after_a_redirect(self):
        self.replies.append(b"HTTP/1.1 307 Redirect\r\nLocation: https://internal.example/pixel\r\n\r\n")
        self.addresses["internal.example"] = "127.0.0.1"
        urls = ["file:///etc/hosts", "data:text/xml,xml", "http://user:pass@images.example/secret",
                "https://images.example:8080/", "https://localhost/mail", "https://images.example/ok"]
        self.assertEqual(remote_images.download_images(urls), {})
        self.assertEqual(self.socket.call_count, 1)
        self.assertEqual([call.args[0] for call in self.resolver.call_args_list],
                         ["images.example", "internal.example"])
        for address in ("0.0.0.0", "10.1.1.1", "169.254.169.254", "224.0.0.1", "::1",
                        "fc00::1", "::ffff:127.0.0.1", "2002:7f00:1::", "64:ff9b::7f00:1"):
            with self.subTest(address=address):
                self.assertFalse(remote_images._public_address(address))

    def test_encoded_or_mislabeled_documents_and_partial_transfers_are_omitted(self):
        self.reply("image/svg+xml", b"<svg/>")
        self.reply("image/png", b"<html>" * 40)
        self.reply("image/gif", PNG)
        self.reply("image/png", PNG, length=99)
        self.reply("application/octet-stream", PNG)
        urls = [f"http://images.example/{i}" for i in range(5)]
        self.assertEqual(remote_images.download_images(urls), {urls[-1]: PNG})
        self.context.wrap_socket.assert_not_called()

    def test_limits_declared_length_chunked_length_and_total_bytes(self):
        self.start_patch("_IMAGE_BYTES", new=len(PNG))
        self.start_patch("_BATCH_BYTES", new=2 * len(PNG) + 1)
        self.reply("image/png", b"", length=10_000)
        self.replies.append(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                            b"Transfer-Encoding: chunked\r\n\r\n64\r\n" + PNG + b"x" * (100 - len(PNG))
                            + b"\r\n0\r\n\r\n")
        self.reply("image/png", PNG)
        urls = [f"http://images.example/{i}" for i in range(8)]
        self.assertEqual(remote_images.download_images(urls), {urls[2]: PNG})
        self.assertEqual(len(self.peers), 3)

    def test_mixed_dns_destinations_are_rejected_without_a_connection(self):
        self.resolver.side_effect = None
        self.resolver.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
            for address in ("93.184.216.34", "192.168.1.1")
        ]
        with self.assertRaises(ValueError):
            remote_images._open_socket("mixed.example", 443, self.context, time.monotonic() + 1)
        self.socket.assert_not_called()

    def test_benchmark_dns_uses_configured_loopback_tunnel_and_authenticates_origin(self):
        self.resolver.side_effect = None
        self.resolver.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.2.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:2::1", 443, 0, 0)),
        ]
        self.proxies.return_value = {"https": "http://127.0.0.1:9988"}
        tunnel = self.start_patch("http.client.HTTPConnection")
        peer = tunnel.return_value.sock
        sock = remote_images._open_socket("images.example", 443, self.context, time.monotonic() + 1)
        self.assertEqual(tunnel.call_args.args, ("127.0.0.1", 9988))
        tunnel.return_value.set_tunnel.assert_called_once_with("images.example", 443)
        tunnel.return_value.connect.assert_called_once()
        self.assertIs(sock, peer)
        self.context.wrap_socket.assert_called_once_with(peer, server_hostname="images.example")
        self.socket.assert_not_called()

    def test_synthetic_dns_cannot_open_arbitrary_proxy_or_private_host(self):
        self.resolver.side_effect = None
        self.resolver.return_value = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                                       ("198.18.2.1", 443))]
        for url, proxies in [
            ("https://198.18.2.1/", {"https": "http://127.0.0.1:9988"}),
            ("https://router.home/", {"https": "http://127.0.0.1:9988"}),
            ("https://images.example/", {"https": "http://proxy.example:8888"}),
            ("https://images.example/", {"https": "http://user:pass@127.0.0.1:9988"}),
            ("https://images.example/", {"https": "http://127.0.0.1:9988", "no": "images.example"}),
            ("https://images.example/", {}),
        ]:
            with self.subTest(url=url):
                self.proxies.return_value = proxies
                self.assertEqual(remote_images.download_images([url]), {})
        self.socket.assert_not_called()
