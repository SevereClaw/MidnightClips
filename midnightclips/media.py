"""Маленький локальный сервер, который отдаёт клипы встроенному плееру (с перемоткой)."""
import os
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TYPES = {".m4a": "audio/mp4", ".mp4": "video/mp4", ".mov": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm"}


class MediaServer:
    def __init__(self, allowed):
        """allowed(path) -> bool: можно ли отдавать этот файл (только из папки клипов)."""
        self.token = secrets.token_urlsafe(18)
        token = self.token
        extra = self._extra = set()   # отдельные файлы вне папки клипов, которые тоже можно отдавать (звуковые дорожки)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self._serve(False)

            def do_GET(self):
                self._serve(True)

            def _serve(self, body):
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                path = (query.get("p") or [""])[0]
                if (parsed.path != "/clip" or (query.get("t") or [""])[0] != token
                        or not (allowed(path) or path in extra) or not os.path.isfile(path)):
                    self.send_error(404)
                    return
                size = os.path.getsize(path)
                start, end, status = 0, size - 1, 200
                header = self.headers.get("Range", "")
                if header.startswith("bytes="):
                    first, _, last = header[6:].split(",")[0].partition("-")
                    try:
                        if first:
                            start = int(first)
                            end = min(int(last), size - 1) if last else size - 1
                        else:
                            start = max(0, size - int(last))
                        status = 206
                    except ValueError:
                        start, end, status = 0, size - 1, 200
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                self.send_response(status)
                self.send_header("Content-Type", TYPES.get(os.path.splitext(path)[1].lower(), "video/mp4"))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "no-store")
                if status == 206:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.end_headers()
                if not body:
                    return
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = f.read(min(256 * 1024, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (OSError, ConnectionError):
                    pass  # плеер перемотал или закрылся — это нормально

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # оборванные плеером соединения — не ошибка

        self._httpd = Quiet(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="media").start()

    def allow(self, path):
        """Разрешить отдавать ещё и этот файл (например, звуковую дорожку из кэша)."""
        self._extra.add(path)
        return self.url(path)

    def url(self, path):
        return "http://127.0.0.1:%d/clip?t=%s&p=%s" % (self.port, self.token, urllib.parse.quote(path, safe=""))
