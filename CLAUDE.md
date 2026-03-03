# CLAUDE.md — nano-ray 项目工作指令

> 本文件是 nano-ray 项目的完整上下文文档，供 Claude agent 直接作为工作依据。
> 请在每次开始工作前阅读本文件，确保理解项目目标、架构决策和当前进度。

---

## 1. 项目概述

### 1.1 nano-ray 是什么

nano-ray 是一个 **教学级分布式计算框架**，借鉴 Ray 的核心设计理念，用 Python + Rust 混合架构实现。目标不是复刻 Ray 的全部功能，而是用最小化的代码精确体现 Ray 最精妙的设计思想。

### 1.2 项目目标（按优先级排序）

1. **正确体现 Ray 的核心设计理念**（Ownership、分布式对象存储、两层调度）
2. **代码清晰可读**，每个模块的职责边界明确
3. **可运行的 demo**，能用 `@remote` + `nanoray.get()` 跑通完整流程
4. **性能关键路径用 Rust 实现**，通过 PyO3 暴露给 Python
5. **有 design doc 解释每个设计决策的 why**

### 1.3 非目标（明确不做的事）

- 不追求生产级可靠性和容错
- 不实现 Ray 的全部 API（如 placement group、runtime env 等）
- 不做复杂的资源管理（CPU/GPU/自定义资源）
- 不做分布式 GC 的完整实现
- 不追求和 Ray 的性能对标

---

## 2. Ray 核心设计理念（agent 必须理解）

以下是 nano-ray 必须体现的 Ray 核心设计理念。**每个设计决策都要能回答"为什么这样做"**。

### 2.1 两个统一原语：Task 和 Actor

Ray 把所有分布式计算归结为两个原语：

- **Task**：无状态的远程函数调用。幂等、可重试、天然并行。
- **Actor**：有状态的远程对象。方法调用按顺序执行，状态封装在 actor 内部。

关键洞察：几乎所有分布式计算模式都可以用 Task + Actor 组合表达——数据并行是一堆 task，参数服务器是 actor，流水线并行是 task + actor 的 DAG。

**nano-ray 实现要求**：必须同时支持 Task 和 Actor，这是最基本的用户 API。

### 2.2 Ownership 模型（最核心的设计）

这是 Ray 和其他分布式系统最本质的区别，nano-ray 必须正确实现。

**传统方式**（如 Spark Driver）：中心化 master 跟踪所有 task 状态 → 单点瓶颈。

**Ray 的 Ownership 模型**：谁提交了 task，谁就是这个 task 和其返回值的 owner。

```
Worker A 调用 f.remote()
  → A 是这个 task 的 owner
  → A 负责跟踪 task 状态
  → A 负责管理返回的 ObjectRef 的生命周期
  → 如果 task 失败，A 负责重试
```

核心好处：

- **去中心化**：没有单点瓶颈，每个 worker 只管理自己提交的 task
- **局部性**：owner 通常就是消费结果的人，减少元数据查询的网络跳数
- **可扩展**：task 管理的负载自然分散到所有 worker

代价：owner 挂了，其管理的所有 task 和 object 元数据丢失（需要 lineage 重建，nano-ray 可简化处理）。

### 2.3 分布式对象存储

每个节点上都有一个 Object Store：

- **不可变对象**：一旦写入不能修改，极大简化一致性问题
- **引用计数**：owner 跟踪每个 object 的引用数，归零时回收
- **位置透明**：调用者不需要知道对象在哪个节点
- **零拷贝**：同一节点的 worker 通过共享内存（或 Arc）直接读取

对象传输策略：

- 小对象（< 64KB）：内联到消息中
- 大对象：通过节点间直接传输通道
- owner 不参与数据传输，只提供位置信息

### 2.4 两层调度

- **本地调度器**：每个节点一个，优先本地调度（数据局部性），资源不足才上报
- **全局调度**：负责跨节点的资源感知调度

调度决策核心考量：数据局部性 > 资源可用性 > 负载均衡。

