import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args):
        pass

@contextmanager
def server(cert=None, key=None):
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        http.socket = context.wrap_socket(http.socket, server_side=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield http.server_address[1]
    finally:
        http.shutdown()
        http.server_close()
        thread.join()

class ReleaseHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        # Generate a real test CA and a distinct server leaf certificate.
        # OpenSSL 3 / Python 3.14 rejects a non-CA certificate as a trust anchor.
        for name in ("trusted", "other"):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                            "-nodes", "-days", "1", "-subj", "/CN=Test " + name + " CA",
                            "-addext", "basicConstraints=critical,CA:TRUE",
                            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                            "-keyout", str(cls.root / (name + ".key")),
                            "-out", str(cls.root / (name + ".crt"))],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048",
                        "-nodes", "-subj", "/CN=management.example",
                        "-keyout", str(cls.root / "server.key"),
                        "-out", str(cls.root / "server.csr")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        extensions = cls.root / "server.ext"
        extensions.write_text("basicConstraints=critical,CA:FALSE\\n".replace("\\n", "\n") +
                              "keyUsage=critical,digitalSignature,keyEncipherment\n" +
                              "extendedKeyUsage=serverAuth\n" +
                              "subjectAltName=DNS:management.example,IP:127.0.0.1\n")
        subprocess.run(["openssl", "x509", "-req", "-in", str(cls.root / "server.csr"),
                        "-CA", str(cls.root / "trusted.crt"),
                        "-CAkey", str(cls.root / "trusted.key"),
                        "-CAcreateserial", "-days", "1", "-sha256",
                        "-extfile", str(extensions),
                        "-out", str(cls.root / "server.crt")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def probe(self, port, cert=None):
        args = ["uvicorn", "--port", str(port)]
        if cert:
            args += ["--ssl-certfile", str(cert)]
        scripts = list((Path(__file__).resolve().parents[1] / "deploy").glob("*release.sh"))
        self.assertTrue(scripts)
        for script in scripts:
            source = script.read_text()
            if "WEB_HEALTH_PY" not in source:
                self.fail("health helper missing: " + str(script))
            code = source.split("<<'WEB_HEALTH_PY'\n", 1)[1].split("\nWEB_HEALTH_PY", 1)[0]
            result = subprocess.run([sys.executable, "-", json.dumps(args)], input=code,
                                    text=True, capture_output=True)
            yield result.returncode

    def test_http_compatibility(self):
        with server() as port:
            self.assertTrue(all(code == 0 for code in self.probe(port)))

    def test_https_uses_configured_certificate(self):
        with server(self.root / "server.crt", self.root / "server.key") as port:
            self.assertTrue(all(code == 0 for code in self.probe(port, self.root / "trusted.crt")))

    def test_https_rejects_untrusted_certificate(self):
        with server(self.root / "server.crt", self.root / "server.key") as port:
            self.assertTrue(all(code != 0 for code in self.probe(port, self.root / "other.crt")))

if __name__ == "__main__":
    unittest.main()
