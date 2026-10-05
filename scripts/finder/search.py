import argparse
import base64
import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import selectors
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent
REDIRECTS = {301, 302, 303, 307, 308}


class SearchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def normalize_isbn(value):
    value = re.sub(r"^isbn(?:-1[03])?\s*:?\s*", "", str(value).strip(), flags=re.I)
    value = re.sub(r"[\s-]", "", value).upper()
    if re.fullmatch(r"97[89]\d{10}", value):
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(value[:12]))
        if (10 - total % 10) % 10 == int(value[-1]):
            return value
    if re.fullmatch(r"\d{9}[\dX]", value):
        if sum((10 if c == "X" else int(c)) * (10 - i) for i, c in enumerate(value)) % 11 == 0:
            prefix = "978" + value[:9]
            total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(prefix))
            return prefix + str((10 - total % 10) % 10)
    raise SearchError("invalid_isbn", "Invalid ISBN: " + value[:40])


def parse_isbns(values):
    result = []
    for value in values:
        if not isinstance(value, str):
            raise SearchError("invalid_isbn", "ISBN inputs must be strings")
        for group in re.split(r"[,;\n]+", value):
            if not group.strip():
                continue
            try:
                parts = [normalize_isbn(group)]
            except SearchError:
                parts = [normalize_isbn(part) for part in group.split()]
            for part in parts:
                if part not in result:
                    result.append(part)
    if not 1 <= len(result) <= 100:
        raise SearchError("invalid_isbn", "Provide 1 to 100 distinct ISBNs")
    return result


class Client:
    def __init__(self):
        env = dict(os.environ)
        env.setdefault("BIBLIO_FETCH_MODE", "browser")
        env.pop("BIBLIO_ANNAS_API_KEY", None)
        self.process = subprocess.Popen(["node", "dist/index.js"], cwd=ROOT, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.number = 0
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "search-python", "version": "1.0.0"}})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def send(self, data):
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params, timeout=150):
        self.number += 1
        request_id = self.number
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise SearchError("server_closed", "The search server closed unexpectedly")
                self.buffer += chunk
                if len(self.buffer) > 8 * 1024 * 1024:
                    raise SearchError("response_limit", "Search response exceeded its size limit")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            data = json.loads(line)
            if data.get("id") != request_id:
                continue
            if "error" in data:
                raise SearchError("server_error", "The search server rejected the request")
            return data["result"]
        raise SearchError("server_timeout", "The search server exceeded its time limit")

    def call(self, name, arguments):
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if response.get("isError"):
            raise SearchError("tool_error", "The source operation could not complete")
        return json.loads(response["content"][0]["text"])

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.selector.close()
        self.process.stdout.close()


def public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        if (address not in ipaddress.ip_network("2000::/3") or address.ipv4_mapped or address.sixtofour
                or address.teredo or address in ipaddress.ip_network("2001::/23")
                or address in ipaddress.ip_network("3fff::/20")):
            return False
    elif address in ipaddress.ip_network("192.0.0.0/24") or address in ipaddress.ip_network("192.88.99.0/24"):
        return False
    return address.is_global and not address.is_multicast and not address.is_reserved


def approved_url(value):
    if not isinstance(value, str) or len(value) > 8192 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise SearchError("blocked_url", "Malformed source address")
    try:
        url = urllib.parse.urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.port not in (None, 443):
            raise ValueError()
        host = url.hostname.rstrip(".").encode("idna").decode("ascii")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        if re.search(r"\.(?:exe|dll|msi|scr|com|bat|cmd|ps1|js|vbs|sh|jar|app|dmg)$", urllib.parse.unquote(url.path), re.I):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not public_address(str(address)):
            raise ValueError()
    except ValueError:
        raise SearchError("blocked_url", "Only public HTTPS addresses without embedded credentials are accepted") from None
    return url, host