### 2.5 Lineage 容错

不做 checkpoint（开销太大），记录 task 的 lineage（血统）。对象丢失时，通过 lineage 重新执行 task 重建对象。

**nano-ray 简化**：P0-P3 阶段不实现容错，但 OwnershipTable 中保留 lineage 信息字段（task_id → function + args 的映射），为后续扩展留口。

---

## 3. 技术架构

### 3.1 语言分工原则

判断模块用 Python 还是 Rust 的三个标准：

1. **是否在热路径上**：每次 task 提交/完成都经过 → Rust
2. **是否涉及内存/并发敏感操作**：共享内存、零拷贝、无锁数据结构 → Rust
3. **是否是用户体验/高层编排**：装饰器、DAG 构建、Actor 代理 → Python

### 3.2 架构分层

```
┌─────────────────────────────────────────────────┐
│              Python 层（用户体验层）               │
│  api.py       — @remote 装饰器、用户 API          │
│  driver.py    — Driver 进程入口                   │
│  actor.py     — Actor 高层抽象                    │
│  dag.py       — Task DAG 构建与依赖解析            │
│  dashboard/   — Web 可视化（可选）                 │
└─────────────────┬───────────────────────────────┘
                  │ PyO3 FFI 边界
┌─────────────────▼───────────────────────────────┐
│              Rust 层（系统性能层）                  │
│  object_store  — 对象存储（DashMap + Arc<Bytes>） │
│  serializer    — 高性能序列化/反序列化             │
│  scheduler     — Task 队列 + 调度决策              │
│  transport     — 节点间通信                       │
│  ownership     — Ownership 表 + 引用计数           │
└─────────────────────────────────────────────────┘
```

### 3.3 项目目录结构

```
nano-ray/
├── Cargo.toml                       # Rust workspace 根配置
├── pyproject.toml                   # Python 包配置（maturin 构建）
├── CLAUDE.md                        # 本文件
├── docs/
│   └── design.md                    # 设计决策文档
├── python/
│   └── nano_ray/
│       ├── __init__.py              # 对外导出 API
│       ├── api.py                   # @remote, get(), init(), shutdown()
│       ├── actor.py                 # ActorHandle, Actor 代理
│       ├── dag.py                   # DAG 依赖解析
│       ├── driver.py                # Driver 进程管理
│       └── _fallback/               # Phase 1 纯 Python 后备实现
│           ├── object_store.py
│           ├── scheduler.py
│           └── ownership.py
├── rust/
│   └── nano_ray_core/
│       ├── Cargo.toml
│       └── src/
│           ├── lib.rs               # PyO3 模块入口 + FFI 导出
│           ├── object_store.rs      # 对象存储
│           ├── ownership.rs         # Ownership 表 + 引用计数
│           ├── scheduler.rs         # 调度器核心
│           ├── serializer.rs        # 序列化
│           ├── transport.rs         # 节点间通信
│           └── types.rs             # 共享类型定义（TaskId, ObjectId 等）
├── examples/
│   ├── 01_hello_remote.py           # 最简单的 @remote 示例
│   ├── 02_pi_estimation.py          # 并行蒙特卡洛估算 π
│   ├── 03_task_chain.py             # Task DAG / pipeline
│   ├── 04_word_count.py             # 经典 MapReduce
│   └── 05_parameter_server.py       # Actor 示例
├── tests/
│   ├── python/
│   │   ├── test_api.py
│   │   ├── test_dag.py
│   │   └── test_actor.py
│   └── rust/
│       └── (Rust 单元测试内嵌在各模块中)
└── benchmarks/
    ├── bench_throughput.py           # task 吞吐量
    └── bench_object_store.py         # 对象读写延迟
```

---

## 4. 模块详细规范

### 4.1 Rust 模块

#### 4.1.1 types.rs — 共享类型

所有 Rust 模块和 PyO3 导出共用的基础类型。

