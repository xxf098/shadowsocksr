#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import errno
import base64
import socket
import select
import logging
import argparse
import datetime
import threading
from collections import namedtuple
from struct import pack, unpack
import traceback
import signal

if os.name != 'nt':
    import resource

logger = logging.getLogger(__name__)

PY3 = sys.version_info[0] == 3
text_type = str
binary_type = bytes
version=b'1'
from urllib import parse as urlparse

from shadowsocks import eventloop, shell, lru_cache
STAGE_INIT = 0
STAGE_NEGOTIATE_0 = 1 # http
STAGE_NEGOTIATE_1 = 2 # socks5 1
STAGE_NEGOTIATE_2 = 3 # socks5 2
STAGE_STREAM = 4
STAGE_DESTROYED = -1

WAIT_STATUS_INIT = 0
WAIT_STATUS_READING = 1
WAIT_STATUS_WRITING = 2
WAIT_STATUS_READWRITING = WAIT_STATUS_READING | WAIT_STATUS_WRITING

STREAM_UP = 0
STREAM_DOWN = 1

BUF_SIZE = 32 * 1024

def text_(s, encoding='utf-8', errors='strict'):    # pragma: no cover
    """Utility to ensure text-like usability.

    If ``s`` is an instance of ``binary_type``, return
    ``s.decode(encoding, errors)``, otherwise return ``s``"""
    if isinstance(s, binary_type):
        return s.decode(encoding, errors)
    return s


def bytes_(s, encoding='utf-8', errors='strict'):   # pragma: no cover
    """Utility to ensure binary-like usability.

    If ``s`` is an instance of ``text_type``, return
    ``s.encode(encoding, errors)``, otherwise return ``s``"""
    if isinstance(s, text_type):
        return s.encode(encoding, errors)
    return s

CRLF, COLON, SP = b'\r\n', b':', b' '
PROXY_AGENT_HEADER = b'Proxy-agent: ssrforward v1'

PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT = CRLF.join([
    b'HTTP/1.1 200 Connection established',
    PROXY_AGENT_HEADER,
    CRLF
])

BAD_GATEWAY_RESPONSE_PKT = CRLF.join([
    b'HTTP/1.1 502 Bad Gateway',
    PROXY_AGENT_HEADER,
    b'Content-Length: 11',
    b'Connection: close',
    CRLF
]) + b'Bad Gateway'

PROXY_AUTHENTICATION_REQUIRED_RESPONSE_PKT = CRLF.join([
    b'HTTP/1.1 407 Proxy Authentication Required',
    PROXY_AGENT_HEADER,
    b'Content-Length: 29',
    b'Connection: close',
    b'Proxy-Authenticate: Basic',
    CRLF
]) + b'Proxy Authentication Required'

PAC_FILE_RESPONSE_PREFIX = CRLF.join([
    b'HTTP/1.1 200 OK',
    b'Content-Type: application/x-ns-proxy-autoconfig',
    b'Connection: close',
    CRLF
])

class ChunkParser(object):
    """HTTP chunked encoding response parser."""

    states = namedtuple('ChunkParserStates', (
        'WAITING_FOR_SIZE',
        'WAITING_FOR_DATA',
        'COMPLETE'
    ))(1, 2, 3)

    def __init__(self):
        self.state = ChunkParser.states.WAITING_FOR_SIZE
        self.body = b''     # Parsed chunks
        self.chunk = b''    # Partial chunk received
        self.size = None    # Expected size of next following chunk

    def parse(self, data):
        more = True if len(data) > 0 else False
        while more:
            more, data = self.process(data)

    def process(self, data):
        if self.state == ChunkParser.states.WAITING_FOR_SIZE:
            # Consume prior chunk in buffer
            # in case chunk size without CRLF was received
            data = self.chunk + data
            self.chunk = b''
            # Extract following chunk data size
            line, data = HttpParser.split(data)
            if not line:    # CRLF not received
                self.chunk = data
                data = b''
            else:
                self.size = int(line, 16)
                self.state = ChunkParser.states.WAITING_FOR_DATA
        elif self.state == ChunkParser.states.WAITING_FOR_DATA:
            remaining = self.size - len(self.chunk)
            self.chunk += data[:remaining]
            data = data[remaining:]
            if len(self.chunk) == self.size:
                data = data[len(CRLF):]
                self.body += self.chunk
                if self.size == 0:
                    self.state = ChunkParser.states.COMPLETE
                else:
                    self.state = ChunkParser.states.WAITING_FOR_SIZE
                self.chunk = b''
                self.size = None
        return len(data) > 0, data


