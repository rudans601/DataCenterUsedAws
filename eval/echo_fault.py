"""이 실험의 echo 호출에만 첫 요청 HTTP 503을 한 번 주입한다."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import requests

from kubeops_copilot.first_response import ECHO_URL


@contextmanager
def one_unavailable_response():
    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.server.first:
                self.server.first = False
                self.send_error(503)
                return
            try:
                response = requests.post(ECHO_URL, data=body, timeout=(1, 3))
                self.send_response(response.status_code)
                self.end_headers()
                self.wfile.write(response.content)
            except requests.RequestException:
                self.send_error(502)

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Proxy) as server:
        server.first = True
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/api/echo"
        finally:
            server.shutdown()
            thread.join()
