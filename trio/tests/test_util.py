import pytest

import signal
from async_generator import async_generator, yield_

from .._util import *
from .. import _core
from ..testing import wait_all_tasks_blocked, assert_yields

def test_signal_raise():
    record = []
    def handler(signum, _):
        record.append(signum)

    old = signal.signal(signal.SIGFPE, handler)
    try:
        signal_raise(signal.SIGFPE)
    finally:
        signal.signal(signal.SIGFPE, old)
    assert record == [signal.SIGFPE]


async def test_UnLock():
    ul1 = UnLock(RuntimeError, "ul1")
    ul2 = UnLock(ValueError)

    async with ul1:
        with assert_yields():
            async with ul2:
                print("ok")

    with pytest.raises(RuntimeError) as excinfo:
        async with ul1:
            with assert_yields():
                async with ul1:
                    pass  # pragma: no cover
    assert "ul1" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        async with ul2:
            with assert_yields():
                async with ul2:
                    pass  # pragma: no cover

    async def wait_with_ul1():
        async with ul1:
            await wait_all_tasks_blocked()

    with pytest.raises(RuntimeError) as excinfo:
        async with _core.open_nursery() as nursery:
            nursery.spawn(wait_with_ul1)
            nursery.spawn(wait_with_ul1)
    assert "ul1" in str(excinfo.value)

    # mixing sync and async entry
    with pytest.raises(RuntimeError) as excinfo:
        with ul1.sync:
            with assert_yields():
                async with ul1:
                    pass  # pragma: no cover
    assert "ul1" in str(excinfo.value)


async def test_acontextmanager_isasyncgen():
    with pytest.raises(TypeError) as excinfo:
        acontextmanager(lambda x: x)
    assert "must be an async generator" in str(excinfo.value)


async def test_acontextmanager_doesnt_yield():
    @acontextmanager
    @async_generator
    async def doesnt_yield():
        raise StopAsyncIteration

    g = doesnt_yield()
    with pytest.raises(RuntimeError) as excinfo:
        await g.__aenter__()
    assert "async generator didn't yield" in str(excinfo.value)


@acontextmanager
@async_generator
async def yields_twice_and_stops():
    try:
        await yield_(None)
    except:
        pass
    await yield_(None)
    raise StopAsyncIteration("foo")

async def test_acontextmanager_exhaust():
    g = yields_twice_and_stops()
    await g.__aenter__()
    await g.__aenter__()
    assert await g.__aexit__(None, None, None) == None

async def test_acontextmanager_exit_without_exhausting():
    g = yields_twice_and_stops()
    await g.__aenter__()
    with pytest.raises(RuntimeError) as excinfo:
        await g.__aexit__(None, None, None)
    assert "async generator didn't stop" in str(excinfo.value)

async def test_acontextmanager_raise_exc_in_middle():
    g = yields_twice_and_stops()
    await g.__aenter__()
    with pytest.raises(RuntimeError) as excinfo:
        await g.__aexit__(Exception, None, None)
    assert "async generator didn't stop after athrow()" in str(excinfo.value)

async def test_acontextmanager_raise_exc_in_middle_specify_exc():
    g = yields_twice_and_stops()
    await g.__aenter__()
    with pytest.raises(RuntimeError) as excinfo:
        await g.__aexit__(Exception, Exception(), None)
    assert "async generator didn't stop after athrow()" in str(excinfo.value)

async def test_acontextmanager_raise_on_enter():
    # Exhaust generator so python doesn't complain
    g = yields_twice_and_stops()
    await g.__aenter__()
    await g.__aenter__()
    with pytest.raises(RuntimeError):
        await g.__aenter__()
    await g.__aexit__(None, None, None)

    with pytest.raises(RuntimeError) as excinfo:
        g.__enter__()
    assert ("use 'async with yields_twice_and_stops(...)', "
            "not 'with yields_twice_and_stops(...)") in str(excinfo.value)


@acontextmanager
@async_generator
async def yields_and_raises_second():
    await yield_(None)
    raise await yield_(None)

async def test_acontextmanager_reraise_same():
    g = yields_and_raises_second()
    await g.__aenter__()
    await g.__aexit__(Exception, None, None)


async def test_acontextmanager_supress_same_exception():
    g = yields_and_raises_second()
    await g.__aenter__()
    await g.__aenter__()
    exit = await g.__aexit__(RuntimeError, None, None)
    assert exit == False
    del g

    g = yields_and_raises_second()
    await g.__aenter__()
    await g.__aenter__()
    exit = await g.__aexit__(StopAsyncIteration, None, None)
    assert exit == False
    del g


@acontextmanager
@async_generator
async def yields_twice_and_wrap_raise_second():
    await yield_(None)
    try:
        await yield_(None)
    except RuntimeError as e:
        raise RuntimeError("re-raise block exception") from e

async def test_acontextmanager_dont_suppress_wrapped_exception():
    g = yields_twice_and_wrap_raise_second()
    await g.__aenter__()
    await g.__aenter__()
    exit = await g.__aexit__(RuntimeError, RuntimeError("foo"), None)
    assert exit == False


@acontextmanager
@async_generator
async def yields_and_raises_exception():
    try:
        await yield_(None)
    except:
        pass
    raise Exception("Generator raised exception")

async def test_acontextmanager_reraise_different():
    g = yields_and_raises_exception()
    await g.__aenter__()
    with pytest.raises(Exception) as excinfo:
        exit = await g.__aexit__(RuntimeError, RuntimeError("Block raised exception"), None)
    assert "Generator raised exception" in str(excinfo.value)


@acontextmanager
@async_generator
async def yields_and_raises_runtimeerror():
    try:
        await yield_(None)
    except:
        pass
    raise RuntimeError("Generator raised exception")

async def test_acontextmanager_reraise_different_runtimeerrror():
    g = yields_and_raises_runtimeerror()
    await g.__aenter__()
    with pytest.raises(RuntimeError) as excinfo:
        exit = await g.__aexit__(RuntimeError, RuntimeError("Block raised exception"), None)
    assert "Generator raised exception" in str(excinfo.value)