class HttpParser(object):
    """HTTP request/response parser."""

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
        self.remote_address = None

    def is_chunked_encoded_response(self):
        return self.type == HttpParser.types.RESPONSE_PARSER and \
            b'transfer-encoding' in self.headers and \
            self.headers[b'transfer-encoding'][1].lower() == b'chunked'

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

# send receive data
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


class Server(Connection):
    """Establish connection to destination server."""

    def __init__(self, host, port):
        super(Server, self).__init__(b'server')
        self.addr = (host, int(port))

    def __del__(self):
        if self.conn:
            self.close()

    def connect(self):
        self.conn = socket.create_connection((self.addr[0], self.addr[1]))

class Socks5Server(Connection):

    def __init__(self, host, port):
        super(Socks5Server, self).__init__(b'server')
        self.addr = (host, int(port))

    def __del__(self):
        if self.conn:
            self.close()

    def connect(self, host, port):
        self.conn = socket.create_connection((self.addr[0], self.addr[1]))
        self.send(pack('3B', 5, 1, 0))
        response = self.recv(2)
        if response[0] == 0x05 and response[1] == 0xFF:
            raise Exception('Auth is required')
        elif response[0] != 0x05 or response[1] != 0x00:
            raise Exception('Fail to connect to sock5 server, invalid data')
        self.remote_addr = (host, port)
        #TODO: ATYP x03
        host_len = pack('!H', len(host))
        if (host_len[0] == 0):
            host_len = host_len.decode()[1].encode()
        port = int(port.decode()) if type(port) == bytes else port
        msg = pack('4B', 5, 1, 0, 3) + host_len + host + pack('!H', port)
        self.send(msg)
        response = self.recv(10)
        if (response[0:4] != b'\x05\x00\x00\x01'):
            raise Exception('Fail to connect to sock5 server')

class Client(Connection):
    """Accepted client connection."""

    def __init__(self, conn, addr):
        super(Client, self).__init__(b'client')
        self.conn = conn
        self.addr = addr


class ProxyError(Exception):
    pass


class ProxyConnectionFailed(ProxyError):

    def __init__(self, host, port, reason):
        self.host = host
        self.port = port
        self.reason = reason

    def __str__(self):
        return '<ProxyConnectionFailed - %s:%s - %s>' % (self.host, self.port, self.reason)


class ProxyAuthenticationFailed(ProxyError):
    pass