```rust
// 核心 ID 类型，全局唯一
pub struct TaskId(pub u64);      // 单调递增或 UUID
pub struct ObjectId(pub u64);    // 与 TaskId 一一对应（一个 task 产生一个 object）
pub struct WorkerId(pub u64);
pub struct NodeId(pub u64);

// Task 规格
pub struct TaskSpec {
    pub task_id: TaskId,
    pub function_descriptor: FunctionDescriptor,  // 函数标识（模块路径 + 函数名）
    pub args: Vec<TaskArg>,
    pub owner: WorkerId,
}

pub enum TaskArg {
    Value(Bytes),             // 已序列化的值，直接传递
    ObjectRef(ObjectId),      // 引用另一个 task 的输出，需要 resolve
}

pub enum TaskStatus {
    WaitingForDependencies,
    Ready,
    Running { worker: WorkerId },
    Finished { location: NodeId },
    Failed { error: String },
}
```

#### 4.1.2 object_store.rs — 对象存储

职责：存储不可变对象，支持并发读取，内存管理。

```rust
pub struct ObjectStore {
    objects: DashMap<ObjectId, Arc<StoredObject>>,
    memory_used: AtomicUsize,
    memory_limit: usize,
    // 条件变量：等待某个 object 被 put
    waiters: DashMap<ObjectId, Vec<Sender<()>>>,
}

pub struct StoredObject {
    pub data: Bytes,            // 不可变数据
    pub size: usize,
    pub created_at: Instant,
}

// 核心 API
impl ObjectStore {
    pub fn put(&self, id: ObjectId, data: Bytes) -> Result<()>;
    pub fn get(&self, id: &ObjectId) -> Option<Arc<StoredObject>>;  // 非阻塞
    pub fn get_or_wait(&self, id: &ObjectId) -> Arc<StoredObject>;  // 阻塞等待
    pub fn delete(&self, id: &ObjectId) -> bool;
    pub fn contains(&self, id: &ObjectId) -> bool;
    pub fn memory_usage(&self) -> usize;
}
```

关键 crate 依赖：`dashmap`（无锁并发 map）、`bytes`（引用计数字节缓冲）、`tokio::sync`（通知机制）。

#### 4.1.3 ownership.rs — Ownership 表

职责：跟踪每个 task 和 object 的 owner、状态、引用计数。这是 Ray 最核心设计的体现。

```rust
pub struct OwnershipTable {
    tasks: DashMap<TaskId, TaskEntry>,
    objects: DashMap<ObjectId, ObjectEntry>,
}

pub struct TaskEntry {
    pub owner: WorkerId,
    pub status: AtomicCell<TaskStatus>,
    pub dependencies: Vec<ObjectId>,
    pub result_id: ObjectId,
    // lineage 信息（预留容错扩展）
    pub function_descriptor: FunctionDescriptor,
    pub serialized_args: Vec<TaskArg>,
}

pub struct ObjectEntry {
    pub owner: WorkerId,
    pub location: RwLock<Option<NodeId>>,
    pub ref_count: AtomicI32,
    pub producer_task: TaskId,
}

impl OwnershipTable {
    // 注册新 task，返回结果的 ObjectId
    pub fn register_task(&self, spec: &TaskSpec) -> ObjectId;
    // task 执行完毕，更新位置信息
    pub fn task_finished(&self, task_id: TaskId, location: NodeId);
    pub fn task_failed(&self, task_id: TaskId, error: String);
    // 引用计数管理
    pub fn add_ref(&self, object_id: ObjectId);
    pub fn remove_ref(&self, object_id: ObjectId) -> bool;  // true = 可回收
    // 查询
    pub fn get_task_status(&self, task_id: TaskId) -> Option<TaskStatus>;
    pub fn get_object_location(&self, object_id: ObjectId) -> Option<NodeId>;
    pub fn get_dependencies(&self, task_id: TaskId) -> Vec<ObjectId>;
}
```

#### 4.1.4 scheduler.rs — 调度器核心

职责：管理 task 的就绪队列、依赖追踪、worker 分配。

