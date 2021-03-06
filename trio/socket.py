from functools import wraps as _wraps, partial as _partial
import socket as _stdlib_socket
import sys as _sys
import os as _os
from contextlib import contextmanager as _contextmanager
import errno as _errno

import idna

from . import _core
from ._threads import run_in_worker_thread as _run_in_worker_thread

__all__ = []

################################################################
# misc utilities
################################################################

def _reexport(name):
    globals()[name] = getattr(_stdlib_socket, name)
    __all__.append(name)


# Usage:
#   async with _try_sync():
#       return sync_call_that_might_fail_with_exception()
#   # we only get here if the sync call in fact did fail with an exception
#   # that passed the blocking_exc_check
#   return await do_it_properly_with_a_check_point()

def _is_blocking_io_error(exc):
    return isinstance(exc, BlockingIOError)

class _try_sync:
    def __init__(self, blocking_exc_check=_is_blocking_io_error):
        self._blocking_exc_check = blocking_exc_check

    async def __aenter__(self):
        await _core.yield_if_cancelled()

    async def __aexit__(self, etype, value, tb):
        if value is not None and self._blocking_exc_check(value):
            # discard the exception and fall through to the code below the
            # block
            return True
        else:
            await _core.yield_briefly_no_cancel()
            # Let the return or exception propagate


################################################################
# CONSTANTS
################################################################

# Hopefully will show up in 3.7:
#   https://github.com/python/cpython/pull/477
if not hasattr(_stdlib_socket, "TCP_NOTSENT_LOWAT"):  # pragma: no branch
    if _sys.platform == "darwin":
        TCP_NOTSENT_LOWAT = 0x201
    elif _sys.platform == "linux":
        TCP_NOTSENT_LOWAT = 25

for _name in _stdlib_socket.__dict__.keys():
    if _name == _name.upper():
        _reexport(_name)

if _sys.platform == "win32":
    # See https://github.com/python-trio/trio/issues/39
    # (you can still get it from stdlib socket, of course, if you want it)
    del SO_REUSEADDR

    # As of at least 3.6, python on Windows is missing IPPROTO_IPV6
    # https://bugs.python.org/issue29515
    if not hasattr(_stdlib_socket, "IPPROTO_IPV6"):  # pragma: no branch
        IPPROTO_IPV6 = 41


################################################################
# Simple re-exports
################################################################

for _name in [
        "gaierror", "herror", "gethostname", "getprotobyname", "ntohs",
        "htonl", "htons", "inet_aton", "inet_ntoa",
        "inet_pton", "inet_ntop", "sethostname", "if_nameindex",
        "if_nametoindex", "if_indextoname",
        ]:
    if hasattr(_stdlib_socket, _name):
        _reexport(_name)


################################################################
# getaddrinfo and friends
################################################################

_NUMERIC_ONLY = _stdlib_socket.AI_NUMERICHOST | _stdlib_socket.AI_NUMERICSERV