class Proxy(object):
    """HTTP proxy implementation.

    Accepts `Client` connection object and act as a proxy between client and server.
    """

    def __init__(self, client, auth_code=None, server_recvbuf_size=8192, client_recvbuf_size=8192, pac_file = None):
        super(Proxy, self).__init__()

        self.start_time = self._now()
        self.last_activity = self.start_time

        self.auth_code = auth_code
        self.client = client
        self.client_recvbuf_size = client_recvbuf_size
        self.server = None
        self.server_recvbuf_size = server_recvbuf_size

        self.request = HttpParser(HttpParser.types.REQUEST_PARSER)
        self.response = HttpParser(HttpParser.types.RESPONSE_PARSER)

        self.sock5_addr = ('127.0.0.1', 8088)

        self.pac_file = pac_file

        self._stage = STAGE_INIT

    @staticmethod
    def _now():
        return datetime.datetime.utcnow()

    def _inactive_for(self):
        return (self._now() - self.last_activity).seconds

    def _is_inactive(self, timeout_sec=30):
        return self._inactive_for() > timeout_sec

    # parse header or pipe data to server socket
    def _process_request(self, data):
        # once we have connection to the server
        # we don't parse the http request packets
        # any further, instead just pipe incoming
        # data from client to server
        # pipe data to server
        if self.server and not self.server.closed:
            self.server.queue(data)

    def _process_response(self, data):
        # parse incoming response packet
        # only for non-https requests
        if not self.request.method == b'CONNECT':
            # not run
            self.response.parse(data)

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

    def _get_waitable_lists(self):
        rlist, wlist, xlist = [self.client.conn], [], []
        if self.client.has_buffer():
            wlist.append(self.client.conn)
        if self.server and not self.server.closed:
            rlist.append(self.server.conn)
        if self.server and not self.server.closed and self.server.has_buffer():
            wlist.append(self.server.conn)
        return rlist, wlist, xlist

    # send all buffer data
    def _process_wlist(self, w):
        if self.client.conn in w:
            logger.debug('client is ready for writes, flushing client buffer')
            self.client.flush()

        if self.server and not self.server.closed and self.server.conn in w:
            logger.debug('server is ready for writes, flushing server buffer')
            self.server.flush()

    def _process_rlist(self, r):
        """Returns True if connection to client must be closed."""
        self.last_activity = self._now()
        if self.client.conn in r:
            logger.debug('client is ready for reads, reading')
            # b'CONNECT cn.bing.com:443 HTTP/1.1\r\nHost: cn.bing.com:443\r\nProxy-Connection: keep-alive\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Ubuntu Chromium/73.0.3683.75 Chrome/73.0.3683.75 Safari/537.36\r\n\r\n'
            # b'CONNECT api.ip.sb:443 HTTP/1.1\r\nHost: api.ip.sb:443\r\nProxy-Connection: keep-alive\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Ubuntu Chromium/73.0.3683.75 Chrome/73.0.3683.75 Safari/537.36\r\n\r\n'
            data = self.client.recv(self.client_recvbuf_size)

            if not data:
                logger.debug('client closed connection, breaking')
                return True

            try:
                # parse header
                # create self.server
                # or
                # pipe data to server socket
                return self._process_request(data)
            except (ProxyAuthenticationFailed, ProxyConnectionFailed) as e:
                logger.exception(e)
                self.client.queue(Proxy._get_response_pkt_by_exception(e))
                self.client.flush()
                return True

        if self.server and not self.server.closed and self.server.conn in r:
            logger.debug('server is ready for reads, reading')
            data = self.server.recv(self.server_recvbuf_size)

            if not data:
                logger.debug('server closed connection')
                self.server.close()
            else:
                # pipe data to client socket
                self._process_response(data)

        return False

    def _serve_pac_file(self):
        logger.debug('serving pac file')
        self.client.queue(PAC_FILE_RESPONSE_PREFIX)
        try:
            with open(self.pac_file) as f:
                for line in f:
                    self.client.queue(line)
        except IOError:
            logger.debug('serving pac file directly')
            self.client.queue(self.pac_file)

        self.client.flush()

    def _negotiate_http(self):
        data = self.client.recv(self.client_recvbuf_size)
        if not data:
            raise Exception('Fail to receive data from client')
        self.last_activity = self._now()
        self.request.parse(data)
        if self.request.state == HttpParser.states.COMPLETE:
            if self.request.method == b'CONNECT':
                host, port = self.request.url.path.split(COLON)
            elif self.request.url:
                host, port = self.request.url.hostname, self.request.url.port if self.request.url.port else 80
            else:
                raise Exception('Invalid request\n%s' % self.request.raw)
        return host, port

    def _negotiate_socks5(self, host, port):
        self.server = Socks5Server(*self.sock5_addr)
        try:
            logger.debug('connecting to server %s:%s' % (host, port))
            self.server.connect(host, port)
            logger.debug('connected to server %s:%s' % (host, port))
        except Exception as e:  # TimeoutError, socket.gaierror
            self.server.closed = True
            raise ProxyConnectionFailed(host, port, repr(e))

    def _negotiate(self):
        host, port = self._negotiate_http()
        self._negotiate_socks5(host, port)

        if self.request.method == b'CONNECT':
            # connection success then send to client
            # b'HTTP/1.1 200 Connection established\r\nProxy-agent: ssrforward v0.4\r\n\r\n'
            self.client.queue(PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT)
        # for usual http requests, re-build request packet
        # and queue for the server with appropriate headers
        else:
            self.server.queue(self.request.build(
                del_headers=[b'proxy-authorization', b'proxy-connection', b'connection', b'keep-alive'],
                add_headers=[(b'Via', b'1.1 ssforward v%s' % version), (b'Connection', b'Close')]
            ))

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

            if self._is_inactive(60):
                logger.warning('timeout reached, breaking')
                break

    @staticmethod
    def _get_response_pkt_by_exception(e):
        if e.__class__.__name__ == 'ProxyAuthenticationFailed':
            return PROXY_AUTHENTICATION_REQUIRED_RESPONSE_PKT
        if e.__class__.__name__ == 'ProxyConnectionFailed':
            return BAD_GATEWAY_RESPONSE_PKT
    ########
    # client send b'CONNECT cn.bing.com:443 HTTP/1.1\r\nHost: cn.bing.com:443\r\nProxy-Connection: keep-alive\r\n\r\n'
    # server send b'HTTP/1.1 200 Connection established\r\nProxy-agent: ssrforward v0.4\r\n\r\n'
    ########
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

