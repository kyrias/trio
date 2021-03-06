import contextlib as _contextlib
import abc as _abc
from . import _core

__all__ = [
    "Clock", "Instrument", "AsyncResource", "SendStream", "ReceiveStream",
    "Stream", "HalfCloseableStream",
]

class Clock(_abc.ABC):
    """The interface for custom run loop clocks.

    """

    @_abc.abstractmethod
    def start_clock(self):
        """Do any setup this clock might need.

        Called at the beginning of the run.

        """

    @_abc.abstractmethod
    def current_time(self):
        """Return the current time, according to this clock.

        This is used to implement functions like :func:`trio.current_time` and
        :func:`trio.move_on_after`.

        Returns:
            float: The current time.

        """

    @_abc.abstractmethod
    def deadline_to_sleep_time(self, deadline):
        """Compute the real time until the given deadline.

        This is called before we enter a system-specific wait function like
        :func:~select.select`, to get the timeout to pass.

        For a clock using wall-time, this should be something like::

           return deadline - self.current_time()

        but of course it may be different if you're implementing some kind of
        virtual clock.

        Args:
            deadline (float): The absolute time of the next deadline,
                according to this clock.

        Returns:
            float: The number of real seconds to sleep until the given
            deadline. May be :data:`math.inf`.

        """


class Instrument(_abc.ABC):
    """The interface for run loop instrumentation.

    Instruments don't have to inherit from this abstract base class, and all
    of these methods are optional. This class serves mostly as documentation.

    """

    def before_run(self):
        """Called at the beginning of :func:`trio.run`.

        """

    def after_run(self):
        """Called just before :func:`trio.run` returns.

        """

    def task_spawned(self, task):
        """Called when the given task is created.

        Args:
            task (trio.Task): The new task.

        """

    def task_scheduled(self, task):
        """Called when the given task becomes runnable.

        It may still be some time before it actually runs, if there are other
        runnable tasks ahead of it.

        Args:
            task (trio.Task): The task that became runnable.

        """

    def before_task_step(self, task):
        """Called immediately before we resume running the given task.

        Args:
            task (trio.Task): The task that is about to run.

        """

    def after_task_step(self, task):
        """Called when we return to the main run loop after a task has yielded.

        Args:
            task (trio.Task): The task that just ran.

        """

    def task_exited(self, task):
        """Called when the given task exits.

        Args:
            task (trio.Task): The finished task.

        """

    def before_io_wait(self, timeout):
        """Called before blocking to wait for I/O readiness.

        Args:
            timeout (float): The number of seconds we are willing to wait.

        """

    def after_io_wait(self, timeout):
        """Called after handling pending I/O.

        Args:
            timeout (float): The number of seconds we were willing to
                wait. This much time may or may not have elapsed, depending on
                whether any I/O was ready.

        """


# We use ABCMeta instead of ABC, plus setting __slots__=(), so as not to force
# a __dict__ onto subclasses.
class AsyncResource(metaclass=_abc.ABCMeta):
    """A standard interface for resources that needs to be cleaned up, and
    where that cleanup may require blocking operations.

    This class distinguishes between "graceful" closes, which may perform I/O
    and thus block, and a "forceful" close, which cannot. For example, cleanly
    shutting down a TLS-encrypted connection requires sending a "goodbye"
    message; but if a peer has become non-responsive, then sending this
    message might block forever, so we may want to just drop the connection
    instead. Therefore the :meth:`graceful_close` method is unusual in that it
    should always close the connection (or at least make its best attempt)
    *even if it fails*; failure indicates a failure to achieve grace, not a
    failure to close the connection.

    Objects that implement this interface can be used as async context
    managers, i.e., you can write::

      async with create_resource() as some_async_resource:
          ...

    Entering the context manager is synchronous (not a checkpoint); exiting it
    calls :meth:`graceful_close`. The default implementations of
    ``__aenter__`` and ``__aexit__`` should be adequate for all subclasses.

    """
    __slots__ = ()

    @_abc.abstractmethod
    def forceful_close(self):
        """Force an immediate close of this resource.

        This will never block, but (depending on the resource in question) it
        might be a "rude" shutdown.

        If the resource is already closed, then this method should silently
        succeed.

        """

    async def graceful_close(self):
        """Close this resource, gracefully.

        This may block in order to perform a "graceful" shutdown (for example,
        sending a "goodbye" message). But, if this fails (e.g., due to being
        cancelled), then it still *must* close the underlying resource,
        possibly by calling :meth:`forceful_close`.

        If the resource is already closed, then this method should silently
        succeed.

        :class:`AsyncResource` provides a default implementation of this
        method that's suitable for resources that don't distinguish between
        graceful and forceful closure: it simply calls :meth:`forceful_close`
        and then executes a checkpoint.

        """
        try:
            self.forceful_close()
        finally:
            await _core.yield_briefly()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.graceful_close()