```rust
pub struct SchedulerCore {
    ready_queue: ConcurrentQueue<TaskId>,
    pending_tasks: DashMap<TaskId, PendingTask>,
    // 反向索引：某个 object 就绪后，哪些 task 会被唤醒
    object_to_waiting_tasks: DashMap<ObjectId, Vec<TaskId>>,
    worker_pool: WorkerPool,
}

struct PendingTask {
    spec: TaskSpec,
    unresolved_deps: AtomicUsize,  // 还有几个依赖未就绪
}

impl SchedulerCore {
    // 提交 task，如果依赖都就绪则直接入 ready_queue
    pub fn submit(&self, spec: TaskSpec) -> ObjectId;
    // 某个 object 就绪，触发依赖它的 task
    pub fn on_object_ready(&self, object_id: ObjectId);
    // worker 获取下一个可执行的 task
    pub fn pop_ready_task(&self) -> Option<TaskSpec>;
    // task 完成回调
    pub fn on_task_finished(&self, task_id: TaskId);
    pub fn on_task_failed(&self, task_id: TaskId, error: String);
}
```

#### 4.1.5 serializer.rs — 序列化

职责：高效序列化 Python 对象，特别是 numpy array 的快速路径。

```rust
pub enum SerializedPayload {
    Pickle(Bytes),                          // 普通 Python 对象
    NumpyArray { dtype: String, shape: Vec<usize>, data: Bytes },  // numpy 快速路径
}

pub fn serialize(py: Python, obj: &PyObject) -> Result<Bytes>;
pub fn deserialize(py: Python, data: &[u8]) -> Result<PyObject>;
```

Phase 2 初期可以先用 Python pickle 做序列化，Rust 层只做字节搬运。numpy 快速路径作为优化项后续添加。

#### 4.1.6 transport.rs — 节点间通信

职责：节点发现、消息传输、对象传输。

```rust
pub enum Message {
    // 控制面消息
    SubmitTask(TaskSpec),
    TaskFinished { task_id: TaskId, location: NodeId },
    TaskFailed { task_id: TaskId, error: String },
    // 数据面消息
    ObjectRequest { object_id: ObjectId, requester: NodeId },
    ObjectTransfer { object_id: ObjectId, data: Bytes },
    // 心跳
    Heartbeat { node_id: NodeId, num_idle_workers: usize },
}

pub struct TransportLayer {
    node_id: NodeId,
    peers: DashMap<NodeId, PeerConnection>,
}

impl TransportLayer {
    pub async fn send(&self, to: NodeId, msg: Message) -> Result<()>;
    pub async fn recv(&self) -> Result<(NodeId, Message)>;
    // 大对象直接传输（绕过消息队列）
    pub async fn transfer_object(&self, to: NodeId, data: &[u8]) -> Result<()>;
}
```

Phase 4 才需要实现。Phase 1-3 用单节点模式（multiprocessing.Queue 通信）。

### 4.2 Python 模块

#### 4.2.1 api.py — 用户 API（最重要的用户接口）

```python
import nano_ray_core  # PyO3 编译的 Rust 模块（Phase 2+ 可用）

def init(num_workers: int = 4, address: str | None = None) -> None:
    """初始化 nano-ray 运行时。
    - num_workers: 本地 worker 进程数
    - address: 远程节点地址（Phase 4+，单机模式为 None）
    """

def shutdown() -> None:
    """关闭运行时，清理所有资源。"""

def remote(func_or_class):
    """装饰器，将函数/类注册为远程可调用。
    用法:
        @nanoray.remote
        def f(x): return x + 1

        @nanoray.remote
        class Counter:
            def __init__(self): self.n = 0
            def inc(self): self.n += 1; return self.n
    """

def get(refs, timeout: float | None = None):
    """阻塞获取一个或多个 ObjectRef 的值。
    - refs: 单个 ObjectRef 或 ObjectRef 列表
    - 返回对应的值或值列表
    """

def wait(refs: list, num_returns: int = 1, timeout: float | None = None):
    """等待至少 num_returns 个结果就绪。
    返回 (ready_refs, remaining_refs) 二元组。
    """
```