class TCPRelayHandler(object):

    def __init__(self, server, fd_to_handlers, loop, local_sock, is_local):
        self._server = server
        self._fd_to_handlers = fd_to_handlers
        self._loop = loop
        self._local_sock = local_sock
        self._remote_sock = None
        self._local_sock_fd = None
        self._remote_sock_fd = None

        self._client_address = local_sock.getpeername()[:2]
        self._remote_address = ('127.0.0.1', 8088)

        self._data_to_write_to_local = []
        self._data_to_write_to_remote = []
        self._upstream_status = WAIT_STATUS_READING
        self._downstream_status = WAIT_STATUS_INIT

        self.request = HttpParser(HttpParser.types.REQUEST_PARSER)

        self._is_local = is_local
        self._recv_buffer_size = 8192
        self._update_activity()
        self._server.add_connection(1)
        self._add_ref = 1
        # self._server.stat_add(self._client_address[0], 1)
        local_sock.setblocking(False)
        local_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        self._local_sock_fd = local_sock.fileno()
        fd_to_handlers[self._local_sock_fd] = self
        loop.add(local_sock, eventloop.POLL_IN | eventloop.POLL_ERR, self._server)
        self._stage = STAGE_INIT

    def _update_activity(self, data_len=0):
        self._server.update_activity(self, data_len)

    def stage(self):
        return self._stage

    def handle_event(self, sock, fd, event):
        handle = False
        if self._stage == STAGE_DESTROYED:
            logger.debug('ignore handle_event: destroyed')
        handle = fd in(self._remote_sock_fd, self._local_sock_fd) and \
                event in (eventloop.POLL_IN, eventloop.POLL_OUT, eventloop.POLL_ERR, eventloop.POLL_HUP)
        if fd == self._remote_sock_fd:
            if event & eventloop.POLL_ERR:
                self._on_remote_error()
            elif event & (eventloop.POLL_IN | eventloop.POLL_HUP):
                self._on_remote_read()
            elif event & eventloop.POLL_OUT:
                self._on_remote_write()
        elif fd == self._local_sock_fd:
            if event & eventloop.POLL_ERR:
                self._on_local_error()
            elif event & (eventloop.POLL_IN | eventloop.POLL_HUP):
                self._on_local_read()
            elif event & eventloop.POLL_OUT:
                self._on_local_write()
        else:
            logger.warn('unknown socket from %s:%d' % (self._client_address[0], self._client_address[1]))
            try:
                self._loop.removefd(fd)
            except Exception as e:
                shell.print_exception(e)
            try:
                del self._fd_to_handlers[fd]
            except Exception as e:
                shell.print_exception(e)
            sock.close()
        return handle

    def _on_local_read(self):
        if not self._local_sock:
            return
        recv_buffer_size = BUF_SIZE
        data = None
        try:
            data = self._local_sock.recv(recv_buffer_size)
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        if self._stage == STAGE_INIT:
            self._handle_negotiate(data)
        if self._stage == STAGE_NEGOTIATE_0:
            pass
        if self._stage == STAGE_STREAM:
            self._write_to_sock(data, self._remote_sock)

    def _handle_negotiate(self, data):
        self._stage = STAGE_NEGOTIATE_0
        self.request.parse(data)
        if self.request.state == HttpParser.states.COMPLETE:
            if self.request.method == b'CONNECT':
                host, port = self.request.url.path.split(COLON)
            elif self.request.url:
                host, port = self.request.url.hostname, self.request.url.port if self.request.url.port else 80
            else:
                raise Exception('Invalid request\n%s' % self.request.raw)
        self.request.remote_address = (host, port)
        # socks5 server
        socks5_server = self._create_socks5_socket(*self._remote_address)

    def _create_socks5_socket(self, ip, port):
        addrs = socket.getaddrinfo(ip, port, 0, socket.SOCK_STREAM, socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("getaddrinfo failed for %s:%d" % (ip, port))
        af, socktype, proto, canonname, sa = addrs[0]
        remote_sock = socket.socket(af, socktype, proto)
        self._remote_sock = remote_sock
        self._remote_sock_fd = remote_sock.fileno()
        self._fd_to_handlers[self._remote_sock_fd] = self
        remote_sock.setblocking(False)
        remote_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        try:
            self._remote_sock.connect((ip, port))
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in (errno.EINPROGRESS,
                    errno.EWOULDBLOCK):
                pass # always goto here
            else:
                raise e
        addr, port = self._remote_sock.getsockname()[:2]
        self._loop.add(self._remote_sock,
                eventloop.POLL_ERR | eventloop.POLL_OUT,
                self._server)
        self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
        self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
        return remote_sock
    
    def _negotiate_socks5(self, host, port):
        if self._stage == STAGE_NEGOTIATE_0:
            self._write_to_sock(pack('3B', 5, 1, 0), self._remote_sock)
            self._stage = STAGE_NEGOTIATE_1
            return
        if self._stage == STAGE_NEGOTIATE_1:
            # self.remote_addr = (host, port)
            #TODO: ATYP x03
            host_len = pack('!H', len(host))
            if (host_len[0] == 0):
                host_len = host_len.decode()[1].encode()
            port = int(port.decode()) if type(port) == bytes else port
            msg = pack('4B', 5, 1, 0, 3) + host_len + host + pack('!H', port)
            self._write_to_sock(msg, self._remote_sock)
            self._stage = STAGE_NEGOTIATE_2
            return

    def _on_remote_read(self):
        if self._stage == STAGE_NEGOTIATE_1:
            response = self._remote_sock.recv(2)
            if response[0] == 0x05 and response[1] == 0xFF:
                raise Exception('Auth is required')
            elif response[0] != 0x05 or response[1] != 0x00:
                raise Exception('Fail to connect to sock5 server, invalid data')
            self._negotiate_socks5(*self.request.remote_address)
            return
        if self._stage == STAGE_NEGOTIATE_2:
            response = self._remote_sock.recv(10)
            if (response[0:4] != b'\x05\x00\x00\x01'):
                raise Exception('Fail to connect to sock5 server')
                
            if self.request.method == b'CONNECT':
                self._write_to_sock(PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT, self._local_sock)
            elif self.request.method == b'GET':
                self._write_to_sock(self.request.raw, self._remote_sock)
            else:
                self._write_to_sock(self.request.build(
                    del_headers=[b'proxy-authorization', b'proxy-connection', b'connection', b'keep-alive'],
                    add_headers=[(b'Via', b'1.1 ssforward v%s' % version), (b'Connection', b'Close')]
                ), self._local_sock)
            self._stage = STAGE_STREAM
            return
        if self._stage != STAGE_STREAM:
            return
        data = None
        try:
            data = self._remote_sock.recv(self._recv_buffer_size)
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK, 10035): #errno.WSAEWOULDBLOCK
                return
        if not data:
            self.destroy()
            return
        try:
            self._write_to_sock(data, self._local_sock)
        except Exception as e:
            shell.print_exception(e)
            logging.error("exception from %s:%d" % (self._client_address[0], self._client_address[1]))
            self.destroy()

    def _on_local_write(self):
        # handle local writable event
        if self._data_to_write_to_local:
            data = b''.join(self._data_to_write_to_local)
            self._data_to_write_to_local = []
            self._write_to_sock(data, self._local_sock)
        else:
            self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)

    def _on_local_error(self):
        if self._local_sock:
            err = eventloop.get_sock_error(self._local_sock)
            if err.errno not in [errno.ECONNRESET, errno.EPIPE]:
                logging.error(err)
                logging.error("local error, exception from %s:%d" % (self._client_address[0], self._client_address[1]))
        self.destroy()

    def _on_remote_write(self):
        # handle remote writable event
        if self._stage == STAGE_NEGOTIATE_0:
            self._negotiate_socks5(*self.request.remote_address)
            return
        if self._data_to_write_to_remote:
            data = b''.join(self._data_to_write_to_remote)
            self._data_to_write_to_remote = []
            self._write_to_sock(data, self._remote_sock)
        else:
            self._update_stream(STREAM_UP, WAIT_STATUS_READING)

    def _on_remote_error(self):
        if self._remote_sock:
            err = eventloop.get_sock_error(self._remote_sock)
            if err.errno not in [errno.ECONNRESET]:
                logging.error(err)
                if self._remote_address:
                    logging.error("remote error, when connect to %s:%d" % (self._remote_address[0], self._remote_address[1]))
                else:
                    logging.error("remote error, exception from %s:%d" % (self._client_address[0], self._client_address[1]))
        self.destroy()

    def _update_stream(self, stream, status):
        # update a stream to a new waiting status

        # check if status is changed
        # only update if dirty
        dirty = False
        if stream == STREAM_DOWN:
            if self._downstream_status != status:
                self._downstream_status = status
                dirty = True
        elif stream == STREAM_UP:
            if self._upstream_status != status:
                self._upstream_status = status
                dirty = True
        if dirty:
            if self._local_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                if self._upstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                self._loop.modify(self._local_sock, event)
            if self._remote_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                if self._upstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                self._loop.modify(self._remote_sock, event)
                # if self._remote_sock_v6:
                # self._loop.modify(self._remote_sock_v6, event)

    def _write_to_sock(self, data, sock):
        if not sock:
            return False
        uncomplete = False
        try:
            self._update_activity(len(data))
            if data:
                l = len(data)
                s = sock.send(data)
                if s < l:
                    data = data[s:]
                    uncomplete = True
            else:
                return
        except (OSError, IOError) as e:
            error_no = eventloop.errno_from_exception(e)
            if error_no in (errno.EAGAIN, errno.EINPROGRESS,
                            errno.EWOULDBLOCK):
                uncomplete = True
            else:
                #traceback.print_exc()
                shell.print_exception(e)
                logging.error("exception from %s:%d" % (self._client_address[0], self._client_address[1]))
                self.destroy()
                return False
        except Exception as e:
            shell.print_exception(e)
            logging.error("exception from %s:%d" % (self._client_address[0], self._client_address[1]))
            self.destroy()
            return False

        if uncomplete:
            if sock == self._local_sock:
                self._data_to_write_to_local.append(data)
                self._update_stream(STREAM_DOWN, WAIT_STATUS_WRITING)
            elif sock == self._remote_sock:
                self._data_to_write_to_remote.append(data)
                self._update_stream(STREAM_UP, WAIT_STATUS_WRITING)
            else:
                logging.error('write_all_to_sock:unknown socket from %s:%d' % (self._client_address[0], self._client_address[1]))
        else:
            if sock == self._local_sock:
                self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
            elif sock == self._remote_sock:
                self._update_stream(STREAM_UP, WAIT_STATUS_READING)
            else:
                logging.error('write_all_to_sock:unknown socket from %s:%d' % (self._client_address[0], self._client_address[1]))
        return True

    def destroy(self):
        if self._stage == STAGE_DESTROYED:
            # this couldn't happen
            logging.debug('already destroyed')
            return
        self._stage = STAGE_DESTROYED
        if self._local_sock:
            logging.debug('destroying local')
            try:
                self._loop.removefd(self._local_sock_fd)
            except Exception as e:
                shell.print_exception(e)
            try:
                if self._local_sock_fd is not None:
                    del self._fd_to_handlers[self._local_sock_fd]
            except Exception as e:
                shell.print_exception(e)
            self._local_sock.close()
            self._local_sock = None
        if self._add_ref > 0:
            self._server.add_connection(-1)
            # self._server.stat_add(self._client_address[0], -1)

