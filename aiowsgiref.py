import asyncio
from io import BytesIO
import socket
import http.client
import email.parser
import sys
import traceback
from wsgiref import simple_server


MAX_CHUNK_SIZE = 32 * 1024 * 1024  # 32 MB


__all__ = ('WSGIServer', 'WSGIRequestHandler', 'run')


class ServerHandler(simple_server.ServerHandler, object):
    error_status = str("500 INTERNAL SERVER ERROR")

    def write(self, data):
        """'write()' callable as specified by PEP 3333"""

        assert isinstance(data, bytes), "write() argument must be bytestring"

        if not self.status:
            raise AssertionError("write() before start_response()")

        elif not self.headers_sent:
            # Before the first output, send the stored headers
            self.bytes_sent = len(data)    # make sure we know content-length
            self.send_headers()
        else:
            self.bytes_sent += len(data)

        # XXX check Content-Length and truncate if too many bytes written?
        data = BytesIO(data)
        for chunk in iter(lambda: data.read(MAX_CHUNK_SIZE), b''):
            self._write(chunk)

    def error_output(self, environ, start_response):
        super(ServerHandler, self).error_output(environ, start_response)
        return ['\n'.join(traceback.format_exception(*sys.exc_info()))]

    @asyncio.coroutine
    def run(self, application):
        """Invoke the application"""
        # Note to self: don't move the close()!  Asynchronous servers shouldn't
        # call close() from finish_response(), so if you close() anywhere but
        # the double-error branch here, you'll break asynchronous servers by
        # prematurely closing.  Async servers must return from 'run()' without
        # closing if there might still be output to iterate over.
        try:
            self.setup_environ()
            self.result = application(self.environ, self.start_response)
            if asyncio.iscoroutine(self.result):
                self.result = yield from self.result
            self.finish_response()
        except:
            try:
                self.handle_error()
            except:
                # If we get an error handling an error, just give up already!
                self.close()
                raise   # ...and let the actual server figure it out.


class Request(object):
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

        self.reader.close = lambda : print('close reader')

        writer.closed = False
        writer.flush = self.writer_flush

    def writer_flush(self):
        print('flush', self)

    def close(self):
        pass


class WSGIServer(simple_server.WSGIServer, object):
    """BaseHTTPServer that implements the Python WSGI protocol"""

    request_queue_size = 1024

    def __init__(self, *args, **kwargs):
        if kwargs.pop('ipv6', False):
            self.address_family = socket.AF_INET6
        super(WSGIServer, self).__init__(*args, **kwargs)
        self.socket.setblocking(0)
        self.loop = asyncio.get_event_loop()
        Request.server = self
        self.coro = asyncio.start_server(self.async_request, sock=self.socket)
        self.server = self.loop.run_until_complete(self.coro)

    @asyncio.coroutine
    def async_request(self, reader, writer):
        addr = writer.get_extra_info('peername')
        self.process_request(Request(reader, writer), addr)

    def server_bind(self):
        """Override server_bind to store the server name."""
        super(WSGIServer, self).server_bind()
        self.setup_environ()

    def serve_forever(self, poll_interval=0.5):
        self.loop.run_forever()

    def shutdown_request(self, request):
        """Called to shutdown and close an individual request."""
        self.close_request(request)