**用户体验标准**：以下代码必须能跑通

```python
import nano_ray as nanoray

nanoray.init(num_workers=4)

@nanoray.remote
def square(x):
    return x * x

# 基本调用
futures = [square.remote(i) for i in range(10)]
results = nanoray.get(futures)
print(results)  # [0, 1, 4, 9, 16, 25, 36, 49, 64, 81]

# Task 链（DAG）
@nanoray.remote
def add(a, b):
    return a + b

ref1 = square.remote(3)
ref2 = square.remote(4)
ref3 = add.remote(ref1, ref2)  # 依赖 ref1 和 ref2
print(nanoray.get(ref3))  # 25

# Actor
@nanoray.remote
class Counter:
    def __init__(self):
        self.n = 0
    def increment(self):
        self.n += 1
        return self.n

counter = Counter.remote()
refs = [counter.increment.remote() for _ in range(5)]
print(nanoray.get(refs))  # [1, 2, 3, 4, 5]（顺序执行）

nanoray.shutdown()
```

#### 4.2.2 actor.py — Actor 抽象

```python
class ActorHandle:
    """远程 Actor 的本地代理。"""
    def __init__(self, cls, actor_id, worker_id):
        self._cls = cls
        self._actor_id = actor_id
        self._worker_id = worker_id  # actor 绑定到固定 worker

    def __getattr__(self, method_name):
        def _remote_call(*args, **kwargs):
            # 提交 actor method task，指定必须在 actor 所在 worker 执行
            return _submit_actor_task(
                self._actor_id, self._worker_id, method_name, args, kwargs
            )
        _remote_call.remote = _remote_call
        return _remote_call
```

Actor 关键约束：

- actor 的所有方法调用必须**顺序执行**（同一个 actor 不能并发）
- actor 绑定到创建时分配的 worker，不迁移
- actor 状态存在 worker 进程内存中，不在 object store

#### 4.2.3 dag.py — DAG 依赖解析

```python
def extract_dependencies(args, kwargs) -> list[ObjectRef]:
    """从函数参数中递归提取所有 ObjectRef。
    这些 ObjectRef 就是当前 task 的依赖。
    """

def resolve_args(args, kwargs, object_store) -> tuple:
    """将参数中的 ObjectRef 替换为实际值。
    调用 object_store.get() 获取每个依赖的值。
    """
```

#### 4.2.4 _fallback/ — 纯 Python 后备实现

Phase 1 使用的纯 Python 实现，Phase 2 后被 Rust 模块替换。保留这些实现的目的：

1. 作为 Rust 模块的功能参考
2. 在没有编译 Rust 的环境下也能运行（降级模式）

```python
# _fallback/object_store.py
class ObjectStore:
    """基于 dict + threading.Event 的简单实现"""

# _fallback/scheduler.py
class Scheduler:
    """基于 queue.Queue 的简单实现"""

# _fallback/ownership.py
class OwnershipTable:
    """基于 dict + threading.Lock 的简单实现"""
```

### 4.3 PyO3 桥接层规范

文件：`rust/nano_ray_core/src/lib.rs`

```rust
use pyo3::prelude::*;

// 导出给 Python 的类
#[pyclass]
pub struct PyObjectRef { /* wraps ObjectId */ }

// 导出给 Python 的函数
#[pyfunction]
fn init_runtime(num_workers: usize) -> PyResult<()>;

#[pyfunction]
fn submit_task(
    function_bytes: &[u8],         // pickle 后的函数
    args: Vec<PyObject>,           // 值或 PyObjectRef 的混合列表
) -> PyResult<PyObjectRef>;

#[pyfunction]
fn get_object(py: Python, obj_ref: &PyObjectRef, timeout_ms: Option<u64>) -> PyResult<PyObject>;

#[pyfunction]
fn shutdown_runtime() -> PyResult<()>;

// 关键：在阻塞等待时释放 GIL
// py.allow_threads(|| { /* Rust 层阻塞 */ })
```