class TCPRelay(object):
    """TCP server implementation.

    Subclass MUST implement `handle` method. It accepts an instance of accepted `Client` connection.
    """

    def __init__(self, hostname='127.0.0.1', port=9050, backlog=120):
        self.hostname = hostname
        self.port = port
        self.backlog = backlog
        self.socket = None
        self._eventloop = None
        self._closed = False
        self._server_socket = None
        self._server_socket_fd = None
        self._fd_to_handlers = {}
        self.server_connections = 0

        self._timeout = 300
        self._timeout_cache = lru_cache.LRUCache(timeout=self._timeout,
                                         close_callback=self._close_tcp_client)

    def handle(self, client):
        raise NotImplementedError()

    def run(self):
        try:
            logger.info('Starting server on port %d' % self.port)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.hostname, self.port))
            self.socket.listen(self.backlog)
            while True:
                # get socket connect with client
                # get local address
                conn, addr = self.socket.accept()
                client = Client(conn, addr)
                self.handle(client)
        except Exception as e:
            logger.exception('Exception while running the server %r' % e)
        finally:
            logger.info('Closing server socket')
            self.socket.close()
            # self.executor.shutdown(wait=False)

    def add_to_loop(self, loop):
        if self._eventloop:
            raise Exception('already add to loop')
        if self._closed:
            raise Exception('already closed')
        self._eventloop = loop
        self._eventloop.add(self._server_socket,
                            eventloop.POLL_IN | eventloop.POLL_ERR, self)
        self._eventloop.add_periodic(self.handle_periodic)

    def handle_event(self, sock, fd, event):
        raise NotImplementedError()

    def handle_periodic(self):
        if self._closed:
            if self._server_socket:
                self._eventloop.removefd(self._server_socket_fd)
                self._server_socket.close()
                self._server_socket = None
                logging.info('closed TCP port %d', self.port)
            for handler in list(self._fd_to_handlers.values()):
                handler.destroy()
        self._sweep_timeout()

    def _sweep_timeout(self):
        self._timeout_cache.sweep()

    def _close_tcp_client(self, client):
        if client.remote_address:
            logging.debug('timed out: %s:%d' %
                         client.remote_address)
        else:
            logging.debug('timed out')
        client.destroy()

    def add_connection(self, val):
        self.server_connections += val
        logger.debug('server port %5d connections = %d' % (self.port, self.server_connections,))

    def update_activity(self, client, data_len):
        self._timeout_cache[hash(client)] = client

