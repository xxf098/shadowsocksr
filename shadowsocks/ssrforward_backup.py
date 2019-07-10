import asyncio
import logging
import sys
import socket
from collections import namedtuple
from struct import pack, unpack
import datetime
import select

logger = logging.getLogger('ssrforward')
CRLF, COLON, SP = b'\r\n', b':', b' '
PROXY_AGENT_HEADER = b'Proxy-agent: socks5 forward v1'
PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT = CRLF.join([
    b'HTTP/1.1 200 Connection established',
    PROXY_AGENT_HEADER,
    CRLF
])

PY3 = sys.version_info[0] == 3
text_type = str
binary_type = bytes
from urllib import parse as urlparse
from concurrent.futures import ThreadPoolExecutor
executor = ThreadPoolExecutor(18)
default_buf_size=8192

def client_connection(reader, writer):
    client = Client(writer._transport._sock, writer.get_extra_info('peername'))
    proxy = Proxy(client)
    executor.submit(proxy.run)
class Proxy(object):

    def __init__(self, client):
        self.sock5_addr = ('127.0.0.1', 8088)
        self.request = HttpParser(HttpParser.types.REQUEST_PARSER)
        self.response = HttpParser(HttpParser.types.RESPONSE_PARSER)

        self.client = client
        self.client_recvbuf_size = default_buf_size
        self.server = None
        self.server_recvbuf_size = default_buf_size

        self.start_time = self._now()
        self.last_activity = self.start_time
    
    @staticmethod
    def _now():
        return datetime.datetime.utcnow()

    def _inactive_for(self):
        return (self._now() - self.last_activity).seconds
    
    def _is_inactive(self):
        return self._inactive_for() > 30    
    
    def _negotiate(self):
        data = self.client.recv(self.client_recvbuf_size)
        self.last_activity = self._now()
        self.request.parse(data)
        if self.request.state == HttpParser.states.COMPLETE:
            if self.request.method == b'CONNECT':
                host, port = self.request.url.path.split(COLON)
            elif self.request.url:
                host, port = self.request.url.hostname, self.request.url.port if self.request.url.port else 80
            else:
                raise Exception('Invalid request\n%s' % request.raw)

        self.server = Socks5Server(*self.sock5_addr)
        try:
            logger.debug('connecting to server %s:%s' % (host, port))
            self.server.connect(host, port)
            logger.debug('connected to server %s:%s' % (host, port))
        except Exception as e:  # TimeoutError, socket.gaierror
            self.server.closed = True
            raise ProxyConnectionFailed(host, port, repr(e)) 
        
        if self.request.method == b'CONNECT':
            # connection success then send to client
            # b'HTTP/1.1 200 Connection established\r\nProxy-agent: proxy.py v0.4\r\n\r\n'
            self.client.queue(PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT)
        # for usual http requests, re-build request packet
        # and queue for the server with appropriate headers
        else:
            self.server.queue(self.request.build(
                del_headers=[b'proxy-authorization', b'proxy-connection', b'connection', b'keep-alive'],
                add_headers=[(b'Via', b'1.1 ssforward v%s' % version), (b'Connection', b'Close')]
            ))              

    def run(self):
        logger.debug('Proxying connection %r' % self.client.conn)
        try:
            self._negotiate()
            self._process()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.exception('Exception while handling connection %r with reason %r' % (self.client.conn, e))
        finally:
            logger.debug(
                'closing client connection with pending client buffer size %d bytes' % self.client.buffer_size())
            self.client.close()
            if self.server:
                logger.debug(
                    'closed client connection with pending server buffer size %d bytes' % self.server.buffer_size())
            self._access_log()
            logger.debug('Closing proxy for connection %r at address %r' % (self.client.conn, self.client.addr))

    def _process(self):
        while True:
            rlist, wlist, xlist = self._get_waitable_lists()
            r, w, x = select.select(rlist, wlist, xlist, 1)

            self._process_wlist(w)
            if self._process_rlist(r):
                break
            if self.client.buffer_size() == 0:
                if self.response.state == HttpParser.states.COMPLETE:
                    logger.debug('client buffer is empty and response state is complete, breaking')
                    break
                if self._is_inactive():
                    logger.debug('client buffer is empty and maximum inactivity has reached, breaking')
                    break
    
    def _process_rlist(self, r):
        if self.client.conn in r:
            data = self.client.recv(self.client_recvbuf_size)
            self.last_activity = self._now()
            if not data:
                logger.debug('client closed connection, breaking')
                return True
            if self.server and not self.server.closed:
                self.server.queue(data)
                return False

        if self.server and not self.server.closed and self.server.conn in r:
            logger.debug('server is ready for reads, reading')
            data = self.server.recv(self.server_recvbuf_size)
            self.last_activity = self._now()

            if not data:
                logger.debug('server closed connection')
                self.server.close()
            else:
                # pipe data to client socket
                self._process_response(data)
        return False
            

    def _process_wlist(self, w):
        if self.client.conn in w:
            logger.debug('client is ready for writes, flushing client buffer')
            self.client.flush()

        if self.server and not self.server.closed and self.server.conn in w:
            logger.debug('server is ready for writes, flushing server buffer')
            self.server.flush()    

    def _get_waitable_lists(self):
        rlist, wlist, xlist = [self.client.conn], [], []
        if self.client.has_buffer():
            wlist.append(self.client.conn)
        if self.server and not self.server.closed:
            rlist.append(self.server.conn)
        if self.server and not self.server.closed and self.server.has_buffer():
            wlist.append(self.server.conn)
        return rlist, wlist, xlist

    def _process_response(self, data):
        # parse incoming response packet
        # only for non-https requests
        if not self.request.method == b'CONNECT':
            # not run
            # self.response.parse(data)
            raise NotImplementedError()

        # queue data for client
        self.client.queue(data)
    
    def _access_log(self):
        host, port = self.server.addr if self.server else (None, None)
        if self.request.method == b'CONNECT':
            logger.info(
                '%s:%s - %s %s:%s' % (self.client.addr[0], self.client.addr[1], self.request.method, host, port))
        elif self.request.method:
            logger.info('%s:%s - %s %s:%s%s - %s %s - %s bytes' % (
                self.client.addr[0], self.client.addr[1], self.request.method, host, port, self.request.build_url(),
                self.response.code, self.response.reason, len(self.response.raw)))