**PyO3 规范要点**：

- 所有可能阻塞的操作必须用 `py.allow_threads()` 释放 GIL
- Python → Rust 传递大对象时用 `&[u8]` 避免拷贝
- Rust → Python 返回大对象时用 `PyBytes::new()` 零拷贝
- 错误处理统一用 `PyResult`，Rust 错误转为 Python 异常

---

## 5. 开发计划

### Phase 0：项目骨架（预计 2-3 天）

**目标**：项目结构搭建完成，Python 能成功调用 Rust 函数。

**任务清单**：

- [ ] 初始化 Git 仓库
- [ ] 创建 `pyproject.toml`，配置 maturin 构建
- [ ] 创建 Rust workspace：`Cargo.toml` + `rust/nano_ray_core/`
- [ ] 在 `lib.rs` 中用 PyO3 导出一个 hello world 函数
- [ ] 创建 `python/nano_ray/__init__.py`，导入 `nano_ray_core`
- [ ] 验证：`python -c "import nano_ray"` 成功
- [ ] 配置基本 CI：`cargo test` + `pytest` + `maturin develop`
- [ ] 创建 `docs/design.md` 骨架

**关键依赖版本**：

```toml
# Cargo.toml
[dependencies]
pyo3 = { version = "0.22", features = ["extension-module"] }
dashmap = "6"
bytes = "1"
crossbeam = "0.8"
tokio = { version = "1", features = ["full"] }  # Phase 4 才需要 full

# pyproject.toml
[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"

[tool.maturin]
features = ["pyo3/extension-module"]
module-name = "nano_ray._core"
```

**验收标准**：

```python
from nano_ray._core import hello
assert hello() == "nano-ray is alive!"
```

---

### Phase 1：最小内核 — 纯 Python 实现（预计 4-5 天）

**目标**：用纯 Python 跑通 `f.remote()` → `nanoray.get()` 的完整流程。

**任务清单**：

- [ ] 实现 `ObjectRef` 类（python/nano_ray/api.py）
- [ ] 实现 `@remote` 装饰器，返回 `RemoteFunction` 包装器
- [ ] 实现 `RemoteFunction.remote(*args)` → 提交 task，返回 `ObjectRef`
- [ ] 实现 `_fallback/object_store.py`：dict + threading.Event
- [ ] 实现 `_fallback/scheduler.py`：queue.Queue + 依赖检查
- [ ] 实现 `_fallback/ownership.py`：dict 记录 owner + 引用计数
- [ ] 实现 Worker 进程：`multiprocessing.Process`，循环从队列取 task 执行
- [ ] 实现 `driver.py`：启动 worker 进程池，管理运行时生命周期
- [ ] 实现 `nanoray.get()` 阻塞等待
- [ ] 编写 example：`01_hello_remote.py`
- [ ] 编写 example：`02_pi_estimation.py`
- [ ] 编写基础测试

**进程间通信方案**（Phase 1 简化版）：

```
Driver 进程
├── task_queue (multiprocessing.Queue): Driver → Worker 分发 task
├── result_queue (multiprocessing.Queue): Worker → Driver 回报结果
└── Worker 进程 × N
    └── 循环: 从 task_queue 取 task → 执行 → 结果放入 result_queue
```

**验收标准**：

```python
import nano_ray as nanoray
nanoray.init(num_workers=2)

@nanoray.remote
def add(a, b):
    return a + b

ref = add.remote(1, 2)
assert nanoray.get(ref) == 3

# 并行
refs = [add.remote(i, i) for i in range(10)]
results = nanoray.get(refs)
assert results == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]

nanoray.shutdown()
```

---

### Phase 2：Rust 核心替换（预计 5-7 天）

**目标**：用 Rust 实现 ObjectStore、OwnershipTable、SchedulerCore，通过 PyO3 替换 Phase 1 的 Python 实现。**功能不变，性能提升**。

