#!/usr/bin/env python3
"""
KEEP_ALIVE — envoltorio para desplegar telegram_listener.py en Render (free)
Mismo patrón que el resto de los servicios de Ridgecrest. Se lanza con -u
(salida sin buffer) para que los logs aparezcan en tiempo real.
"""
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_last_restart_count = 0
_lock = threading.Lock()


def build_cmd():
    return [sys.executable, "-u", "telegram_listener.py"]


def run_forever():
    global _last_restart_count
    cmd = build_cmd()
    print(f"[keep_alive] Lanzando: {' '.join(cmd)}", flush=True)
    while True:
        proc = subprocess.Popen(cmd)
        proc.wait()
        with _lock:
            _last_restart_count += 1
        print(
            f"[keep_alive] telegram_listener.py terminó con código "
            f"{proc.returncode}. Reiniciando en 10s (reinicio #{_last_restart_count})...",
            flush=True,
        )
        time.sleep(10)


class PingHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        with _lock:
            restarts = _last_restart_count
        body = (
            f"ridgecrest-telegram-listener activo\n"
            f"reinicios: {restarts}\n"
            f"hora UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    thread = threading.Thread(target=run_forever, daemon=True)
    thread.start()

    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    print(f"[keep_alive] Servidor de ping escuchando en 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
