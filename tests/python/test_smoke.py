"""Smoke test: verify that nano_ray can be imported and the Rust bridge works."""


def test_hello():
    from nano_ray._core import hello

    assert hello() == "nano-ray is alive!"


def test_package_hello():
    import nano_ray

    assert "nano-ray is alive!" in nano_ray.hello()