**任务清单**：

- [ ] 实现 `types.rs`：TaskId, ObjectId, TaskSpec 等
- [ ] 实现 `object_store.rs`：DashMap + Arc\<Bytes\> + 等待通知机制
- [ ] 为 ObjectStore 编写 Rust 单元测试
- [ ] 实现 `ownership.rs`：并发安全的 task/object 跟踪
- [ ] 为 OwnershipTable 编写 Rust 单元测试
- [ ] 实现 `scheduler.rs`：无锁 ready queue + 依赖触发
- [ ] 为 SchedulerCore 编写 Rust 单元测试
- [ ] 实现 `serializer.rs`：初期直接透传 pickle bytes
- [ ] 更新 `lib.rs`：通过 PyO3 导出上述模块
- [ ] 修改 Python 层，检测 Rust 模块可用时自动切换：

  ```python
  try:
      from nano_ray._core import ObjectStore, OwnershipTable, SchedulerCore
      _USE_RUST = True
  except ImportError:
      from nano_ray._fallback import ObjectStore, OwnershipTable, Scheduler
      _USE_RUST = False
  ```

- [ ] 编写 benchmark：对比 Python vs Rust 实现的 task 吞吐量
- [ ] 确保所有 Phase 1 测试仍通过

**验收标准**：

1. 所有 Phase 1 测试通过
2. benchmark 显示 task 提交吞吐量有显著提升（预期 3-10x）

---

### Phase 3：Task DAG + Actor（预计 5-7 天）

**目标**：支持 ObjectRef 作为参数传递（task 链），支持 Actor 模型。

**任务清单**：

- [ ] 修改 `submit_task`：识别参数中的 ObjectRef，提取为依赖
- [ ] 实现 `dag.py`：`extract_dependencies()` + `resolve_args()`
- [ ] Rust scheduler 支持 `on_object_ready()` 触发等待中的 task
- [ ] 编写 example：`03_task_chain.py`
- [ ] 实现 `actor.py`：ActorHandle 代理
- [ ] 实现 Actor 的 worker 绑定 + 顺序执行保证
- [ ] 编写 example：`05_parameter_server.py`
- [ ] 实现 `serializer.rs` 的 numpy 快速路径（可选优化）
- [ ] 编写 example：`04_word_count.py`（使用 task 链）
- [ ] 全面测试

**验收标准**：

```python
# Task 链
ref1 = square.remote(3)
ref2 = square.remote(4)
ref3 = add.remote(ref1, ref2)  # 自动识别依赖
assert nanoray.get(ref3) == 25

# Actor 顺序执行
counter = Counter.remote()
refs = [counter.increment.remote() for _ in range(5)]
assert nanoray.get(refs) == [1, 2, 3, 4, 5]
```

---

### Phase 4：多节点通信（预计 7-10 天）

**目标**：支持 2-3 个节点组成集群，task 可以跨节点调度。

**任务清单**：

- [ ] 实现 `transport.rs`：基于 tokio + tonic (gRPC) 或自定义 TCP 协议
- [ ] 定义 protobuf / 消息格式
- [ ] 实现节点发现（简单方案：启动时指定 head node 地址）
- [ ] 实现跨节点对象传输
- [ ] 实现两层调度：本地调度器 + 全局调度
- [ ] 修改 `init()` 支持 `address` 参数
- [ ] 编写多节点启动脚本
- [ ] 测试：2 节点集群跑通 word_count

**验收标准**：

```bash
# Node 1 (head)
python -m nano_ray.start --head --port 6379 --num-workers 2

# Node 2
python -m nano_ray.start --address 192.168.1.100:6379 --num-workers 2

# Client
nanoray.init(address="192.168.1.100:6379")
# ... task 可以跨节点执行
```

---

### Phase 5：Dashboard + 打磨（预计 5-7 天）

**目标**：可视化 + 文档 + 打磨。

**任务清单**：

