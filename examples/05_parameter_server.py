"""Example 05: Parameter Server (Actor pattern).

Demonstrates:
- Actor creation with @remote on a class
- Sequential method execution guarantee
- Multiple actors operating independently
- Classic parameter server pattern used in distributed ML

In real ML training, the parameter server holds model weights and
workers compute gradients. This example simulates that pattern.
"""

import nano_ray as nanoray


@nanoray.remote
class ParameterServer:
    """A simple parameter server that holds and updates model weights."""

    def __init__(self, num_params: int):
        self.weights = [0.0] * num_params
        self.update_count = 0

    def get_weights(self) -> list[float]:
        return self.weights.copy()

    def apply_gradient(self, gradient: list[float], learning_rate: float = 0.1) -> int:
        """Apply a gradient update. Returns the update count."""
        for i in range(len(self.weights)):
            self.weights[i] -= learning_rate * gradient[i]
        self.update_count += 1
        return self.update_count


@nanoray.remote
def compute_gradient(weights: list[float], data_batch: list[float]) -> list[float]:
    """Simulate gradient computation (mock: returns scaled batch)."""
    return [d * 0.1 for d in data_batch]


if __name__ == "__main__":
    nanoray.init(num_workers=4)

    # Create parameter server with 3 parameters
    ps = ParameterServer.remote(3)

    # Simulate 5 training steps
    for step in range(5):
        # Get current weights from the parameter server
        weights_ref = ps.get_weights.remote()
        weights = nanoray.get(weights_ref)

        # Simulate computing gradients on different data batches
        batches = [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
        ]
        grad_refs = [compute_gradient.remote(weights, batch) for batch in batches]
        gradients = nanoray.get(grad_refs)

        # Apply gradients to the parameter server
        for grad in gradients:
            ps.apply_gradient.remote(grad, learning_rate=0.01)

    # Check final state
    final_weights = nanoray.get(ps.get_weights.remote())
    update_count = nanoray.get(ps.apply_gradient.remote([0, 0, 0]))

    print(f"Final weights: {[f'{w:.4f}' for w in final_weights]}")
    print(f"Total updates: {update_count}")
    assert update_count == 11  # 5 steps * 2 batches + 1 final

    # --- Simple counter actor ---
    @nanoray.remote
    class Counter:
        def __init__(self):
            self.n = 0

        def increment(self):
            self.n += 1
            return self.n

    counter = Counter.remote()
    refs = [counter.increment.remote() for _ in range(5)]
    results = nanoray.get(refs)
    print(f"Counter results: {results}")
    assert results == [1, 2, 3, 4, 5]
    print("All checks passed!")

    nanoray.shutdown()