async def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Look up a numeric address given a name.

    Arguments and return values are identical to :func:`socket.getaddrinfo`,
    except that this version is async.

    Also, :func:`trio.socket.getaddrinfo` correctly uses IDNA 2008 to process
    non-ASCII domain names. (:func:`socket.getaddrinfo` uses IDNA 2003, which
    can give the wrong result in some cases and cause you to connect to a
    different host than the one you intended; see `bpo-17305
    <https://bugs.python.org/issue17305>`__.)

    """

    # If host and port are numeric, then getaddrinfo doesn't block and we can
    # skip the whole thread thing, which seems worthwhile. So we try first
    # with the _NUMERIC_ONLY flags set, and then only spawn a thread if that
    # fails with EAI_NONAME:
    def numeric_only_failure(exc):
        return isinstance(exc, gaierror) and exc.errno == EAI_NONAME
    async with _try_sync(numeric_only_failure):
        return _stdlib_socket.getaddrinfo(
            host, port, family, type, proto, flags | _NUMERIC_ONLY)
    # That failed; it's a real hostname. We better use a thread.
    #
    # Also, it might be a unicode hostname, in which case we want to do our
    # own encoding using the idna module, rather than letting Python do
    # it. (Python will use the old IDNA 2003 standard, and possibly get the
    # wrong answer - see bpo-17305). However, the idna module is picky, and
    # will refuse to process some valid hostname strings, like "::1". So if
    # it's already ascii, we pass it through; otherwise, we encode it to.
    if isinstance(host, str):
        try:
            host = host.encode("ascii")
        except UnicodeEncodeError:
            # UTS-46 defines various normalizations; in particular, by default
            # idna.encode will error out if the hostname has Capital Letters
            # in it; with uts46=True it will lowercase them instead.
            host = idna.encode(host, uts46=True)
    return await _run_in_worker_thread(
        _stdlib_socket.getaddrinfo, host, port, family, type, proto, flags,
        cancellable=True)

__all__.append("getaddrinfo")


async def getnameinfo(sockaddr, flags):
    """Look up a name given a numeric address.

    Arguments and return values are identical to :func:`socket.getnameinfo`,
    except that this version is async.

    """
    return await _run_in_worker_thread(
        _stdlib_socket.getnameinfo, sockaddr, flags, cancellable=True)

__all__.append("getnameinfo")


async def getprotobyname(name):
    """Look up a protocol number by name. (Rarely used.)

    Like :func:`socket.getprotobyname`, but async.

    """
    return await _run_in_worker_thread(
        _stdlib_socket.getprotobyname, name, cancellable=True)

__all__.append("getprotobyname")

# obsolete gethostbyname etc. intentionally omitted


################################################################
# Socket "constructors"
################################################################

def from_stdlib_socket(sock):
    """Convert a standard library :func:`socket.socket` into a trio socket.

    """
    return SocketType(sock)
__all__.append("from_stdlib_socket")

@_wraps(_stdlib_socket.fromfd, assigned=(), updated=())
def fromfd(*args, **kwargs):
    return from_stdlib_socket(_stdlib_socket.fromfd(*args, **kwargs))
__all__.append("fromfd")

if hasattr(_stdlib_socket, "fromshare"):
    @_wraps(_stdlib_socket.fromshare, assigned=(), updated=())
    def fromshare(*args, **kwargs):
        return from_stdlib_socket(_stdlib_socket.fromshare(*args, **kwargs))
    __all__.append("fromshare")

@_wraps(_stdlib_socket.socketpair, assigned=(), updated=())
def socketpair(*args, **kwargs):
    return tuple(
        from_stdlib_socket(s)
        for s in _stdlib_socket.socketpair(*args, **kwargs))
__all__.append("socketpair")

@_wraps(_stdlib_socket.socket, assigned=(), updated=())
def socket(*args, **kwargs):
    return from_stdlib_socket(_stdlib_socket.socket(*args, **kwargs))
__all__.append("socket")


################################################################
# SocketType
################################################################

# sock.type gets weird stuff set in it, in particular on Linux:
#
#   https://bugs.python.org/issue21327
#
# But on other platforms (e.g. Windows) SOCK_NONBLOCK and SOCK_CLOEXEC aren't
# even defined. To recover the actual socket type (e.g. SOCK_STREAM) from a
# socket.type attribute, mask with this:
_SOCK_TYPE_MASK = ~(
    getattr(_stdlib_socket, "SOCK_NONBLOCK", 0)
    | getattr(_stdlib_socket, "SOCK_CLOEXEC", 0))

class SocketType:
    def __init__(self, sock):
        if type(sock) is not _stdlib_socket.socket:
            # For example, ssl.SSLSocket subclasses socket.socket, but we
            # certainly don't want to blindly wrap one of those.
            raise TypeError(
                "expected object of type 'socket.socket', not '{}"
                .format(type(sock).__name__))
        self._sock = sock
        self._sock.setblocking(False)
        self._did_SHUT_WR = False

        # Hopefully Python will eventually make something like this public
        # (see bpo-21327) but I don't want to make it public myself and then
        # find out they picked a different name... this is used internally in
        # this file and also elsewhere in trio.
        self._real_type = sock.type & _SOCK_TYPE_MASK

        # Defaults:
        if self._sock.family == AF_INET6:
            try:
                self.setsockopt(IPPROTO_IPV6, IPV6_V6ONLY, False)
            except OSError:
                pass

        # See https://github.com/python-trio/trio/issues/39
        if _sys.platform == "win32":
            self.setsockopt(SOL_SOCKET, _stdlib_socket.SO_REUSEADDR, False)
            self.setsockopt(SOL_SOCKET, SO_EXCLUSIVEADDRUSE, True)
        else:
            self.setsockopt(SOL_SOCKET, SO_REUSEADDR, True)

    ################################################################
    # Simple + portable methods and attributes
    ################################################################

    # NB this doesn't work because for loops don't create a scope
    # for _name in [
    #         ]:
    #     _meth = getattr(_stdlib_socket.socket, _name)
    #     @_wraps(_meth, assigned=("__name__", "__doc__"), updated=())
    #     def _wrapped(self, *args, **kwargs):
    #         return getattr(self._sock, _meth)(*args, **kwargs)
    #     locals()[_meth] = _wrapped
    # del _name, _meth, _wrapped

    _forward = {
        "detach", "get_inheritable", "set_inheritable", "fileno",
        "getpeername", "getsockname", "getsockopt", "setsockopt", "listen",
        "close", "share",
    }
    def __getattr__(self, name):
        if name in self._forward:
            return getattr(self._sock, name)
        raise AttributeError(name)

    def __dir__(self):
        return super().__dir__() + list(self._forward)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._sock.__exit__(*exc_info)

    @property
    def family(self):
        return self._sock.family

    @property
    def type(self):
        return self._sock.type

    @property
    def proto(self):
        return self._sock.proto

    def __repr__(self):
        return repr(self._sock).replace("socket.socket", "trio.socket.socket")

    def dup(self):
        """Same as :meth:`socket.socket.dup`.

        """
        return SocketType(self._sock.dup())

    def bind(self, address):
        """Bind this socket to the given address.

        Unlike the stdlib :meth:`~socket.socket.connect`, this method requires
        a pre-resolved address. See :meth:`resolve_local_address`.

        """
        self._check_address(address, require_resolved=True)
        return self._sock.bind(address)

    def shutdown(self, flag):
        # no need to worry about return value b/c always returns None:
        self._sock.shutdown(flag)
        # only do this if the call succeeded:
        if flag in [SHUT_WR, SHUT_RDWR]:
            self._did_SHUT_WR = True

    async def wait_writable(self):
        await _core.wait_socket_writable(self._sock)

    ################################################################
    # Address handling
    ################################################################

    # For socket operations that take addresses, Python helpfully accepts
    # addresses containing names, and implicitly resolves them. This is no
    # good, because the implicit resolution is blocking. We require that all
    # such addresses be "pre-resolved" meaning:
    #
    # - For AF_INET or AF_INET6, they must contain only numeric elements. We
    #   check using getaddrinfo with AI_NUMERIC{HOST,SERV} flags set.
    # - For other families, we cross our fingers and hope the user knows what
    #   they're doing.
    #
    # And we provide two convenience functions to do this "pre-resolution",
    # which attempt to match what Python does.

    def _check_address(self, address, *, require_resolved):
        if self._sock.family == AF_INET:
            if not isinstance(address, tuple) or not len(address) == 2:
                raise ValueError("address should be a (host, port) tuple")
        elif self._sock.family == AF_INET6:
            if not isinstance(address, tuple) or not 2 <= len(address) <= 4:
                raise ValueError(
                    "address should be a (host, port, [flowinfo, [scopeid]]) "
                    "tuple")
        else:
            return
        if require_resolved:  # for AF_INET{,6} only
            try:
                _stdlib_socket.getaddrinfo(
                    address[0], address[1],
                    self._sock.family,
                    self._real_type,
                    self._sock.proto,
                    flags=_NUMERIC_ONLY)
            except gaierror as exc:
                if exc.errno == _stdlib_socket.EAI_NONAME:
                    raise ValueError(
                        "expected an already-resolved numeric address, not {}"
                        .format(address))
                else:
                    raise

    # Take an address in Python's representation, and returns a new address in
    # the same representation, but with names resolved to numbers,
    # etc.
    async def _resolve_address(self, address, flags):
        await _core.yield_if_cancelled()
        try:
            self._check_address(address, require_resolved=False)
        except:
            await _core.yield_briefly_no_cancel()
            raise
        if self._sock.family not in (AF_INET, AF_INET6):
            await _core.yield_briefly_no_cancel()
            return address
        # Since we always pass in an explicit family here, AI_ADDRCONFIG
        # doesn't add any value -- if we have no ipv6 connectivity and are
        # working with an ipv6 socket, then things will break soon enough! And
        # if we do enable it, then it makes it impossible to even run tests
        # for ipv6 address resolution on travis-ci, which as of 2017-03-07 has
        # no ipv6.
        # flags |= AI_ADDRCONFIG
        if self._sock.family == AF_INET6:
            if not self._sock.getsockopt(IPPROTO_IPV6, IPV6_V6ONLY):
                flags |= AI_V4MAPPED
        gai_res = await getaddrinfo(
            address[0], address[1],
            self._sock.family,
            self._real_type,
            self._sock.proto,
            flags)
        # AFAICT from the spec it's not possible for getaddrinfo to return an
        # empty list.
        assert len(gai_res) >= 1
        # Address is the last item in the first entry
        (*_, normed), *_ = gai_res
        # The above ignored any flowid and scopeid in the passed-in address,
        # so restore them if present:
        if self._sock.family == AF_INET6:
            normed = list(normed)
            assert len(normed) == 4
            if len(address) >= 3:
                normed[2] = address[2]
            if len(address) >= 4:
                normed[3] = address[3]
            normed = tuple(normed)
        # Should never fail:
        self._check_address(normed, require_resolved=True)
        return normed

    # Returns something appropriate to pass to bind()
    async def resolve_local_address(self, address):
        """Resolve the given address into a numeric address suitable for
        passing to :meth:`bind`.

        This performs the same address resolution that the standard library
        :meth:`~socket.socket.bind` call would do, taking into account the
        current socket's settings (e.g. if this is an IPv6 socket then it
        returns IPv6 addresses). In particular, a hostname of ``None`` is
        mapped to the wildcard address.

        """
        return await self._resolve_address(address, AI_PASSIVE)

    # Returns something appropriate to pass to connect()/sendto()/sendmsg()
    async def resolve_remote_address(self, address):
        """Resolve the given address into a numeric address suitable for
        passing to :meth:`connect` or similar.

        This performs the same address resolution that the standard library
        :meth:`~socket.socket.connect` call would do, taking into account the
        current socket's settings (e.g. if this is an IPv6 socket then it
        returns IPv6 addresses). In particular, a hostname of ``None`` is
        mapped to the localhost address.

        """
        return await self._resolve_address(address, 0)

    async def _nonblocking_helper(self, fn, args, kwargs, wait_fn):
        # We have to reconcile two conflicting goals:
        # - We want to make it look like we always blocked in doing these
        #   operations. The obvious way is to always do an IO wait before
        #   calling the function.
        # - But, we also want to provide the correct semantics, and part
        #   of that means giving correct errors. So, for example, if you
        #   haven't called .listen(), then .accept() raises an error
        #   immediately. But in this same circumstance, then on MacOS, the
        #   socket does not register as readable. So if we block waiting
        #   for read *before* we call accept, then we'll be waiting
        #   forever instead of properly raising an error. (On Linux,
        #   interestingly, AFAICT a socket that can't possible read/write
        #   *does* count as readable/writable for select() purposes. But
        #   not on MacOS.)
        #
        # So, we have to call the function once, with the appropriate
        # cancellation/yielding sandwich if it succeeds, and if it gives
        # BlockingIOError *then* we fall back to IO wait.
        #
        # XX think if this can be combined with the similar logic for IOCP
        # submission...
        async with _try_sync():
            return fn(self._sock, *args, **kwargs)
        # First attempt raised BlockingIOError:
        while True:
            await wait_fn(self._sock)
            try:
                return fn(self._sock, *args, **kwargs)
            except BlockingIOError:
                pass

    def _make_simple_sock_method_wrapper(methname, wait_fn, maybe_avail=False):
        fn = getattr(_stdlib_socket.socket, methname)
        @_wraps(fn, assigned=("__name__",), updated=())
        async def wrapper(self, *args, **kwargs):
            return await self._nonblocking_helper(fn, args, kwargs, wait_fn)
        wrapper.__doc__ = (
            """Like :meth:`socket.socket.{}`, but async.

            """.format(methname))
        if maybe_avail:
            wrapper.__doc__ += (
                "Only available on platforms where :meth:`socket.socket.{}` "
                "is available.".format(methname))
        return wrapper

    ################################################################
    # accept
    ################################################################

    _accept = _make_simple_sock_method_wrapper(
        "accept", _core.wait_socket_readable)

    async def accept(self):
        """Like :meth:`socket.socket.accept`, but async.

        """
        sock, addr = await self._accept()
        return from_stdlib_socket(sock), addr

    ################################################################
    # connect
    ################################################################

    async def connect(self, address):
        """Connect the socket to a remote address.

        Similar to :meth:`socket.socket.connect`, except async and requiring a
        pre-resolved address. See :meth:`resolve_remote_address`.

        .. warning::

           Due to limitations of the underlying operating system APIs, it is
           not always possible to properly cancel a connection attempt once it
           has begun. If :meth:`connect` is cancelled, and is unable to
           abort the connection attempt, then it will:

           1. forcibly close the socket to prevent accidental re-use
           2. raise :exc:`~trio.Cancelled`.

           tl;dr: if :meth:`connect` is cancelled then you should throw away
           that socket and make a new one.

        """
        # nonblocking connect is weird -- you call it to start things
        # off, then the socket becomes writable as a completion
        # notification. This means it isn't really cancellable...
        async with _try_sync():
            self._check_address(address, require_resolved=True)
            # An interesting puzzle: can a non-blocking connect() return EINTR
            # (= raise InterruptedError)? PEP 475 specifically left this as
            # the one place where it lets an InterruptedError escape instead
            # of automatically retrying. This is based on the idea that EINTR
            # from connect means that the connection was already started, and
            # will continue in the background. For a blocking connect, this
            # sort of makes sense: if it returns EINTR then the connection
            # attempt is continuing in the background, and on many system you
            # can't then call connect() again because there is already a
            # connect happening. See:
            #
            #   http://www.madore.org/~david/computers/connect-intr.html
            #
            # For a non-blocking connect, it doesn't make as much sense --
            # surely the interrupt didn't happen after we successfully
            # initiated the connect and are just waiting for it to complete,
            # because a non-blocking connect does not wait! And the spec
            # describes the interaction between EINTR/blocking connect, but
            # doesn't have anything useful to say about non-blocking connect:
            #
            #   http://pubs.opengroup.org/onlinepubs/007904975/functions/connect.html
            #
            # So we have a conundrum: if EINTR means that the connect() hasn't
            # happened (like it does for essentially every other syscall),
            # then InterruptedError should be caught and retried. If EINTR
            # means that the connect() has successfully started, then
            # InterruptedError should be caught and ignored. Which should we
            # do?
            #
            # In practice, the resolution is probably that non-blocking
            # connect simply never returns EINTR, so the question of how to
            # handle it is moot.  Someone spelunked MacOS/FreeBSD and
            # confirmed this is true there:
            #
            #   https://stackoverflow.com/questions/14134440/eintr-and-non-blocking-calls
            #
            # and exarkun seems to think it's true in general of non-blocking
            # calls:
            #
            #   https://twistedmatrix.com/pipermail/twisted-python/2010-September/022864.html
            # (and indeed, AFAICT twisted doesn't try to handle
            # InterruptedError).
            #
            # So we don't try to catch InterruptedError. This way if it
            # happens, someone will hopefully tell us, and then hopefully we
            # can investigate their system to figure out what its semantics
            # are.
            return self._sock.connect(address)
        # It raised BlockingIOError, meaning that it's started the
        # connection attempt. We wait for it to complete:
        try:
            await _core.wait_socket_writable(self._sock)
        except _core.Cancelled:
            # We can't really cancel a connect, and the socket is in an
            # indeterminate state. Better to close it so we don't get
            # confused.
            self._sock.close()
            raise
        # Okay, the connect finished, but it might have failed:
        err = self._sock.getsockopt(SOL_SOCKET, SO_ERROR)
        if err != 0:
            raise OSError(err, "Error in connect: " + _os.strerror(err))

    ################################################################
    # recv
    ################################################################

    recv = _make_simple_sock_method_wrapper(
        "recv", _core.wait_socket_readable)

    ################################################################
    # recv_into
    ################################################################

    recv_into = _make_simple_sock_method_wrapper(
        "recv_into", _core.wait_socket_readable)

    ################################################################
    # recvfrom
    ################################################################

    recvfrom = _make_simple_sock_method_wrapper(
        "recvfrom", _core.wait_socket_readable)

    ################################################################
    # recvfrom_into
    ################################################################

    recvfrom_into = _make_simple_sock_method_wrapper(
        "recvfrom_into", _core.wait_socket_readable)

    ################################################################
    # recvmsg
    ################################################################

    if hasattr(_stdlib_socket.socket, "recvmsg"):
        recvmsg = _make_simple_sock_method_wrapper(
            "recvmsg", _core.wait_socket_readable, maybe_avail=True)

    ################################################################
    # recvmsg_into
    ################################################################

    if hasattr(_stdlib_socket.socket, "recvmsg_into"):
        recvmsg_into = _make_simple_sock_method_wrapper(
            "recvmsg_into", _core.wait_socket_readable, maybe_avail=True)

    ################################################################
    # send
    ################################################################

    send = _make_simple_sock_method_wrapper(
        "send", _core.wait_socket_writable)

    ################################################################
    # sendto
    ################################################################

    @_wraps(_stdlib_socket.socket.sendto, assigned=(), updated=())
    async def sendto(self, *args):
        """Similar to :meth:`socket.socket.sendto`, but async and requiring a
        pre-resolved address. See :meth:`resolve_remote_address`.

        """
        # args is: data[, flags], address)
        # and kwargs are not accepted
        self._check_address(args[-1], require_resolved=True)
        return await self._nonblocking_helper(
            _stdlib_socket.socket.sendto, args, {},
            _core.wait_socket_writable)

    ################################################################
    # sendmsg
    ################################################################

    if hasattr(_stdlib_socket.socket, "sendmsg"):
        @_wraps(_stdlib_socket.socket.sendmsg, assigned=(), updated=())
        async def sendmsg(self, *args):
            """Similar to :meth:`socket.socket.sendmsg`, but async and
            requiring a pre-resolved address. See
            :meth:`resolve_remote_address`.

            Only available on platforms where :meth:`socket.socket.sendmsg` is
            available.

            """
            # args is: buffers[, ancdata[, flags[, address]]]
            # and kwargs are not accepted
            if len(args) == 4 and args[-1] is not None:
                self._check_address(args[-1], require_resolved=True)
            return await self._nonblocking_helper(
                _stdlib_socket.socket.sendmsg, args, {},
                _core.wait_socket_writable)

    ################################################################
    # sendall
    ################################################################

    async def sendall(self, data, flags=0):
        """Send the data to the socket, blocking until all of it has been
        accepted by the operating system.

        ``flags`` are passed on to ``send``.

        Most low-level operations in trio provide a guarantee: if they raise
        :exc:`trio.Cancelled`, this means that they had no effect, so the
        system remains in a known state. This is **not true** for
        :meth:`sendall`. If this operation raises :exc:`trio.Cancelled` (or
        any other exception for that matter), then it may have sent some, all,
        or none of the requested data, and there is no way to know which.

        """
        with memoryview(data) as data:
            if not data:
                await _core.yield_briefly()
                return
            total_sent = 0
            while total_sent < len(data):
                with data[total_sent:] as remaining:
                    sent = await self.send(remaining, flags)
                total_sent += sent

    ################################################################
    # sendfile
    ################################################################

    # Not implemented yet:
    # async def sendfile(self, file, offset=0, count=None):
    #     XX

    # Intentionally omitted:
    #   makefile
    #   setblocking
    #   settimeout
    #   timeout

__all__.append("SocketType")


################################################################
# create_connection
################################################################

# Copied from socket.create_connection and slightly tweaked.
#
# So this is a derivative work licensed under the PSF License, which requires
# the following notice:
#
#     Copyright © 2001-2017 Python Software Foundation; All Rights Reserved
#
# XX shouldn't this use AI_ADDRCONFIG? and ideally happy eyeballs...
#   actually it looks like V4MAPPED | ADDRCONFIG is the default on Linux, but
#   not on other systems. (V4MAPPED is irrelevant here b/c it's a no-op unless
#   family=AF_INET6)
# XX possibly we should just throw it out and replace with whatever API we
# like better :-) maybe an easy TLS option? AF_UNIX equivalent?
#
# some prior art: https://twistedmatrix.com/documents/current/api/twisted.internet.endpoints.HostnameEndpoint.html
# interesting considerations for happy eyeballs:
# - controlling the lag between attempt starts
# - start the next attempt early if the one before it errors out
# - per-attempt timeouts? (for if lag is set very high / infinity, disabling
#   happy eyeballs)

# Actually, disabling this for now because it's probably broken (see above),
# untested, and we probably want to use a different API anyway (see #73). We
# can revisit after the initial release.
#
# async def create_connection(address, source_address=None):
#     host, port = address
#     err = None
#     for res in await _getaddrinfo_impl(host, port, 0, SOCK_STREAM):
#         af, socktype, proto, canonname, sa = res
#         sock = None
#         try:
#             sock = socket(af, socktype, proto)
#             if source_address:
#                 sock.bind(source_address)
#             await sock.connect(sa)
#             return sock
#         except OSError as _:
#             err = _
#             if sock is not None:
#                 sock.close()
#     if err is not None:
#         raise err
#     else:
#         raise OSError("getaddrinfo returned an empty list")
# __all__.append("create_connection")