- [ ] 实现简单的 Web Dashboard（React 或纯 HTML）：
  - Task DAG 可视化
  - Worker 状态（空闲/忙碌）
  - Object Store 内存使用
  - Task 延迟直方图
- [ ] 完善 `docs/design.md`：每个设计决策的 why
- [ ] 编写 README.md：项目介绍 + 快速开始 + 架构图
- [ ] 完善 benchmark 报告
- [ ] 代码清理 + 注释补全

---

## 6. 编码规范

### 6.1 Rust 规范

- 使用 `rustfmt` 格式化，`clippy` 检查
- 错误处理：使用 `thiserror` 定义错误类型，不用 `unwrap()`（测试除外）
- 并发原语：优先 `DashMap` > `RwLock` > `Mutex`
- 所有公开 API 写文档注释（`///`）
- 性能关键路径避免分配：优先 `&[u8]`、`Bytes`、`Arc`

### 6.2 Python 规范

- 类型标注：所有公开函数加 type hints
- docstring：Google 风格
- 用 `ruff` 做 lint + 格式化
- 测试：用 `pytest`

### 6.3 提交规范

```
feat(object_store): implement DashMap-based concurrent object storage
fix(scheduler): resolve deadlock in dependency tracking
docs(design): add ownership model trade-off analysis
bench(throughput): add task submission benchmark
```

在每次做完可拆解的一次修改和变更后，请确保所有的代码格式检查和测试都通过，然后再发起一个git commit，并且在 commit message 中说明修改了什么内容，为什么修改，以及和 Ray 的设计有什么异同（如果有的话）。这样可以帮助我们更好地追踪设计决策和实现细节。
请注意：不需要将你纳入coauthors，请考虑只有我在这个项目中进行提交。谢谢！

---

## 7. Agent 工作流程指引

### 7.1 开始新任务前

1. 确认当前处于哪个 Phase
2. 查看该 Phase 的任务清单，找到下一个未完成的任务
3. 如果需要修改已有代码，先理解相关模块的接口和约束

### 7.2 实现新模块时

1. 先写接口（函数签名 + docstring / 文档注释）
2. 再写测试（至少覆盖正常路径 + 一个边界情况）
3. 最后写实现
4. 运行测试确认通过

### 7.3 Rust ↔ Python 交互时

1. 先在 Rust 侧实现功能 + 单元测试
2. 再写 PyO3 桥接代码
3. 最后写 Python 侧的调用代码 + 集成测试
4. 注意：阻塞操作必须 `py.allow_threads()`

### 7.4 做设计决策时

每个非显而易见的设计决策都要在 `docs/design.md` 或代码注释中记录：

- **决策**：我们选择了 X
- **替代方案**：也可以用 Y
- **理由**：X 更适合 nano-ray 因为 ...
- **与 Ray 的对比**：Ray 选择了 Z，因为 ...，nano-ray 简化为 X 因为 ...

### 7.5 遇到不确定的问题时

优先级排序：

1. 查看本文件中是否有明确规范
2. 参考 Ray 的设计论文（Ray: A Distributed Framework for Emerging AI Applications）
3. 选择更简单的方案，在注释中标注 `// SIMPLIFICATION: ...` 或 `# SIMPLIFICATION: ...`
4. 如果涉及架构级决策，停下来记录问题，等待人工确认

---

## 8. 参考资料

- Ray 论文：[Ray: A Distributed Framework for Emerging AI Applications (OSDI 2018)](https://www.usenix.org/conference/osdi18/presentation/moritz)
- Ray 架构白皮书：[Ray v2 Architecture](https://docs.google.com/document/d/1lAy0Owi-vPz2jEqBSaHNQcy2IBSDEHyXNOQZlGuj93c)
- Ray 源码：[github.com/ray-project/ray](https://github.com/ray-project/ray)
- PyO3 文档：[pyo3.rs](https://pyo3.rs/)
- maturin 文档：[maturin.rs](https://www.maturin.rs/)
- DashMap：[docs.rs/dashmap](https://docs.rs/dashmap/)