class HTTP(TCPRelay):
    """HTTP proxy server implementation.

    Spawns new process to proxy accepted client connection.
    """

    def __init__(self, hostname='127.0.0.1', port=9050, backlog=100,
                 auth_code=None, server_recvbuf_size=8192, client_recvbuf_size=8192, pac_file=None):
        super(HTTP, self).__init__(hostname, port, backlog)
        self.auth_code = auth_code
        self.client_recvbuf_size = client_recvbuf_size
        self.server_recvbuf_size = server_recvbuf_size
        self.pac_file = pac_file
        # start http proxy server
        addrs = socket.getaddrinfo(hostname, port, 0,
                                   socket.SOCK_STREAM, socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("can't get addrinfo for %s:%d" %
                            (hostname, port))
        af, socktype, proto, canonname, sa = addrs[0]
        server_socket = socket.socket(af, socktype, proto)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(sa)
        server_socket.setblocking(False)
        server_socket.listen(1024)
        self._server_socket = server_socket
        self._server_socket_fd = server_socket.fileno()

    def handle(self, client):
        proxy = Proxy(client,
                      auth_code=self.auth_code,
                      server_recvbuf_size=self.server_recvbuf_size,
                      client_recvbuf_size=self.client_recvbuf_size,
                      pac_file=self.pac_file)
        proxy.daemon = True
        # proxy.start()
        # self.executor.submit(proxy.run)

    def handle_event(self, sock, fd, event):
        handle = False
        if sock:
            logger.info('fd {} {}'.format(fd, eventloop.EVENT_NAMES.get(event, event)))
        if sock == self._server_socket:
            if event & eventloop.POLL_ERR:
                raise Exception('server_socket error')
            handler = None
            handle = True
            try:
                logging.debug('accept')
                conn = self._server_socket.accept()
                handler = TCPRelayHandler(self, self._fd_to_handlers, self._eventloop, conn[0], True)
                if handler.stage() == STAGE_DESTROYED:
                    conn[0].close()
            except (OSError, IOError) as e:
                error_no = eventloop.errno_from_exception(e)
                if error_no in (errno.EAGAIN, errno.EINPROGRESS,
                                errno.EWOULDBLOCK):
                    return
                else:
                    traceback.print_exc()
                    if handler:
                        handler.destroy()
        else:
            # dispatch to TCPRelayHandler.handle_event
            if sock:
                handler = self._fd_to_handlers.get(fd, None)
                if handler:
                    handle = handler.handle_event(sock, fd, event)
                else:
                    logging.warn('unknown fd')
                    handle = True
                    try:
                        self._eventloop.removefd(fd)
                    except Exception as e:
                        traceback.print_exc()
                    sock.close()
            else:
                logging.warn('poll removed fd')
                handle = True
                if fd in self._fd_to_handlers:
                    try:
                        del self._fd_to_handlers[fd]
                    except Exception as e:
                        traceback.print_exc()
        return handle

    def close(self, next_tick=False):
        logging.debug('TCP close')
        self._closed = True
        if not next_tick:
            if self._eventloop:
                self._eventloop.remove_periodic(self.handle_periodic)
                self._eventloop.removefd(self._server_socket_fd)
            self._server_socket.close()
            for handler in list(self._fd_to_handlers.values()):
                handler.destroy()


def set_open_file_limit(soft_limit):
    """Configure open file description soft limit on supported OS."""
    if os.name != 'nt':  # resource module not available on Windows OS
        curr_soft_limit, curr_hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        if curr_soft_limit < soft_limit < curr_hard_limit:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, curr_hard_limit))
            logger.info('Open file descriptor soft limit set to %d' % soft_limit)