def public_endpoint(host, timeout=None):
    answers = queue.Queue(maxsize=1)

    def resolve():
        try:
            answers.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            answers.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        records = answers.get(timeout=timeout)
    except queue.Empty:
        raise SearchError("network_timeout", "Address lookup exceeded its time limit") from None
    if not records:
        raise SearchError("network_error", "Source address could not be resolved")
    if any(not public_address(record[4][0]) for record in records):
        raise SearchError("blocked_address", "Source address resolves to a non-public network")
    return records[0][0], records[0][4]


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, endpoint, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.endpoint = endpoint

    def connect(self):
        family, address = self.endpoint
        proxy = urllib.request.getproxies().get("https")
        use_proxy = proxy and not urllib.request.proxy_bypass(self.host)
        if use_proxy:
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.scheme != "http" or not parsed.hostname:
                raise SearchError("proxy_unsupported", "The configured HTTPS proxy must use an HTTP CONNECT endpoint")
            raw = socket.create_connection((parsed.hostname, parsed.port or 80), self.timeout)
        else:
            raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            if use_proxy:
                headers = {}
                if parsed.username is not None:
                    credential = urllib.parse.unquote(parsed.username) + ":" + urllib.parse.unquote(parsed.password or "")
                    headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credential.encode()).decode()
                self.set_tunnel(address[0], address[1], headers)
                self.sock = raw
                self._tunnel()
            else:
                raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def retrieve(url, target):
    current = url
    seen = set()
    for _ in range(6):
        parsed, host = approved_url(current)
        if current in seen:
            raise SearchError("redirect_loop", "Source redirects in a loop")
        seen.add(current)
        endpoint = public_endpoint(host)
        connection = PinnedConnection(host, endpoint, None)
        try:
            path = urllib.parse.urlunsplit(("", "", urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                                           urllib.parse.quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~"), ""))
            connection.request("GET", path, headers={"User-Agent": "search/1.0", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in REDIRECTS:
                location = response.getheader("Location")
                if not location:
                    raise SearchError("invalid_redirect", "Source redirect has no destination")
                current = urllib.parse.urljoin(current, location)
                continue
            if response.status != 200:
                raise SearchError("access_failed", "Source returned HTTP " + str(response.status))
            content_type = response.getheader("Content-Type", "").lower()
            if re.search(r"html|json|javascript|text/", content_type):
                raise SearchError("not_a_book", "Source returned a page or text response")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise SearchError("encoded_response", "Encoded transport bodies are not accepted")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) <= 0):
                raise SearchError("invalid_size", "Source size is invalid")
            size = 0
            with target.open("xb") as stream:
                os.chmod(target, 0o600)
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0 or (length is not None and size != int(length)):
                raise SearchError("incomplete_file", "Source returned empty or incomplete data")
            return {"url": current, "bytes": size, "content_type": content_type}
        except (OSError, http.client.HTTPException):
            raise SearchError("network_error", "Source connection or TLS validation failed") from None
        finally:
            connection.close()
    raise SearchError("redirect_limit", "Source exceeded the redirect limit")


def digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


def private_directory(path):
    path = path.absolute()
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise SearchError("unsafe_path", "State directories must not contain symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def inspect_file(path, md5, content_type):
    result = subprocess.run(["node", "scripts/check-file.mjs", str(path), md5, content_type], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise SearchError("file_rejected", "File did not pass the catalog MD5 and supported-format checks")
    return json.loads(result.stdout)["format"]


def process_link(url, md5, state_dir, run_id):
    quarantine = private_directory(state_dir / "quarantine")
    staging = Path(tempfile.mkdtemp(prefix="search-", dir=quarantine))
    path = staging / "item.part"
    try:
        transfer = retrieve(url, path)
        fingerprint = digest(path)
        format_name = inspect_file(path, md5, transfer["content_type"])
        if digest(path) != fingerprint:
            raise SearchError("file_changed", "Quarantined file changed during inspection")
        destination = private_directory(state_dir / "checked" / run_id) / (md5 + "." + format_name)
        os.link(path, destination, follow_symlinks=False)
        return {"status": "file_verified", "url": transfer["url"], "path": str(destination),
                "sha256": fingerprint, "md5": md5, "bytes": transfer["bytes"], "format": format_name,
                "checks": ["catalog-md5", "supported-format", "sha256-stability"], "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    finally:
        shutil.rmtree(staging)


def needs_verification(result):
    values = [error.get("code", "") for error in result.get("errors", [])]
    values += [attempt.get("code", "") for error in result.get("errors", []) for attempt in error.get("attempts", [])]
    return any(value in {"browser_challenge", "captcha", "login_required", "access_blocked"} for value in values)


def verified_books(result, isbn):
    books = []
    seen = set()
    for book in result.get("results", []):
        known = set()
        for value in book.get("isbns", []) + ([book["isbn"]] if book.get("isbn") else []):
            try:
                known.add(normalize_isbn(value))
            except SearchError:
                pass
        md5 = book.get("md5", "").lower()
        if isbn in known and re.fullmatch(r"[a-f0-9]{32}", md5) and md5 not in seen:
            seen.add(md5)
            books.append(book)
    return books


def resolve_book(client, book, state_dir, run_id):
    md5 = book["md5"].lower()
    response = client.call("get_download_links", {"md5": md5})
    links = response.get("links", [])
    seen = set()
    attempts = []
    for link in links:
        if not link.get("direct") or not link.get("url") or link["url"] in seen:
            continue
        if len(attempts) >= 5:
            break
        seen.add(link["url"])
        try:
            outcome = process_link(link["url"], md5, state_dir, run_id)
            return {**outcome, "attempts": attempts}
        except SearchError as error:
            attempts.append({"code": error.code, "reason": str(error)})
        except (OSError, subprocess.TimeoutExpired):
            attempts.append({"code": "processing_failed", "reason": "Local file processing did not complete"})
    return {"status": "verification_required" if needs_verification(response) else "not_released", "attempts": attempts,
            "reason": "No candidate passed all checks" if attempts else "No direct candidate was available"}


def save_report(path, report):
    descriptor, name = tempfile.mkstemp(prefix=".search-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(isbns, limit, state_dir, report_path, interactive):
    run_id = uuid.uuid4().hex
    report = {"run_id": run_id, "isbns": isbns, "checks": ["catalog-md5", "supported-format", "sha256-stability"], "results": [],
              "status": "running", "assessment": "Files match catalog MD5, supported format and stable SHA-256 checks."}
    completed = {}
    client = None
    save_report(report_path, report)
    try:
        client = Client()
        for isbn in isbns:
            print("Searching " + isbn, flush=True)
            row = {"isbn": isbn, "results": []}
            report["results"].append(row)
            try:
                for attempt in range(2):
                    result = client.call("search_books", {"query": isbn, "limit": 20, "refresh": True})
                    books = verified_books(result, isbn)
                    row["candidate_count"] = len(result.get("results", []))
                    row["verified_candidate_count"] = sum(book.get("isbnMatch") == "verified" for book in result.get("results", []))
                    row["verified_record_count"] = len(books)
                    row["selected_record_count"] = min(limit, len(books))
                    row["search_status"] = result.get("status")
                    row["source_errors"] = [{"source": e.get("source"), "code": e.get("code")} for e in result.get("errors", [])]
                    row["results"] = []
                    for book in books[:limit]:
                        md5 = book["md5"].lower()
                        reused = md5 in completed
                        if not reused:
                            print("Inspecting a verified record for " + isbn, flush=True)
                            completed[md5] = resolve_book(client, book, state_dir, run_id)
                        row["results"].append({"title": book["title"], "source": book["source"], "isbn_match": "verified",
                                               "reused_result": reused, **completed[md5]})
                        save_report(report_path, report)
                    verification = (not books and needs_verification(result)) or any(
                        item["status"] == "verification_required" for item in row["results"])
                    if not verification or attempt == 1:
                        break
                    if not interactive:
                        break
                    client.close()
                    client = None
                    print("Complete verification in the browser, then press Enter in its setup terminal.", flush=True)
                    if subprocess.run(["npm", "run", "browser-setup"], cwd=ROOT).returncode:
                        raise SearchError("verification_failed", "Browser setup did not complete")
                    completed = {key: value for key, value in completed.items() if value["status"] == "file_verified"}
                    client = Client()
                passed = sum(item["status"] == "file_verified" for item in row["results"])
                row["status"] = ("file_verified" if passed and passed == len(row["results"]) else "partial" if passed
                                 else "verification_required" if verification else "no_verified_result" if not books else "not_released")
                if verification:
                    row["reason"] = "Source verification is still required; rerun from a desktop terminal"
                print(isbn + ": " + str(passed) + " files verified", flush=True)
            except SearchError as error:
                row.update(status=error.code, reason=str(error))
            save_report(report_path, report)
        report["status"] = "complete" if all(row["status"] == "file_verified" for row in report["results"]) else "partial"
    except BaseException as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        raise
    finally:
        if client is not None:
            client.close()
        save_report(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(prog="search", description="Search ISBNs and inspect matching files in local quarantine.")
    parser.add_argument("isbns", nargs="*")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / "Downloads/knowledge")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        values = list(args.isbns)
        if args.input:
            contents = args.input.read_text()
            if args.input.suffix == ".json":
                supplied = json.loads(contents)
                if not isinstance(supplied, list):
                    raise SearchError("invalid_input", "JSON input must be an array of ISBN strings")
                values.extend(supplied)
            else:
                values.append(contents)
        if not values and sys.stdin.isatty():
            values.append(input("ISBNs: "))
        isbns = parse_isbns(values)
        if not 1 <= args.limit <= 20:
            raise SearchError("invalid_limit", "Limit must be between 1 and 20")
        if not (ROOT / "dist/index.js").exists():
            raise SearchError("build_required", "Run npm ci --ignore-scripts and npm run build first")
        state = private_directory(args.state_dir)
        output = args.output or private_directory(state / "reports") / ("search-" + uuid.uuid4().hex + ".json")
        if output.exists() or output.is_symlink():
            raise SearchError("output_exists", "Report path already exists; choose a new path")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        report = run(isbns, args.limit, state, output, sys.stdin.isatty())
        print("Report: " + str(output))
        return 0 if all(row["status"] == "file_verified" for row in report["results"]) else 2
    except (SearchError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped; completed results were preserved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
