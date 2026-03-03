"""Tests for Actor model: stateful remote objects with sequential execution."""

import nano_ray as nanoray


def setup_module():
    nanoray.init(num_workers=2)


def teardown_module():
    nanoray.shutdown()


# ---- Basic Actor ----


def test_actor_counter():
    """Basic counter actor with sequential increments."""

    @nanoray.remote
    class Counter:
        def __init__(self):
            self.n = 0

        def increment(self):
            self.n += 1
            return self.n

        def get_value(self):
            return self.n

    counter = Counter.remote()
    refs = [counter.increment.remote() for _ in range(5)]
    results = nanoray.get(refs)
    assert results == [1, 2, 3, 4, 5]


def test_actor_with_init_args():
    """Actor with constructor arguments."""

    @nanoray.remote
    class Accumulator:
        def __init__(self, initial):
            self.total = initial

        def add(self, value):
            self.total += value
            return self.total

    acc = Accumulator.remote(100)
    refs = [acc.add.remote(i) for i in [10, 20, 30]]
    results = nanoray.get(refs)
    assert results == [110, 130, 160]


def test_actor_sequential_guarantee():
    """Verify that actor methods execute in submission order."""

    @nanoray.remote
    class Logger:
        def __init__(self):
            self.log = []

        def append(self, msg):
            self.log.append(msg)
            return len(self.log)

        def get_log(self):
            return self.log

    logger = Logger.remote()
    for i in range(10):
        logger.append.remote(f"msg-{i}")

    # get_log depends on all previous appends completing (via chaining)
    ref = logger.get_log.remote()
    log = nanoray.get(ref)
    assert log == [f"msg-{i}" for i in range(10)]


def test_multiple_actors():
    """Multiple independent actors don't interfere."""

    @nanoray.remote
    class Counter:
        def __init__(self, start=0):
            self.n = start

        def increment(self):
            self.n += 1
            return self.n

    c1 = Counter.remote(0)
    c2 = Counter.remote(100)

    refs1 = [c1.increment.remote() for _ in range(3)]
    refs2 = [c2.increment.remote() for _ in range(3)]

    assert nanoray.get(refs1) == [1, 2, 3]
    assert nanoray.get(refs2) == [101, 102, 103]