def main():
    parser = argparse.ArgumentParser(
        description='socks5 forward v1',
        epilog='issues'
    )

    parser.add_argument('--hostname', default='127.0.0.1', help='Default: 127.0.0.1')
    parser.add_argument('--port', default='9050', help='Default: 8899')
    parser.add_argument('--backlog', default='100', help='Default: 100. '
                                                         'Maximum number of pending connections to proxy server')
    parser.add_argument('--basic-auth', default=None, help='Default: No authentication. '
                                                           'Specify colon separated user:password '
                                                           'to enable basic authentication.')
    parser.add_argument('--server-recvbuf-size', default='8192', help='Default: 8 KB. '
                                                                      'Maximum amount of data received from the '
                                                                      'server in a single recv() operation. Bump this '
                                                                      'value for faster downloads at the expense of '
                                                                      'increased RAM.')
    parser.add_argument('--client-recvbuf-size', default='8192', help='Default: 8 KB. '
                                                                      'Maximum amount of data received from the '
                                                                      'client in a single recv() operation. Bump this '
                                                                      'value for faster uploads at the expense of '
                                                                      'increased RAM.')
    parser.add_argument('--open-file-limit', default='1024', help='Default: 1024. '
                                                                  'Maximum number of files (TCP connections) '
                                                                  'that ssrforward can open concurrently.')
    parser.add_argument('--log-level', default='WARNING', help='DEBUG, INFO (default), WARNING, ERROR, CRITICAL')
    parser.add_argument('--pac-file', default='', help='A file (Proxy Auto Configuration) or string to serve when '
                                                       'the server receives a direct file request.')
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s')
    logger.warning('Start http proxy 127.0.0.1:%s' % (args.port))
    try:
        set_open_file_limit(int(args.open_file_limit))

        auth_code = None
        if args.basic_auth:
            auth_code = b'Basic %s' % base64.b64encode(bytes_(args.basic_auth))
        http_server = HTTP(hostname=args.hostname,
                     port=int(args.port),
                     backlog=int(args.backlog),
                     auth_code=auth_code,
                     server_recvbuf_size=int(args.server_recvbuf_size),
                     client_recvbuf_size=int(args.client_recvbuf_size),
                     pac_file=args.pac_file)
        def handler(signum, _):
            logger.warning('received SIGQUIT, doing graceful shutting down..')
            http_server.close(next_tick=True)
        signal.signal(getattr(signal, 'SIGQUIT', signal.SIGTERM), handler)

        def int_handler(signum, _):
            sys.exit(1)
        signal.signal(signal.SIGINT, int_handler)

        loop = eventloop.EventLoop()
        http_server.add_to_loop(loop)
        loop.run()
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == '__main__':
    main()
