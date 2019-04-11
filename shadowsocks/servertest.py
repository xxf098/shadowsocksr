import socket
import struct
import ipaddress
from urllib.parse import urlparse

def host_is_ip(host):
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return False
    else:
        return True

class Socks5Negotiator:
    def __init__(self, proxy):
        self.proxy = proxy
    
    def negotiate(self, dest_pair):
        cmd = b"\x01"
        self._request(cmd, dest_pair)    
    def _request(self, cmd, dst):
        try:
            self.proxy.send(struct.pack('3B', 5, 1, 0))
            self.proxy.writer.flush()
            chosen_auth = self.proxy.recv(2)
            if chosen_auth[0] != 0x05 or chosen_auth[1] != 0x00:
                raise Exception("SOCKS5 proxy server sent invalid data")
            self.proxy.send(b"\x05" + cmd + b"\x00")
            resolved = self._write_address(dst)
            resp = self.proxy.recv(3)
            if resp[0] != 0x05 or resp[1] != 0x00:
                raise Exception('SOCKS5 proxy server sent invalid data')
            bnd = self._read_address()
            return (resolved, bnd)
        finally:
            self.proxy.close()
    
    def _write_address(self, addr):
        host, port = addr
        family_to_byte = {socket.AF_INET: b"\x01", socket.AF_INET6: b"\x04"}
        addresses = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                            socket.SOCK_STREAM,
                            socket.IPPROTO_TCP,
                            socket.AI_ADDRCONFIG)
        target_addr = addresses[0]
        family = target_addr[0]
        host = target_addr[4][0]
        addr_bytes = socket.inet_pton(family, host)
        self.proxy.send(family_to_byte[family] + addr_bytes)
        host = socket.inet_ntop(family, addr_bytes)
        self.proxy.send(struct.pack(">H", port))
        self.proxy.writer.flush()
        return host, port

    def _read_address(self):       
        atyp = proxy.recv(1)
        if atyp == b"\x01":
            addr = socket.inet_ntoa(self.proxy.recv(4))
        elif atyp == b"\x03":
            length = self.proxy.recv(1)
            addr = self.proxy.recv(ord(length))
        elif atyp == b"\x04":
            addr = socket.inet_ntop(socket.AF_INET6, self.proxy.recv(16))
        else:
            raise Exception("SOCKS5 proxy server sent invalid data")
        port = struct.unpack(">H", self.proxy.recv(2))[0]
        return addr, port

# TODO: support https
class Proxy:
    def __init__(self, host=None, port=None, timeout=2):
        self.host = host
        if not host_is_ip(self.host):
            raise Exception('Not a IP Addrerss')
        self.port = int(port)
        if self.port > 65535:
            raise Exception('port is out of range')
        self.proxy_addr = (self.host, self.port)
        self.timeout = timeout
        self.negotiator = None
        self.reader = None
        self.writer = None
        self.sock = None
        self._closed = True

    def connect(self, dest_pair):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.proxy_addr)
        except socket.error as error:
            self.sock.close()
            raise error
        else:
            try:
                self.writer = self.sock.makefile("wb")
                self.reader = self.sock.makefile("rb", 0)
                Socks5Negotiator(self).negotiate(dest_pair)
            except Exception as error:
                self.close()
                raise error 
    def send(self, data):
        _data = data.encode() if not isinstance(data, bytes) else data
        self.writer.write(_data)    
    def recv(self, length=0):
        return self._readall(self.reader, length)    
    def _readall(self, file, count):
        data = b""
        while len(data) < count:
            d = file.read(count - len(data))
            if not d:
                raise Exception("Connection closed unexpectedly")
            data += d
        return data
    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.writer:
            self.writer.close()
        self.writer = None
        self.reader = None
        self.negotiator = None
    def testRequest(self):
        try:
            url = 'http://ip-api.com/json'
            uri= urlparse(url)
            port = 80 if uri.scheme == 'http' else 443
            self.connect((uri.netloc, 80))
            headers = """GET {} HTTP/1.0\r\n
    Host:{}\r\n""".format(url, uri.netloc)
            self.sock.sendall(headers.encode('utf-8'))
            resp = self.sock.recv(4096)
            return int(resp[9:12]) == 200
        except Exception as e:
            return False

if __name__ == "__main__":
#     headers = """GET http://ip-api.com/json HTTP/1.1\r\n
# Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8\r\n
# host: ip-api.com\r\n"""
    proxy = Proxy('127.0.0.1', 8088)
    resp = proxy.testRequest()
    print(resp)