"""Tests for Task DAG: ObjectRef as argument, dependency resolution."""

import nano_ray as nanoray


def setup_module():
    nanoray.init(num_workers=2)


def teardown_module():
    nanoray.shutdown()


# ---- Basic DAG (task chain) ----


def test_simple_chain():
    """ref1 -> ref2: one task depends on another."""

    @nanoray.remote
    def square(x):
        return x * x

    @nanoray.remote
    def add_one(x):
        return x + 1

    ref1 = square.remote(3)  # 9
    ref2 = add_one.remote(ref1)  # 10
    assert nanoray.get(ref2) == 10


def test_diamond_dag():
    """Classic diamond: ref3 depends on ref1 AND ref2."""

    @nanoray.remote
    def square(x):
        return x * x

    @nanoray.remote
    def add(a, b):
        return a + b

    ref1 = square.remote(3)  # 9
    ref2 = square.remote(4)  # 16
    ref3 = add.remote(ref1, ref2)  # 25
    assert nanoray.get(ref3) == 25


def test_deep_chain():
    """A chain of 5 tasks."""

    @nanoray.remote
    def increment(x):
        return x + 1

    ref = increment.remote(0)
    for _ in range(4):
        ref = increment.remote(ref)
    assert nanoray.get(ref) == 5


def test_objectref_in_list():
    """ObjectRefs nested inside a list argument."""

    @nanoray.remote
    def sum_all(values):
        return sum(values)

    @nanoray.remote
    def identity(x):
        return x

    refs = [identity.remote(i) for i in range(5)]
    result_ref = sum_all.remote(refs)
    assert nanoray.get(result_ref) == 10


def test_objectref_in_kwargs():
    """ObjectRef passed as keyword argument."""

    @nanoray.remote
    def multiply(a, b):
        return a * b

    @nanoray.remote
    def make_value(x):
        return x

    ref_a = make_value.remote(7)
    ref_b = make_value.remote(6)
    ref_result = multiply.remote(a=ref_a, b=ref_b)
    assert nanoray.get(ref_result) == 42


def test_mixed_args():
    """Mix of direct values and ObjectRefs."""

    @nanoray.remote
    def add(a, b):
        return a + b

    @nanoray.remote
    def square(x):
        return x * x

    ref1 = square.remote(3)  # 9
    ref2 = add.remote(ref1, 1)  # 9 + 1 = 10 (ref1 is ObjectRef, 1 is direct)
    assert nanoray.get(ref2) == 10


def test_get_intermediate_results():
    """User can get() intermediate results too."""

    @nanoray.remote
    def double(x):
        return x * 2

    ref1 = double.remote(5)  # 10
    ref2 = double.remote(ref1)  # 20
    ref3 = double.remote(ref2)  # 40

    assert nanoray.get(ref1) == 10
    assert nanoray.get(ref2) == 20
    assert nanoray.get(ref3) == 40
