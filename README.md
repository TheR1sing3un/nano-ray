# nano-ray

**An educational distributed computing framework inspired by [Ray](https://github.com/ray-project/ray).**

nano-ray distills Ray's most elegant design ideas into a minimal, readable codebase. It is built with a Python + Rust hybrid architecture: Python for the user-facing API, Rust (via PyO3) for the performance-critical internals.

> This is a teaching tool, not a production system. The goal is to make Ray's core ideas accessible — ownership, distributed object store, task DAGs, actors — without the complexity of a production-grade distributed system.

```
              Python Layer (User Experience)
  ┌──────────────────────────────────────────────────┐
  │  @remote decorator ─ ObjectRef ─ get() ─ Actor   │
  └──────────────────────┬───────────────────────────┘
                         │ PyO3 FFI
  ┌──────────────────────▼───────────────────────────┐
  │          Rust Layer (System Performance)          │
  │  ObjectStore(DashMap) ─ Scheduler(SegQueue) ─ …   │
  └──────────────────────────────────────────────────┘
```

---

## Features

- **`@remote` decorator** — turn any function into a distributed task with one line
- **Task DAG** — pass `ObjectRef`s as arguments; the system builds and resolves the dependency graph automatically
- **Actor model** — `@remote` on a class creates a stateful remote object with sequential execution guarantees
- **Rust core** — lock-free object store (`DashMap`), lock-free scheduler (`SegQueue`), GIL-released blocking wait (`parking_lot::Condvar`)
- **Pure Python fallback** — runs without compiling Rust (`dict + threading.Event`)
- **Multi-node cluster** — head node + worker nodes communicate over TCP
- **Web dashboard** — real-time task metrics at `http://localhost:8265`

---

## Quick Start

### Install

```bash
# Clone
git clone https://github.com/your-username/nano-ray.git
cd nano-ray

# Install with Rust backend (recommended)
pip install -e .

# Or install without Rust (pure Python fallback)
pip install -e . --no-build-isolation
```

Requires **Python >= 3.10**. Building the Rust backend requires a [Rust toolchain](https://rustup.rs/) and [maturin](https://www.maturin.rs/).

### Hello, Remote

```python
import nano_ray as nanoray

nanoray.init(num_workers=4)

@nanoray.remote
def square(x):
    return x * x

# Submit 10 tasks in parallel
refs = [square.remote(i) for i in range(10)]
print(nanoray.get(refs))  # [0, 1, 4, 9, 16, 25, 36, 49, 64, 81]

nanoray.shutdown()
```

### Task DAG

Pass `ObjectRef`s as arguments — nano-ray automatically tracks dependencies:

```python
@nanoray.remote
def add(a, b):
    return a + b

ref1 = square.remote(3)   # 9
ref2 = square.remote(4)   # 16
ref3 = add.remote(ref1, ref2)  # depends on ref1 and ref2
print(nanoray.get(ref3))  # 25
```

### Actor

`@remote` on a class creates a stateful actor with sequential execution:

```python
@nanoray.remote
class Counter:
    def __init__(self):
        self.n = 0
    def increment(self):
        self.n += 1
        return self.n

counter = Counter.remote()
refs = [counter.increment.remote() for _ in range(5)]
print(nanoray.get(refs))  # [1, 2, 3, 4, 5] — guaranteed order
```

### Cluster Mode

```bash
# Terminal 1: start head node
python -m nano_ray.start --head --port 6379 --num-workers 2

# Terminal 2: start a worker node
python -m nano_ray.start --address 127.0.0.1:6379 --num-workers 4

# Terminal 3: client script
python -c "
import nano_ray as nanoray
nanoray.init(address='127.0.0.1:6379')

@nanoray.remote
def compute(x):
    return x ** 2

refs = [compute.remote(i) for i in range(8)]
print(nanoray.get(refs))

nanoray.shutdown()
"
```

Dashboard available at `http://127.0.0.1:8265` when the head node is running.

---

## Examples

| Example | Pattern | Key Concept |
|---------|---------|-------------|
| [`01_hello_remote.py`](examples/01_hello_remote.py) | Basic remote call | `@remote`, `get()` |
| [`02_pi_estimation.py`](examples/02_pi_estimation.py) | Embarrassingly parallel | Monte Carlo parallelism |
| [`03_task_chain.py`](examples/03_task_chain.py) | Task DAG | Diamond, chain, complex DAG |
| [`04_word_count.py`](examples/04_word_count.py) | MapReduce | Task chains with list args |
| [`05_parameter_server.py`](examples/05_parameter_server.py) | Actor | Parameter server for ML |

Run any example:

```bash
python examples/01_hello_remote.py
```

---

## Architecture

```
nano-ray/
├── python/nano_ray/
│   ├── api.py               # @remote, get(), init(), shutdown()
│   ├── driver.py             # Single-node runtime (workers + scheduler)
│   ├── actor.py              # ActorHandle + state chaining
│   ├── dag.py                # Dependency extraction & resolution
│   ├── head.py               # Head node (multi-node coordinator)
│   ├── node.py               # Worker node (multi-node executor)
│   ├── remote_runtime.py     # Client-side runtime for cluster mode
│   ├── transport.py          # TCP message protocol
│   ├── dashboard.py          # Web monitoring UI
│   └── _fallback/            # Pure Python fallback implementations
├── rust/nano_ray_core/src/
│   ├── object_store.rs       # DashMap + Condvar (GIL-free wait)
│   ├── scheduler.rs          # SegQueue (lock-free ready queue)
│   ├── ownership.rs          # Task/object metadata tracking
│   └── serializer.rs         # Serialization utilities
├── examples/                 # 5 runnable examples
├── tests/python/             # 30 tests (pytest)
├── benchmarks/               # Throughput & latency benchmarks
└── docs/design.md            # Design decisions with Ray comparisons
```

### How It Works

1. **`@remote`** wraps a function into a `RemoteFunction`. Calling `.remote()` serializes it with cloudpickle and submits to the scheduler.
2. **Scheduler** tracks dependencies. When all `ObjectRef` dependencies are resolved, the task enters the ready queue.
3. **Worker processes** pull tasks from the queue, execute them, and report results.
4. **Object store** holds immutable results. `get()` blocks (with GIL released) until the result is available.
5. **Actors** achieve sequential execution by chaining tasks: each method call depends on the previous one's output state.

### Key Differences from Ray

| Aspect | Ray | nano-ray |
|--------|-----|----------|
| Ownership | Decentralized (any worker can own) | Centralized (driver owns all) |
| Object store | Shared memory (Plasma, zero-copy) | In-process `DashMap` (or `dict`) |
| Scheduling | Two-layer (local raylet + global) | Single scheduler |
| Transport | gRPC + protobuf | Raw TCP + cloudpickle |
| Actor | Dedicated worker process | Task DAG with state chaining |
| Serialization | Arrow + custom numpy path | cloudpickle only |

See [`docs/design.md`](docs/design.md) for detailed explanations of every design decision.

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run Python tests
pytest tests/python/ -v

# Run Rust tests
cargo test --manifest-path rust/nano_ray_core/Cargo.toml

# Run benchmarks
python benchmarks/bench_throughput.py
python benchmarks/bench_object_store.py

# Lint
ruff check python/
```

---

## API Reference

### Core Functions

```python
nanoray.init(num_workers=4, address=None)  # Start runtime
nanoray.shutdown()                          # Stop runtime
nanoray.get(refs, timeout=None)            # Block until result(s) ready
nanoray.wait(refs, num_returns=1)          # Wait for subset of results
```

### Decorators

```python
@nanoray.remote      # Function → RemoteFunction (stateless task)
@nanoray.remote      # Class → RemoteClass (stateful actor)
```

### RemoteFunction

```python
ref = func.remote(*args, **kwargs)  # Submit task, returns ObjectRef
result = func(*args, **kwargs)      # Direct local call (for debugging)
```

### Actor

```python
actor = MyClass.remote(*init_args)          # Create actor
ref = actor.method_name.remote(*args)       # Call method (returns ObjectRef)
```

### Cluster CLI

```bash
python -m nano_ray.start --head --port 6379 [--num-workers N] [--dashboard-port 8265]
python -m nano_ray.start --address host:port [--num-workers N]
```

---

## References

- [Ray: A Distributed Framework for Emerging AI Applications (OSDI 2018)](https://www.usenix.org/conference/osdi18/presentation/moritz)
- [Ray Architecture Whitepaper](https://docs.google.com/document/d/1lAy0Owi-vPz2jEqBSaHNQcy2IBSDEHyXNOQZlGuj93c)
- [PyO3 User Guide](https://pyo3.rs/)
- [maturin Documentation](https://www.maturin.rs/)

---

## License

MIT

---

# nano-ray

**一个受 [Ray](https://github.com/ray-project/ray) 启发的教学级分布式计算框架。**

nano-ray 将 Ray 最精妙的设计思想提炼到一个最小化、可读性强的代码库中。采用 Python + Rust 混合架构：Python 处理用户接口层，Rust（通过 PyO3）实现性能关键的内部组件。

> 这是一个教学工具，而非生产系统。目标是让 Ray 的核心理念——Ownership 模型、分布式对象存储、Task DAG、Actor——变得触手可及，无需面对生产级分布式系统的复杂性。

```
            Python 层（用户体验层）
  ┌──────────────────────────────────────────────────┐
  │  @remote 装饰器 ─ ObjectRef ─ get() ─ Actor       │
  └──────────────────────┬───────────────────────────┘
                         │ PyO3 FFI
  ┌──────────────────────▼───────────────────────────┐
  │           Rust 层（系统性能层）                     │
  │  ObjectStore(DashMap) ─ Scheduler(SegQueue) ─ …   │
  └──────────────────────────────────────────────────┘
```

---

## 特性

- **`@remote` 装饰器** — 一行代码将任意函数变为分布式任务
- **Task DAG** — 将 `ObjectRef` 作为参数传递，系统自动构建并解析依赖图
- **Actor 模型** — 在类上使用 `@remote` 创建有状态的远程对象，保证顺序执行
- **Rust 核心** — 无锁对象存储（`DashMap`）、无锁调度器（`SegQueue`）、释放 GIL 的阻塞等待（`parking_lot::Condvar`）
- **纯 Python 后备** — 无需编译 Rust 也能运行（`dict + threading.Event`）
- **多节点集群** — Head 节点 + Worker 节点通过 TCP 通信
- **Web 监控面板** — 实时任务指标，访问 `http://localhost:8265`

---

## 快速开始

### 安装

```bash
# 克隆
git clone https://github.com/your-username/nano-ray.git
cd nano-ray

# 安装（含 Rust 后端，推荐）
pip install -e .

# 或者不编译 Rust（使用纯 Python 后备）
pip install -e . --no-build-isolation
```

需要 **Python >= 3.10**。编译 Rust 后端需要 [Rust 工具链](https://rustup.rs/) 和 [maturin](https://www.maturin.rs/)。

### Hello, Remote

```python
import nano_ray as nanoray

nanoray.init(num_workers=4)

@nanoray.remote
def square(x):
    return x * x

# 并行提交 10 个任务
refs = [square.remote(i) for i in range(10)]
print(nanoray.get(refs))  # [0, 1, 4, 9, 16, 25, 36, 49, 64, 81]

nanoray.shutdown()
```

### Task DAG

传递 `ObjectRef` 作为参数——nano-ray 自动追踪依赖关系：

```python
@nanoray.remote
def add(a, b):
    return a + b

ref1 = square.remote(3)   # 9
ref2 = square.remote(4)   # 16
ref3 = add.remote(ref1, ref2)  # 依赖 ref1 和 ref2
print(nanoray.get(ref3))  # 25
```

### Actor

在类上使用 `@remote` 创建有状态的 Actor，保证方法顺序执行：

```python
@nanoray.remote
class Counter:
    def __init__(self):
        self.n = 0
    def increment(self):
        self.n += 1
        return self.n

counter = Counter.remote()
refs = [counter.increment.remote() for _ in range(5)]
print(nanoray.get(refs))  # [1, 2, 3, 4, 5] — 保证顺序
```

### 集群模式

```bash
# 终端 1：启动 Head 节点
python -m nano_ray.start --head --port 6379 --num-workers 2

# 终端 2：启动 Worker 节点
python -m nano_ray.start --address 127.0.0.1:6379 --num-workers 4

# 终端 3：客户端脚本
python -c "
import nano_ray as nanoray
nanoray.init(address='127.0.0.1:6379')

@nanoray.remote
def compute(x):
    return x ** 2

refs = [compute.remote(i) for i in range(8)]
print(nanoray.get(refs))

nanoray.shutdown()
"
```

Head 节点运行时可访问 `http://127.0.0.1:8265` 查看监控面板。

---

## 示例

| 示例 | 模式 | 核心概念 |
|------|------|----------|
| [`01_hello_remote.py`](examples/01_hello_remote.py) | 基础远程调用 | `@remote`、`get()` |
| [`02_pi_estimation.py`](examples/02_pi_estimation.py) | 尴尬并行 | 蒙特卡洛并行估算 Pi |
| [`03_task_chain.py`](examples/03_task_chain.py) | Task DAG | 菱形、链式、复杂 DAG |
| [`04_word_count.py`](examples/04_word_count.py) | MapReduce | 列表参数的任务链 |
| [`05_parameter_server.py`](examples/05_parameter_server.py) | Actor | ML 参数服务器 |

运行示例：

```bash
python examples/01_hello_remote.py
```

---

## 架构

```
nano-ray/
├── python/nano_ray/
│   ├── api.py               # @remote, get(), init(), shutdown()
│   ├── driver.py             # 单节点运行时（worker 进程 + 调度器）
│   ├── actor.py              # ActorHandle + 状态链式传递
│   ├── dag.py                # 依赖提取与解析
│   ├── head.py               # Head 节点（多节点协调器）
│   ├── node.py               # Worker 节点（多节点执行器）
│   ├── remote_runtime.py     # 集群模式客户端运行时
│   ├── transport.py          # TCP 消息协议
│   ├── dashboard.py          # Web 监控面板
│   └── _fallback/            # 纯 Python 后备实现
├── rust/nano_ray_core/src/
│   ├── object_store.rs       # DashMap + Condvar（释放 GIL 的阻塞等待）
│   ├── scheduler.rs          # SegQueue（无锁就绪队列）
│   ├── ownership.rs          # Task/Object 元数据追踪
│   └── serializer.rs         # 序列化工具
├── examples/                 # 5 个可运行示例
├── tests/python/             # 30 个测试（pytest）
├── benchmarks/               # 吞吐量和延迟基准测试
└── docs/design.md            # 设计决策文档（含 Ray 对比）
```

### 工作原理

1. **`@remote`** 将函数包装为 `RemoteFunction`。调用 `.remote()` 时用 cloudpickle 序列化函数并提交到调度器。
2. **调度器** 追踪依赖关系。当所有 `ObjectRef` 依赖都已解析，任务进入就绪队列。
3. **Worker 进程** 从队列取出任务，执行后上报结果。
4. **对象存储** 保存不可变的结果。`get()` 阻塞等待（释放 GIL）直到结果可用。
5. **Actor** 通过链式任务实现顺序执行：每次方法调用都依赖前一次调用的输出状态。

### 与 Ray 的关键差异

| 方面 | Ray | nano-ray |
|------|-----|----------|
| Ownership | 去中心化（任何 worker 都可以是 owner） | 中心化（driver 拥有所有 task） |
| 对象存储 | 共享内存（Plasma，零拷贝） | 进程内 `DashMap`（或 `dict`） |
| 调度 | 两层（本地 raylet + 全局） | 单调度器 |
| 传输 | gRPC + protobuf | 原始 TCP + cloudpickle |
| Actor | 专用 worker 进程 | 基于状态链的 Task DAG |
| 序列化 | Arrow + numpy 快速路径 | 仅 cloudpickle |

详细设计解释参见 [`docs/design.md`](docs/design.md)。

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行 Python 测试
pytest tests/python/ -v

# 运行 Rust 测试
cargo test --manifest-path rust/nano_ray_core/Cargo.toml

# 运行基准测试
python benchmarks/bench_throughput.py
python benchmarks/bench_object_store.py

# Lint 检查
ruff check python/
```

---

## API 速查

### 核心函数

```python
nanoray.init(num_workers=4, address=None)  # 启动运行时
nanoray.shutdown()                          # 关闭运行时
nanoray.get(refs, timeout=None)            # 阻塞等待结果
nanoray.wait(refs, num_returns=1)          # 等待部分结果
```

### 装饰器

```python
@nanoray.remote      # 函数 → RemoteFunction（无状态任务）
@nanoray.remote      # 类 → RemoteClass（有状态 Actor）
```

### RemoteFunction

```python
ref = func.remote(*args, **kwargs)  # 提交任务，返回 ObjectRef
result = func(*args, **kwargs)      # 本地直接调用（调试用）
```

### Actor

```python
actor = MyClass.remote(*init_args)          # 创建 Actor
ref = actor.method_name.remote(*args)       # 调用方法（返回 ObjectRef）
```

### 集群 CLI

```bash
python -m nano_ray.start --head --port 6379 [--num-workers N] [--dashboard-port 8265]
python -m nano_ray.start --address host:port [--num-workers N]
```

---

## 参考资料

- [Ray: A Distributed Framework for Emerging AI Applications (OSDI 2018)](https://www.usenix.org/conference/osdi18/presentation/moritz)
- [Ray 架构白皮书](https://docs.google.com/document/d/1lAy0Owi-vPz2jEqBSaHNQcy2IBSDEHyXNOQZlGuj93c)
- [PyO3 用户指南](https://pyo3.rs/)
- [maturin 文档](https://www.maturin.rs/)

---

## 许可证

MIT
