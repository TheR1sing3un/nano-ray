"""Tests for nano-ray core API: @remote, get(), init(), shutdown()."""

import nano_ray as nanoray


def setup_module():
    nanoray.init(num_workers=2)


def teardown_module():
    nanoray.shutdown()


# ---- Basic remote function ----


def test_remote_single_call():
    @nanoray.remote
    def add(a, b):
        return a + b

    ref = add.remote(1, 2)
    assert isinstance(ref, nanoray.ObjectRef)
    assert nanoray.get(ref) == 3


def test_remote_parallel_calls():
    @nanoray.remote
    def double(x):
        return x * 2

    refs = [double.remote(i) for i in range(10)]
    results = nanoray.get(refs)
    assert results == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]


def test_remote_kwargs():
    @nanoray.remote
    def greet(name, greeting="Hello"):
        return f"{greeting}, {name}!"

    ref = greet.remote("World", greeting="Hi")
    assert nanoray.get(ref) == "Hi, World!"


def test_remote_returns_none():
    @nanoray.remote
    def noop():
        pass

    ref = noop.remote()
    assert nanoray.get(ref) is None


def test_remote_large_result():
    @nanoray.remote
    def make_list(n):
        return list(range(n))

    ref = make_list.remote(1000)
    result = nanoray.get(ref)
    assert result == list(range(1000))


# ---- Error handling ----


def test_remote_task_error():
    @nanoray.remote
    def bad_func():
        raise ValueError("intentional error")

    ref = bad_func.remote()
    try:
        nanoray.get(ref)
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "intentional error" in str(e)


# ---- Direct call (no .remote) ----


def test_direct_call():
    @nanoray.remote
    def square(x):
        return x * x

    assert square(5) == 25


# ---- wait() ----


def test_wait_basic():
    @nanoray.remote
    def identity(x):
        return x

    refs = [identity.remote(i) for i in range(5)]
    ready, remaining = nanoray.wait(refs, num_returns=3, timeout=10)
    assert len(ready) >= 3
    assert len(ready) + len(remaining) == 5
