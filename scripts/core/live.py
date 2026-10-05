from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from core.app import agent_at, check_html, status
from core.pipeline import pipeline_status


def live_server(home, agent_id, port=0):
    agent_at(home, agent_id)
    check_html(home, agent_id)
    html = Path(status(home, agent_id)['education_html']).read_bytes()
    token = secrets.token_urlsafe(32)
    prefix = '/' + token + '/'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            origin = 'http://127.0.0.1:' + str(self.server.server_port)
            if (self.headers.get('Host') != origin.removeprefix('http://')
                    or self.headers.get('Origin', origin) != origin
                    or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == prefix:
                body, kind = html, 'text/html; charset=utf-8'
            elif path == prefix + 'status':
                try:
                    value = pipeline_status(home, agent_id) or {'agent_id': agent_id, 'jobs': []}
                    value.pop('queue', None)
                    for job in value['jobs']:
                        job.pop('reason', None)
                        coverage = job.get('coverage') or {}
                        visual = coverage.get('visual_coverage')
                        if visual:
                            visual['gaps'] = [{} for _ in visual.get('gaps', [])]
                    body, kind = json.dumps(value).encode(), 'application/json'
                except (ValueError, OSError, KeyError):
                    self.send_error(503, 'Status unavailable')
                    return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server, 'http://127.0.0.1:' + str(server.server_port) + prefix + '#processing'


def serve(home, agent_id, port=0):
    server, url = live_server(home, agent_id, port)
    print(json.dumps({'url': url, 'agent_id': agent_id}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