class SendStream(AsyncResource):
    """A standard interface for sending data on a byte stream.

    The underlying stream may be unidirectional, or bidirectional. If it's
    bidirectional, then you probably want to also implement
    :class:`ReceiveStream`, which makes your object a :class:`Stream`.

    Every :class:`SendStream` also implements the :class:`AsyncResource`
    interface.

    """
    __slots__ = ()

    @_abc.abstractmethod
    async def send_all(self, data):
        """Sends the given data through the stream, blocking if necessary.

        Args:
          data (bytes, bytearray, or memoryview): The data to send.

        Raises:
          trio.ResourceBusyError: if another task is already executing a
              :meth:`send_all`, :meth:`wait_send_all_might_not_block`, or
              :meth:`HalfCloseableStream.send_eof` on this stream.

        """

    @_abc.abstractmethod
    async def wait_send_all_might_not_block(self):
        """Block until it's possible that :meth:`send_all` might not block.

        This method may return early: it's possible that after it returns,
        :meth:`send_all` will still block. (In the worst case, if no better
        implementation is available, then it might always return immediately
        without blocking. It's nice to do better than that when possible,
        though.)

        This method **must not** return *late*: if it's possible for
        :meth:`send_all` to complete without blocking, then it must
        return. When implementing it, err on the side of returning early.

        Raises:
          trio.ResourceBusyError: if another task is already executing a
              :meth:`send_all`, :meth:`wait_send_all_might_not_block`, or
              :meth:`HalfCloseableStream.send_eof` on this stream.

        Note:

          This method is intended to aid in implementing protocols that want
          to delay choosing which data to send until the last moment. E.g.,
          suppose you're working on an implemention of a remote display server
          like `VNC
          <https://en.wikipedia.org/wiki/Virtual_Network_Computing>`__, and
          the network connection is currently backed up so that if you call
          :meth:`send_all` now then it will sit for 0.5 seconds before actually
          sending anything. In this case it doesn't make sense to take a
          screenshot, then wait 0.5 seconds, and then send it, because the
          screen will keep changing while you wait; it's better to wait 0.5
          seconds, then take the screenshot, and then send it, because this
          way the data you deliver will be more
          up-to-date. Using :meth:`wait_send_all_might_not_block` makes it
          possible to implement the better strategy.

          If you use this method, you might also want to read up on
          ``TCP_NOTSENT_LOWAT``.

          Further reading:

          * `Prioritization Only Works When There's Pending Data to Prioritize
            <https://insouciant.org/tech/prioritization-only-works-when-theres-pending-data-to-prioritize/>`__

          * WWDC 2015: Your App and Next Generation Networks: `slides
            <http://devstreaming.apple.com/videos/wwdc/2015/719ui2k57m/719/719_your_app_and_next_generation_networks.pdf?dl=1>`__,
            `video and transcript
            <https://developer.apple.com/videos/play/wwdc2015/719/>`__

        """