class Connection(object):
    """TCP server/client connection abstraction."""

    def __init__(self, what):
        self.conn = None
        self.buffer = b''
        self.closed = False
        self.what = what  # server or client

    def send(self, data):
        # TODO: Gracefully handle BrokenPipeError exceptions
        return self.conn.send(data)

    def recv(self, bufsiz=8192):
        try:
            data = self.conn.recv(bufsiz)
            if len(data) == 0:
                logger.debug('rcvd 0 bytes from %s' % self.what)
                return None
            logger.debug('rcvd %d bytes from %s' % (len(data), self.what))
            return data
        except Exception as e:
            if e.errno == errno.ECONNRESET:
                logger.debug('%r' % e)
            else:
                logger.exception(
                    'Exception while receiving from connection %s %r with reason %r' % (self.what, self.conn, e))
            return None

    def close(self):
        self.conn.close()
        self.closed = True

    def buffer_size(self):
        return len(self.buffer)

    def has_buffer(self):
        return self.buffer_size() > 0

    def queue(self, data):
        self.buffer += data

    def flush(self):
        sent = self.send(self.buffer)
        self.buffer = self.buffer[sent:]
        logger.debug('flushed %d bytes to %s' % (sent, self.what))

class Client(Connection):
    """Accepted client connection."""

    def __init__(self, conn, addr):
        super(Client, self).__init__(b'client')
        self.conn = conn
        self.addr = addr

class Socks5Server(Connection):

    def __init__(self, host, port):
        super(Socks5Server, self).__init__(b'server')
        self.addr = (host, int(port))

    def __del__(self):
        if self.conn:
            self.close()

    def connect(self, host, port):
        self.conn = socket.create_connection((self.addr[0], self.addr[1]))
        self.send(b'\x05\x01\x00')
        response = self.recv()
        if (response != b'\x05\x00'):
            raise Exception('Fail to connect to sock5 server')
        self.remote_addr = (host, port)
        #TODO: ATYP x03
        host_len = pack('!H', len(host))
        if (host_len[0] == 0):
            host_len = host_len.decode()[1].encode()
        port = int(port.decode()) if type(port) == bytes else port
        msg = host_len + host + pack('!H', port)
        msg = b'\x05\x01\x00\x03' + msg
        self.send(msg)
        response = self.recv()
        if (response[0:4] != b'\x05\x00\x00\x01'):
            raise Exception('Fail to connect to sock5 server')

class ChunkParser(object):

    def __init__(self):
        raise Exception('not implemented')

