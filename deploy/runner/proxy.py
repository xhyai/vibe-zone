"""Build-only CONNECT proxy. Resolve once and reject non-public destinations."""
import ipaddress
import os
import select
import socket
import socketserver

ALLOWED = {'github.com', 'gitlab.com', 'api.github.com', 'codeload.github.com', 'objects.githubusercontent.com',
           'raw.githubusercontent.com', 'ghcr.io', 'pkg-containers.githubusercontent.com',
           'registry-1.docker.io', 'auth.docker.io', 'production.cloudflare.docker.com', 'production.cloudfront.docker.com',
           'docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com',
           'pypi.org', 'files.pythonhosted.org', 'dl-cdn.alpinelinux.org', 'proxy.golang.org',
           'sum.golang.org', 'storage.googleapis.com', 'registry.npmjs.org'}
# storage.googleapis.com is a dependency CDN for proxy.golang.org, not a deployed cloud service.
ALLOWED.update(filter(None, os.environ.get('BUILD_ALLOWED_HOSTS', '').split(',')))


class Proxy(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self):
        upstream = None
        try:
            line = self.rfile.readline(4097)
            if len(line) > 4096:
                raise ValueError('Request too long')
            method, target, _ = line.decode('ascii').strip().split(' ')
            total = 0
            while True:
                header = self.rfile.readline(4097)
                total += len(header)
                if total > 16384 or not header:
                    raise ValueError('Invalid headers')
                if header == b'\r\n':
                    break
            host, port = target.rsplit(':', 1)
            if method != 'CONNECT' or port != '443' or host.lower() not in ALLOWED:
                raise ValueError('Destination is not allowed')
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError('Non-public destination')
            family, kind, protocol, _, endpoint = addresses[0]
            upstream = socket.socket(family, kind, protocol)
            upstream.settimeout(15)
            upstream.connect(endpoint)
            self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            self.wfile.flush()
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 30)
                if not ready:
                    break
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
        except (ValueError, OSError, UnicodeError):
            try:
                self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
        finally:
            if upstream:
                upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32


if __name__ == '__main__':
    with Server(('0.0.0.0', 3128), Proxy) as server:
        server.serve_forever()
