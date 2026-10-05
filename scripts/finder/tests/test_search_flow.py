import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import search


ISBN = "9780596805524"
HASH = "a" * 32


def pdf():
    value = b"%PDF-1.4\n"
    offsets = [0]
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>",
               b"<< /Length 0 >>\nstream\n\nendstream"]
    for index, content in enumerate(objects, 1):
        offsets.append(len(value))
        value += str(index).encode() + b" 0 obj\n" + content + b"\nendobj\n"
    start = len(value)
    value += b"xref\n0 5\n0000000000 65535 f \n"
    value += b"".join(("%010d 00000 n \n" % offset).encode() for offset in offsets[1:])
    return value + b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"


class Response:
    def __init__(self, body=b"fixture", status=200, headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/pdf", "Content-Length": str(len(body))}
        self.body = io.BytesIO(body)

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read(self, size):
        return self.body.read(size)


class Connection:
    def __init__(self, response):
        self.response = response
        self.sock = MagicMock()
        self.requests = []

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_code(self, code, function, *args):
        with self.assertRaises(search.SearchError) as error:
            function(*args)
        self.assertEqual(error.exception.code, code)

    def test_isbn_normalization_and_batch_input(self):
        self.assertEqual(search.parse_isbns(["0-596-80552-7, 9780596805524", "9781593273880\n9780321618528"]),
                         [ISBN, "9781593273880", "9780321618528"])
        self.assertEqual(search.parse_isbns(["ISBN-13: 978 0 596 80552 4"]), [ISBN])
        self.assert_code("invalid_isbn", search.parse_isbns, ["9780596805525"])
        self.assert_code("invalid_isbn", search.parse_isbns, [])
        self.assert_code("invalid_isbn", search.parse_isbns, [9780596805524])

    def test_only_metadata_verified_records_with_hashes_are_eligible(self):
        result = {"results": [{"md5": HASH, "isbnMatch": "verified"}, {"md5": HASH, "isbns": [ISBN]},
                              {"md5": HASH, "isbn": ISBN}, {"isbn": ISBN}, {"md5": "b" * 32, "isbn": "9781593273880"}]}
        self.assertEqual(len(search.verified_books(result, ISBN)), 1)

    def test_rejects_non_public_addresses_and_transition_ranges(self):
        for address in ["127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                        "192.0.0.9", "192.88.99.1", "198.18.0.1", "::1", "fc00::1", "fe80::1",
                        "::ffff:127.0.0.1", "2002:7f00:1::", "64:ff9b::7f00:1", "3fff::1"]:
            self.assertFalse(search.public_address(address), address)
        self.assertTrue(search.public_address("8.8.8.8"))
        self.assertTrue(search.public_address("2606:4700:4700::1111"))

    def test_rejects_unsafe_urls_before_connecting(self):
        for url in ["http://example.com/a", "file:///etc/passwd", "https://user:secret@example.com/a",
                    "https://127.0.0.1/a", "https://[::1]/a", "https://localhost/a", "https://x.local/a",
                    "https://example.com:8443/a", "https://example.com/a.exe", "https://example.com/a%2Eexe",
                    "https://example.com/a\r\nHeader: value", "https://example.com\\@localhost/a"]:
            self.assert_code("blocked_url", search.approved_url, url)

    def test_all_dns_answers_are_checked(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(search.socket, "getaddrinfo", return_value=answers):
            self.assert_code("blocked_address", search.public_endpoint, "example.com", 1)

    def test_connection_uses_pinned_address_and_original_tls_hostname(self):
        raw = MagicMock()
        context = MagicMock()
        endpoint = (socket.AF_INET, ("8.8.8.8", 443))
        with patch.object(search.socket, "socket", return_value=raw), patch.object(search.ssl, "create_default_context", return_value=context), \
                patch.object(search.urllib.request, "getproxies", return_value={}):
            connection = search.PinnedConnection("example.com", endpoint, 3)
            connection.connect()
        raw.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")

    def test_proxy_connect_pins_the_destination_and_preserves_tls_hostname(self):
        raw = MagicMock()
        context = MagicMock()
        with patch.object(search.socket, "create_connection", return_value=raw) as connect, \
                patch.object(search.ssl, "create_default_context", return_value=context), \
                patch.object(search.urllib.request, "getproxies", return_value={"https": "http://user:password@proxy:8080"}), \
                patch.object(search.urllib.request, "proxy_bypass", return_value=False):
            connection = search.PinnedConnection("example.com", (socket.AF_INET, ("8.8.8.8", 443)), None)
            with patch.object(connection, "_tunnel") as tunnel:
                connection.connect()
            connect.assert_called_once_with(("proxy", 8080), None)
            tunnel.assert_called_once()
            self.assertEqual(connection._tunnel_host, "8.8.8.8")
            self.assertEqual(connection._tunnel_port, 443)
            self.assertIn("Proxy-Authorization", connection._tunnel_headers)
            context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")

    def test_unsupported_proxy_fails_without_opening_a_direct_connection(self):
        with patch.object(search.urllib.request, "getproxies", return_value={"https": "socks5://proxy:1080"}), \
                patch.object(search.urllib.request, "proxy_bypass", return_value=False), \
                patch.object(search.socket, "socket") as direct:
            connection = search.PinnedConnection("example.com", (socket.AF_INET, ("8.8.8.8", 443)), None)
            self.assert_code("proxy_unsupported", connection.connect)
            direct.assert_not_called()

    def test_redirects_are_checked_before_the_next_request(self):
        for destination in ["https://127.0.0.1/private", "http://example.com/item"]:
            connection = Connection(Response(status=302, headers={"Location": destination}))
            with patch.object(search, "public_endpoint", return_value=(socket.AF_INET, ("8.8.8.8", 443))) as resolve, \
                    patch.object(search, "PinnedConnection", return_value=connection) as factory:
                self.assert_code("blocked_url", search.retrieve, "https://example.com/start", self.root / "item")
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(factory.call_count, 1)

    def test_valid_redirect_uses_fresh_dns_and_no_credentials(self):
        connections = [Connection(Response(status=302, headers={"Location": "https://other.example/item"})), Connection(Response())]
        with patch.object(search, "public_endpoint", return_value=(socket.AF_INET, ("8.8.8.8", 443))) as resolve, \
                patch.object(search, "PinnedConnection", side_effect=connections):
            result = search.retrieve("https://example.com/start", self.root / "item")
        self.assertEqual(result["url"], "https://other.example/item")
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual((self.root / "item").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Cookie", connections[1].requests[0][2])
        self.assertNotIn("Authorization", connections[1].requests[0][2])

    def test_files_over_100_mib_are_accepted_with_and_without_content_length(self):
        body = b"x" * (101 * 1024 * 1024)
        for declared in (False, True):
            headers = {"Content-Type": "application/pdf"}
            if declared:
                headers["Content-Length"] = str(len(body))
            connection = Connection(Response(body=body, headers=headers))
            path = self.root / str(declared)
            with patch.object(search, "public_endpoint", return_value=(socket.AF_INET, ("8.8.8.8", 443))), \
                    patch.object(search, "PinnedConnection", return_value=connection):
                result = search.retrieve("https://example.com/item", path)
            self.assertEqual(result["bytes"], len(body))
            self.assertEqual(path.stat().st_size, len(body))

    def test_transfer_can_continue_beyond_60_seconds_without_socket_deadline(self):
        now = [0]
        response = Response()
        original_read = response.read

        def read(size):
            now[0] += 61
            return original_read(size)

        response.read = read
        with patch.object(search.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(search, "public_endpoint", return_value=(socket.AF_INET, ("8.8.8.8", 443))), \
                patch.object(search, "PinnedConnection", return_value=Connection(response)) as factory:
            result = search.retrieve("https://example.com/item", self.root / "item")
        self.assertEqual(result["bytes"], 7)
        self.assertGreater(now[0], 60)
        self.assertIsNone(factory.call_args.args[2])

    def test_international_paths_are_encoded_for_transport(self):
        connection = Connection(Response())
        with patch.object(search, "public_endpoint", return_value=(socket.AF_INET, ("8.8.8.8", 443))), \
                patch.object(search, "PinnedConnection", return_value=connection):
            search.retrieve("https://example.com/café.pdf?title=café", self.root / "item")
        self.assertEqual(connection.requests[0][1], "/caf%C3%A9.pdf?title=caf%C3%A9")

    def test_pages_errors_and_invalid_sizes_never_become_files(self):
        cases = [(Response(status=403), "access_failed"),
                 (Response(headers={"Content-Type": "text/html"}), "not_a_book"),
                 (Response(headers={"Content-Length": "-1"}), "invalid_size"),
                 (Response(headers={"Content-Encoding": "gzip"}), "encoded_response")]
        for index, (response, code) in enumerate(cases):
            path = self.root / str(index)
            with patch.object(search, "public_endpoint", return_value=(socket.AF_INET, ("8.8.8.8", 443))), \
                    patch.object(search, "PinnedConnection", return_value=Connection(response)):
                self.assert_code(code, search.retrieve, "https://example.com/item", path)
            self.assertFalse(path.exists())

    def test_tls_failure_is_closed(self):
        connection = Connection(Response())
        connection.request = MagicMock(side_effect=ssl.SSLError("certificate failed"))
        with patch.object(search, "public_endpoint", return_value=(socket.AF_INET, ("8.8.8.8", 443))), \
                patch.object(search, "PinnedConnection", return_value=connection):
            self.assert_code("network_error", search.retrieve, "https://example.com/item", self.root / "item")

    def transfer(self, url, path):
        path.write_bytes(pdf())
        path.chmod(0o600)
        return {"url": url, "bytes": path.stat().st_size, "content_type": "application/pdf"}

    def test_file_is_released_after_validation_without_antivirus(self):
        with patch.object(search, "retrieve", side_effect=self.transfer):
            result = search.process_link("https://example.com/item", hashlib.md5(pdf()).hexdigest(), self.root, "run")
        self.assertEqual(result["status"], "file_verified")
        self.assertEqual(Path(result["path"]).read_bytes(), pdf())
        self.assertEqual(result["sha256"], hashlib.sha256(pdf()).hexdigest())
        self.assertEqual(result["checks"], ["catalog-md5", "supported-format", "sha256-stability"])
        self.assertNotIn("engine", result)
        self.assertEqual(list((self.root / "quarantine").iterdir()), [])

    def test_wrong_hash_or_unsupported_file_is_never_released(self):
        with patch.object(search, "retrieve", side_effect=self.transfer):
            self.assert_code("file_rejected", search.process_link, "https://example.com/item", HASH, self.root, "run")
        self.assertFalse((self.root / "checked").exists())
        self.assertEqual(list((self.root / "quarantine").iterdir()), [])
        def transfer(url, path):
            path.write_bytes(b"not a supported book")
            return {"url": url, "bytes": path.stat().st_size, "content_type": "application/octet-stream"}
        with patch.object(search, "retrieve", side_effect=transfer):
            self.assert_code("file_rejected", search.process_link, "https://example.com/item",
                             hashlib.md5(b"not a supported book").hexdigest(), self.root, "run")
        self.assertFalse((self.root / "checked").exists())
        self.assertEqual(list((self.root / "quarantine").iterdir()), [])

    def test_file_changed_during_validation_is_never_released(self):
        def inspect(path, md5, content_type):
            path.write_bytes(b"changed")
            return "pdf"
        with patch.object(search, "retrieve", side_effect=self.transfer), patch.object(search, "inspect_file", side_effect=inspect):
            self.assert_code("file_changed", search.process_link, "https://example.com/item", HASH, self.root, "run")
        self.assertFalse((self.root / "checked").exists())
        self.assertEqual(list((self.root / "quarantine").iterdir()), [])

    def test_default_knowledge_folder_contains_files_and_updated_reports_across_runs(self):
        ready = {"results": [{"title": "Fixture", "source": "libgen", "md5": HASH, "isbns": [ISBN],
                             "isbnMatch": "verified"}], "errors": [], "status": "complete"}
        client = MagicMock()
        client.call.return_value = ready
        snapshots = []
        original_save = search.save_report

        def save(path, report):
            original_save(path, report)
            snapshots.append(json.loads(path.read_text()))

        def resolve(client, book, state, run_id):
            return search.process_link("https://example.com/item", HASH, state, run_id)

        knowledge = self.root / "Downloads" / "knowledge"
        self.assertFalse(knowledge.exists())
        with patch.object(search.Path, "home", return_value=self.root), \
                patch.object(sys, "argv", ["search.py", ISBN, "--limit", "1"]), \
                patch.object(search, "Client", return_value=client), \
                patch.object(search, "retrieve", side_effect=self.transfer), \
                patch.object(search, "inspect_file", return_value="pdf"), \
                patch.object(search, "resolve_book", side_effect=resolve), \
                patch.object(search, "save_report", side_effect=save), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(search.main(), 0)
            reports = list((knowledge / "reports").glob("*.json"))
            self.assertEqual(len(reports), 1)
            original_report = reports[0].read_bytes()
            self.assertEqual(search.main(), 0)
        self.assertEqual(reports[0].read_bytes(), original_report)
        reports = list((knowledge / "reports").glob("*.json"))
        self.assertEqual(len(reports), 2)
        for path in reports:
            report = json.loads(path.read_text())
            self.assertEqual(report["status"], "complete")
            item = report["results"][0]["results"][0]
            self.assertTrue(Path(item["path"]).is_relative_to(knowledge / "checked"))
            self.assertEqual(Path(item["path"]).read_bytes(), pdf())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list((knowledge / "checked").glob("*/*.pdf"))), 2)
        self.assertEqual(knowledge.stat().st_mode & 0o777, 0o700)
        self.assertTrue(any(snapshot["status"] == "running" and any(
            row["results"] for row in snapshot["results"]) for snapshot in snapshots))

    def test_symlink_state_directory_is_rejected(self):
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to(self.root / "real", target_is_directory=True)
        self.assert_code("unsafe_path", search.private_directory, self.root / "link" / "child")

    def test_verification_closes_profile_before_human_setup_and_retries_once(self):
        blocked = {"results": [], "errors": [{"code": "browser_challenge"}]}
        ready = {"results": [{"title": "Fixture", "source": "libgen", "md5": HASH, "isbns": [ISBN]}], "errors": [], "status": "complete"}
        first, second = MagicMock(), MagicMock()
        first.call.return_value = blocked
        second.call.return_value = ready

        def setup(*args, **kwargs):
            first.close.assert_called_once()
            return subprocess.CompletedProcess([], 0)

        with patch.object(search, "Client", side_effect=[first, second]), \
                patch.object(search.subprocess, "run", side_effect=setup) as prompt, \
                patch.object(search, "resolve_book", return_value={"status": "file_verified", "path": "fixture"}), contextlib.redirect_stdout(io.StringIO()):
            report = search.run([ISBN], 1, self.root, self.root / "report.json", True)
        prompt.assert_called_once()
        self.assertEqual(report["results"][0]["status"], "file_verified")
        second.close.assert_called_once()

    def test_noninteractive_challenge_is_reported_without_pretending_success(self):
        client = MagicMock()
        client.call.return_value = {"results": [], "errors": [{"code": "captcha"}]}
        with patch.object(search, "Client", return_value=client), \
                patch.object(search.subprocess, "run") as prompt, contextlib.redirect_stdout(io.StringIO()):
            report = search.run([ISBN], 1, self.root, self.root / "report.json", False)
        prompt.assert_not_called()
        self.assertEqual(report["results"][0]["status"], "verification_required")

    def test_python_client_uses_actual_mcp_and_never_follows_blocked_file_links(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.end_headers()
                if self.path.startswith("/ads.php"):
                    value = '<a href="/get.php?md5=' + HASH + '">GET</a>'
                elif self.path.startswith("/md5/"):
                    value = '<title>Captcha</title>'
                else:
                    value = '<table><tr><td>Author</td><td><a href="ads.php?md5=' + HASH + '">Fixture</a></td><td>ISBN ' + ISBN + '</td></tr></table>'
                self.wfile.write(value.encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        mirror = "http://127.0.0.1:" + str(server.server_port)
        try:
            with patch.dict(os.environ, {"BIBLIO_FETCH_MODE": "http", "BIBLIO_LIBGEN_MIRRORS": mirror,
                                         "BIBLIO_ANNAS_MIRRORS": mirror, "BIBLIO_ZLIB_MIRRORS": mirror}):
                client = search.Client()
                try:
                    result = client.call("search_books", {"query": ISBN, "sources": ["libgen"], "limit": 1})
                    self.assertEqual(result["results"][0]["isbnMatch"], "verified")
                    outcome = search.resolve_book(client, result["results"][0], self.root, "run")
                    self.assertEqual(outcome["status"], "verification_required")
                    self.assertEqual(outcome["attempts"][0]["code"], "blocked_url")
                finally:
                    client.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(any(path.startswith("/get.php") for path in paths))



if __name__ == "__main__":
    unittest.main()