class HttpParser(object):

    states = namedtuple('HttpParserStates', (
    'INITIALIZED',
    'LINE_RCVD',
    'RCVING_HEADERS',
    'HEADERS_COMPLETE',
    'RCVING_BODY',
    'COMPLETE'))(1, 2, 3, 4, 5, 6)

    types = namedtuple('HttpParserTypes', (
        'REQUEST_PARSER',
        'RESPONSE_PARSER'
    ))(1, 2)

    def __init__(self, parser_type):
        assert parser_type in (HttpParser.types.REQUEST_PARSER, HttpParser.types.RESPONSE_PARSER)
        self.type = parser_type
        self.state = HttpParser.states.INITIALIZED

        self.raw = b''
        self.buffer = b''

        self.headers = dict()
        self.body = None

        self.method = None
        self.url = None
        self.code = None
        self.reason = None
        self.version = None

        self.chunk_parser = None
    
    def parse(self, data):
        self.raw += data
        data = self.buffer + data
        self.buffer = b''

        more = True if len(data) > 0 else False
        while more:
            more, data = self.process(data)
        self.buffer = data

    def process(self, data):
        if self.state in (HttpParser.states.HEADERS_COMPLETE,
                          HttpParser.states.RCVING_BODY,
                          HttpParser.states.COMPLETE) and \
                (self.method == b'POST' or self.type == HttpParser.types.RESPONSE_PARSER):
            if not self.body:
                self.body = b''

            if b'content-length' in self.headers:
                self.state = HttpParser.states.RCVING_BODY
                self.body += data
                if len(self.body) >= int(self.headers[b'content-length'][1]):
                    self.state = HttpParser.states.COMPLETE
            elif self.is_chunked_encoded_response():
                if not self.chunk_parser:
                    self.chunk_parser = ChunkParser()
                self.chunk_parser.parse(data)
                if self.chunk_parser.state == ChunkParser.states.COMPLETE:
                    self.body = self.chunk_parser.body
                    self.state = HttpParser.states.COMPLETE

            return False, b''

        line, data = HttpParser.split(data)
        if line is False:
            return line, data

        if self.state == HttpParser.states.INITIALIZED:
            # CONNECT google.com:443 HTTP/1.1
            self.process_line(line)
        elif self.state in (HttpParser.states.LINE_RCVD, HttpParser.states.RCVING_HEADERS):
            self.process_header(line)

        # When connect request is received without a following host header
        # See `TestHttpParser.test_connect_request_without_host_header_request_parse` for details
        if self.state == HttpParser.states.LINE_RCVD and \
                self.type == HttpParser.types.REQUEST_PARSER and \
                self.method == b'CONNECT' and \
                data == CRLF:
            self.state = HttpParser.states.COMPLETE

        # When raw request has ended with \r\n\r\n and no more http headers are expected
        # See `TestHttpParser.test_request_parse_without_content_length` and
        # `TestHttpParser.test_response_parse_without_content_length` for details
        elif self.state == HttpParser.states.HEADERS_COMPLETE and \
                self.type == HttpParser.types.REQUEST_PARSER and \
                self.method != b'POST' and \
                self.raw.endswith(CRLF * 2):
            self.state = HttpParser.states.COMPLETE
        elif self.state == HttpParser.states.HEADERS_COMPLETE and \
                self.type == HttpParser.types.REQUEST_PARSER and \
                self.method == b'POST' and \
                (b'content-length' not in self.headers or
                 (b'content-length' in self.headers and
                  int(self.headers[b'content-length'][1]) == 0)) and \
                self.raw.endswith(CRLF * 2):
            self.state = HttpParser.states.COMPLETE

        return len(data) > 0, data    

    def process_line(self, data):
        line = data.split(SP)
        if self.type == HttpParser.types.REQUEST_PARSER:
            self.method = line[0].upper()
            self.url = urlparse.urlsplit(line[1])
            self.version = line[2]
        else:
            self.version = line[0]
            self.code = line[1]
            self.reason = b' '.join(line[2:])
        self.state = HttpParser.states.LINE_RCVD

    # Proxy-Connection: keep-alive
    def process_header(self, data):
        if len(data) == 0:
            if self.state == HttpParser.states.RCVING_HEADERS:
                self.state = HttpParser.states.HEADERS_COMPLETE
            elif self.state == HttpParser.states.LINE_RCVD:
                self.state = HttpParser.states.RCVING_HEADERS
        else:
            self.state = HttpParser.states.RCVING_HEADERS
            parts = data.split(COLON)
            key = parts[0].strip()
            value = COLON.join(parts[1:]).strip()
            self.headers[key.lower()] = (key, value)

    def build_url(self):
        if not self.url:
            return b'/None'

        url = self.url.path
        if url == b'':
            url = b'/'
        if not self.url.query == b'':
            url += b'?' + self.url.query
        if not self.url.fragment == b'':
            url += b'#' + self.url.fragment
        return url

    def build(self, del_headers=None, add_headers=None):
        req = b' '.join([self.method, self.build_url(), self.version])
        req += CRLF

        if not del_headers:
            del_headers = []
        for k in self.headers:
            if k not in del_headers:
                req += self.build_header(self.headers[k][0], self.headers[k][1]) + CRLF

        if not add_headers:
            add_headers = []
        for k in add_headers:
            req += self.build_header(k[0], k[1]) + CRLF

        req += CRLF
        if self.body:
            req += self.body

        return req

    @staticmethod
    def build_header(k, v):
        return k + b': ' + v

    @staticmethod
    def split(data):
        pos = data.find(CRLF)
        if pos == -1:
            return False, data
        line = data[:pos]
        data = data[pos + len(CRLF):]
        return line, data

class ProxyConnectionFailed(Exception):

    def __init__(self, host, port, reason):
        self.host = host
        self.port = port
        self.reason = reason

    def __str__(self):
        return '<ProxyConnectionFailed - %s:%s - %s>' % (self.host, self.port, self.reason)

#TODO: polipo
#TODO: authcode
def main():
    host, port = '127.0.0.1', 9050
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(funcName)s:%(lineno)d - %(message)s')
    logger.info(f'Serving on {host}:{port}')
    loop = asyncio.get_event_loop()
    future = asyncio.start_server(client_connection,
                host=host,
                port=port,
                loop=loop)
    server = loop.run_until_complete(future)
    try:
        loop.run_forever()
    except KeyboardInterrupt as e:
        logger.info('Keyboard interrupted. Exit.')
    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()


if __name__ == '__main__':
    main()