class WSGIRequestHandler(simple_server.WSGIRequestHandler, object):
    def __init__(self, *args, **kwargs):
        super(WSGIRequestHandler, self).__init__(*args, **kwargs)

    def setup(self):
        self.connection = self.request
        self.rfile = self.request.reader
        self.wfile = self.request.writer

    def handle(self):
        asyncio.async(self.async_handle())

    def finish(self):
        pass

    @asyncio.coroutine
    def async_handle(self):
        """Handle a single HTTP request"""

        self.raw_requestline = yield from self.rfile.readline()
        if len(self.raw_requestline) > 65536:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            self.send_error(414)
            return

        if not (yield from self.parse_request()):  # An error code has been sent, just exit
            return

        handler = ServerHandler(
            self.rfile, self.wfile, self.get_stderr(), self.get_environ()
        )
        handler.request_handler = self      # backpointer for logging
        yield from handler.run(self.server.get_app())

    @asyncio.coroutine
    def parse_request(self):
        """Parse a request (internal).

        The request should be stored in self.raw_requestline; the results
        are in self.command, self.path, self.request_version and
        self.headers.

        Return True for success, False for failure; on failure, an
        error is sent back.

        """
        self.command = None  # set in case of error on the first line
        self.request_version = version = self.default_request_version
        self.close_connection = 1
        requestline = str(self.raw_requestline, 'iso-8859-1')
        requestline = requestline.rstrip('\r\n')
        self.requestline = requestline
        words = requestline.split()
        if len(words) == 3:
            command, path, version = words
            if version[:5] != 'HTTP/':
                self.send_error(400, "Bad request version (%r)" % version)
                return False
            try:
                base_version_number = version.split('/', 1)[1]
                version_number = base_version_number.split(".")
                # RFC 2145 section 3.1 says there can be only one "." and
                #   - major and minor numbers MUST be treated as
                #      separate integers;
                #   - HTTP/2.4 is a lower version than HTTP/2.13, which in
                #      turn is lower than HTTP/12.3;
                #   - Leading zeros MUST be ignored by recipients.
                if len(version_number) != 2:
                    raise ValueError
                version_number = int(version_number[0]), int(version_number[1])
            except (ValueError, IndexError):
                self.send_error(400, "Bad request version (%r)" % version)
                return False
            if version_number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
                self.close_connection = 0
            if version_number >= (2, 0):
                self.send_error(505,
                          "Invalid HTTP Version (%s)" % base_version_number)
                return False
        elif len(words) == 2:
            command, path = words
            self.close_connection = 1
            if command != 'GET':
                self.send_error(400,
                                "Bad HTTP/0.9 request type (%r)" % command)
                return False
        elif not words:
            return False
        else:
            self.send_error(400, "Bad request syntax (%r)" % requestline)
            return False
        self.command, self.path, self.request_version = command, path, version

        # Examine the headers and look for a Connection directive.
        try:
            self.headers = yield from parse_headers(self.rfile, _class=self.MessageClass)
        except http.client.LineTooLong:
            self.send_error(400, "Line too long")
            return False

        conntype = self.headers.get('Connection', "")
        if conntype.lower() == 'close':
            self.close_connection = 1
        elif (conntype.lower() == 'keep-alive' and
              self.protocol_version >= "HTTP/1.1"):
            self.close_connection = 0
        # Examine the headers and look for an Expect directive
        expect = self.headers.get('Expect', "")
        if (expect.lower() == "100-continue" and
                self.protocol_version >= "HTTP/1.1" and
                self.request_version >= "HTTP/1.1"):
            if not self.handle_expect_100():
                return False
        return True

    def address_string(self):
        # Short-circuit parent method to not call socket.getfqdn
        return self.client_address


@asyncio.coroutine
def parse_headers(fp, _class=http.client.HTTPMessage):
    """Parses only RFC2822 headers from a file pointer.

    email Parser wants to see strings rather than bytes.
    But a TextIOWrapper around self.rfile would buffer too many bytes
    from the stream, bytes which we later need to read as bytes.
    So we read the correct bytes here, as bytes, for email Parser
    to parse.

    """
    headers = []
    while True:
        line = yield from fp.readline()
        if len(line) > http.client._MAXLINE:
            raise http.client.LineTooLong("header line")
        if len(headers) > http.client._MAXHEADERS:
            raise http.client.HTTPException("got more than %d headers" % http.client._MAXHEADERS)
        headers.append(line)
        if line in (b'\r\n', b'\n', b''):
            break
    hstring = b''.join(headers).decode('iso-8859-1')
    return email.parser.Parser(_class=_class).parsestr(hstring)


def run(addr, port, wsgi_handler, ipv6=False):
    server_address = (addr, port)
    httpd = WSGIServer(server_address, WSGIRequestHandler, ipv6=ipv6)
    httpd.set_app(wsgi_handler)
    httpd.serve_forever()


if __name__ == '__main__':
    @asyncio.coroutine
    def demo_app(environ, start_response):
        status = '200 OK' # HTTP Status
        headers = [('Content-type', 'text/plain')]  # HTTP Headers
        start_response(status, headers)
        return [b'Hello World']

    run('127.0.0.1', 8000, demo_app)