class ReceiveStream(AsyncResource):
    """A standard interface for receiving data on a byte stream.

    The underlying stream may be unidirectional, or bidirectional. If it's
    bidirectional, then you probably want to also implement
    :class:`SendStream`, which makes your object a :class:`Stream`.

    Every :class:`ReceiveStream` also implements the :class:`AsyncResource`
    interface.

    """
    __slots__ = ()

    @_abc.abstractmethod
    async def receive_some(self, max_bytes):
        """Wait until there is data available on this stream, and then return
        at most ``max_bytes`` of it.

        A return value of ``b""`` (an empty bytestring) indicates that the
        stream has reached end-of-file. Implementations should be careful that
        they return ``b""`` if, and only if, the stream has reached
        end-of-file!

        This method will return as soon as any data is available, so it may
        return fewer than ``max_bytes`` of data. But it will never return
        more.

        Args:
          max_bytes (int): The maximum number of bytes to return. Must be
              greater than zero.

        Returns:
          bytes or bytearray: The data received.

        Raises:
          trio.ResourceBusyError: if two tasks attempt to call
              :meth:`receive_some` on the same stream at the same time.
          trio.BrokenStreamError: if something has gone wrong, and the stream
              is broken.
          trio.ClosedStreamError: if someone already called one of the close
              methods on this stream object.

        """


class Stream(SendStream, ReceiveStream):
    """A standard interface for interacting with bidirectional byte streams.

    A :class:`Stream` is an object that implements both the
    :class:`SendStream` and :class:`ReceiveStream` interfaces.

    If implementing this interface, you should consider whether you can go one
    step further and implement :class:`HalfCloseableStream`.

    """
    __slots__ = ()


class HalfCloseableStream(Stream):
    """This interface extends :class:`Stream` to also allow closing the send
    part of the stream without closing the receive part.

    """
    __slots__ = ()


    @_abc.abstractmethod
    async def send_eof(self):
        """Send an end-of-file indication on this stream, if possible.

        The difference between :meth:`send_eof` and
        :meth:`~AsyncResource.graceful_close` is that :meth:`send_eof` is a
        *unidirectional* end-of-file indication. After you call this method,
        you shouldn't try sending any more data on this stream, and your
        remote peer should receive an end-of-file indication (eventually,
        after receiving all the data you sent before that). But, they may
        continue to send data to you, and you can continue to receive it by
        calling :meth:`~ReceiveStream.receive_some`. You can think of it as
        calling :meth:`~AsyncResource.graceful_close` on just the
        :class:`SendStream` "half" of the stream object (and in fact that's
        literally how :class:`trio.StapledStream` implements it).

        Examples:

        * On a socket, this corresponds to ``shutdown(..., SHUT_WR)`` (`man
          page <https://linux.die.net/man/2/shutdown>`__).

        * The SSH protocol provides the ability to multiplex bidirectional
          "channels" on top of a single encrypted connection. A trio
          implementation of SSH could expose these channels as
          :class:`HalfCloseableStream` objects, and calling :meth:`send_eof`
          would send an ``SSH_MSG_CHANNEL_EOF`` request (see `RFC 4254 §5.3
          <https://tools.ietf.org/html/rfc4254#section-5.3>`__).

        * On an SSL/TLS-encrypted connection, the protocol doesn't provide any
          way to do a unidirectional shutdown without closing the connection
          entirely, so :class:`~trio.ssl.SSLStream` implements
          :class:`Stream`, not :class:`HalfCloseableStream`.

        If an EOF has already been sent, then this method should silently
        succeed.

        Raises:
          trio.ResourceBusyError: if another task is already executing a
              :meth:`~SendStream.send_all`,
              :meth:`~SendStream.wait_send_all_might_not_block`, or
              :meth:`send_eof` on this stream.
          trio.BrokenStreamError: if something has gone wrong, and the stream
              is broken.
          trio.ClosedStreamError: if someone already called one of the close
              methods on this stream object.

        """
