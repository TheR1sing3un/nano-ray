# nano-ray Design Document

This document records the key design decisions in nano-ray and explains
*why* each choice was made, with comparisons to Ray where relevant.

---

## 1. Overall Architecture

**Decision**: Python + Rust hybrid via PyO3/maturin.

- Python handles the user-facing API layer (`@remote`, `get()`, `ActorHandle`).
- Rust implements the performance-critical internals (object store, ownership
  table, scheduler core).

**Why**: The user experience of Ray lives in Python (decorators, dynamic
attribute access, seamless integration with numpy/pickle). The systems backbone
of Ray lives in C++ for performance. nano-ray mirrors this split but uses Rust
for memory safety and simpler build tooling.

**Compared to Ray**: Ray uses C++ with a custom build system. We use Rust +
maturin for a friendlier developer experience while still achieving zero-copy
and lock-free concurrency.

**Alternative**: Pure Python would be simpler but too slow for the hot path
(object store lookups, scheduler ready-queue operations). Pure Rust would lose
the Python decorator ergonomics that make Ray's API feel natural.

---

## 2. Ownership Model

**Decision**: Simplified centralized ownership in the driver process.

In nano-ray, the driver process owns all tasks and their result objects.
The `OwnershipTable` tracks:
- Task → owner mapping (always the driver PID in single-node mode)
- Task status lifecycle: `Pending → Running → Finished/Failed`
- Object → producer task mapping
- Reference counts for objects
- Lineage information (serialized function + args) for future fault tolerance

**Why**: Ray's ownership model is its most innovative design — whoever submits
a task owns it, enabling fully decentralized metadata management. However,
implementing true decentralized ownership requires distributed reference
counting, distributed garbage collection, and handling owner failures — all of
which are production-level complexities.

nano-ray preserves the *interface* of ownership (OwnershipTable with owner_id,
ref_count, lineage fields) to demonstrate the concept, but simplifies to a
single owner (the driver). This lets us show *what* ownership tracks without
the complexity of *distributed* ownership.

**Compared to Ray**:
- Ray: Any worker can be an owner. Owner tracks task state and handles retries.
  If an owner dies, its tasks and objects become unrecoverable (without lineage
  reconstruction).
- nano-ray: Driver is the sole owner. Simpler but creates a single point of
  failure and a potential metadata bottleneck.

**Lineage preservation**: We store `serialized_args` (the full lineage) in
each `TaskEntry` — both in the Python fallback and the Rust backend. This
lineage is actively used for object reconstruction (see Section 10).

---

## 3. Object Store

**Decision**: `DashMap<u64, PyObject>` in Rust, with `parking_lot::Condvar`
for blocking waits and GIL release.

The object store is nano-ray's data plane — it holds task results as immutable
Python objects. Key design choices:

### 3.1 Concurrency: DashMap

We use `dashmap::DashMap` instead of `HashMap + Mutex` because:
- Multiple threads (result collector, user's get() calls) access the store
  concurrently
- DashMap provides fine-grained locking (sharded internally), so a `put()` on
  one object doesn't block a `get()` on another

**Compared to Ray**: Ray's object store (Plasma) uses shared memory with
mmap for zero-copy access. nano-ray stores Python objects directly (via
PyO3's `PyObject`), which is simpler but doesn't support zero-copy across
processes.

### 3.2 Blocking Wait: Condvar with GIL Release

`get_or_wait()` is the most performance-sensitive operation. When a result
isn't ready yet, we:

1. Check if the object exists (fast path, no blocking)
2. Register a notification handle (Condvar)
3. Double-check after registration (prevents TOCTOU race)
4. Release the GIL via `py.allow_threads()` and wait on the Condvar

The GIL release is critical: without it, a Python thread calling `get()` would
hold the GIL while sleeping, blocking all other Python threads.

**Compared to Ray**: Ray uses a `Get()` RPC to the local object store, which
returns immediately if the object is local, or triggers a pull from the remote
location. nano-ray's single-node model simplifies to a direct in-process wait.

### 3.3 Python Fallback

The pure Python fallback uses `dict + threading.Event`:
- `threading.Event` per object for blocking waits
- Simple `dict` for storage with a global `threading.Lock`

This is functionally correct but slower due to the GIL and coarser locking.

---

## 4. Scheduler

**Decision**: Lock-free `SegQueue` for the ready queue, `DashMap` for
dependency tracking, with an `on_object_ready()` trigger mechanism.

### 4.1 Ready Queue: crossbeam SegQueue

The ready queue is the scheduler's hot path — tasks are pushed when
dependencies resolve and popped by the driver (for dispatch to workers). We use
`crossbeam::queue::SegQueue` because:
- Lock-free: multiple threads can push/pop concurrently without contention
- Unbounded: no capacity planning needed
- FIFO: tasks execute roughly in submission order

**Alternative**: `std::collections::VecDeque + Mutex` would be simpler but
creates a bottleneck when the result collector and user thread both interact
with the scheduler.

### 4.2 Dependency Resolution

When a task is submitted with `ObjectRef` arguments, the scheduler:
1. Counts unresolved dependencies (`unresolved_deps: AtomicUsize`)
2. Builds a reverse index: `object_id → [waiting_task_ids]`
3. When `on_object_ready(object_id)` is called (by the result collector),
   decrements `unresolved_deps` for each waiting task
4. When `unresolved_deps` reaches 0, moves the task to the ready queue

This is an efficient O(1) per dependency resolution, using atomic operations
to avoid locks.

**Race condition fix**: A critical edge case occurs when a task is submitted
with dependencies that are already resolved (e.g., actor method chains where
`get()` is called between submissions). The `on_object_ready()` for those
objects already fired before the task was registered. We handle this by
re-triggering `on_object_ready()` for any already-available dependencies
after `scheduler.submit()`.

**Compared to Ray**: Ray's scheduler is two-layered (local raylet + global).
The local raylet schedules tasks that can run locally (data locality), and
forwards others to the global scheduler. nano-ray uses a single flat scheduler,
which is simpler but doesn't optimize for data locality.

---

## 5. Serialization Strategy

**Decision**: cloudpickle for function/argument serialization, with Rust doing
byte-level pass-through.

### 5.1 Why cloudpickle

Python's standard `pickle` can't serialize closures, lambdas, or functions
defined in `__main__`. Since `@remote` functions are typically defined in user
scripts, we need cloudpickle which handles these cases.

**Compared to Ray**: Ray also uses cloudpickle for the same reason, plus a
custom serializer for numpy arrays (Apache Arrow format via Plasma).

### 5.2 Serialization Points

Serialization happens at two points:
1. **Task submission** (`_flush_ready_tasks`): function + resolved args are
   pickled into a single `bytes` payload and sent to the worker queue
2. **Result return** (`_worker_loop`): the worker pickles the return value
   and sends it back via the result queue

We intentionally defer serialization to `_flush_ready_tasks()` rather than at
`submit_task()` time. This is because ObjectRefs in the arguments need to be
resolved to actual values first, which can only happen after dependencies are
satisfied.

### 5.3 Rust Serializer (Placeholder)

The `serializer.rs` module currently provides pass-through functions
(`wrap_bytes`/`unwrap_bytes`). A future optimization would add a numpy fast
path that serializes arrays as raw bytes + metadata, bypassing pickle overhead.

---

## 6. Actor Model

**Decision**: Two-task pattern with state chaining for sequential execution.

### 6.1 The Problem

Actors need sequential execution guarantees: if you call `counter.increment()`
five times, the results must be `[1, 2, 3, 4, 5]`, not some arbitrary
interleaving. But nano-ray's scheduler dispatches tasks to any available worker.

### 6.2 The Solution: State Chaining

Each actor method call produces two tasks:

1. **State task**: Takes the previous state ref (or initial state) as input,
   executes the method, returns `(new_state, return_value)` tuple.
   This task depends on the previous state task, forming a chain.

2. **Value task**: Takes the state task's output and extracts just the
   `return_value` for the user. This is what the user's ObjectRef points to.

```
submit(inc) → state_ref_1 → submit(inc, depends=state_ref_1) → state_ref_2 → ...
                   ↓                                               ↓
             value_task_1 → user_ref_1                       value_task_2 → user_ref_2
```

The dependency chain ensures sequential execution: state_task_2 can't start
until state_task_1 completes, because it needs the output state.

**Why two tasks?** If we only had one task returning `(state, value)`, the user
would receive the full `(state, value)` tuple instead of just the value. The
value extraction task separates the internal state plumbing from the user-
visible result.

**Compared to Ray**: Ray assigns each actor to a dedicated worker process. The
worker maintains the actor's state in memory and processes method calls
sequentially from a per-actor queue. This is more efficient (no state
serialization between calls) but requires worker affinity management.

nano-ray's approach is more elegant in its simplicity: actors are just DAGs of
tasks with dependency chains, using the same scheduler infrastructure as
regular tasks. The tradeoff is serialization overhead for the state on every
method call.

### 6.3 ActorHandle

The `ActorHandle` is a client-side proxy that:
- Maintains `_last_ref`: the ObjectRef of the most recent state task
- Uses `_call_lock` to serialize method submissions (preventing interleaving
  of the two-task pattern)
- Exposes methods via `__getattr__` that return `_ActorMethodCaller` objects
  with a `.remote()` method

---

## 7. Transport Layer

**Decision**: Length-prefixed TCP with cloudpickle serialization.

### 7.1 Wire Protocol

Each message is sent as:
```
[4 bytes: message length N (big-endian)] [N bytes: cloudpickle payload]
```

This is the simplest reliable framing protocol. The length prefix eliminates
the need for delimiter-based parsing and handles arbitrary binary payloads.

**Compared to Ray**: Ray uses gRPC with protobuf for the control plane (task
submission, metadata queries) and a custom direct-transfer protocol for the
data plane (large object transfers). This gives Ray type safety, versioning,
and efficient large-object streaming. nano-ray uses a single protocol for
everything, trading these properties for implementation simplicity.

### 7.2 Multi-Node Architecture

```
Client ──TCP──> Head Node <──TCP── Worker Node 1
                  |                Worker Node 2
                  |                Worker Node N
                  v
              Scheduler
              ObjectStore
              OwnershipTable
```

- **Head node**: Central coordinator. Manages all scheduling, dependency
  resolution, and result storage. Analogous to Ray's GCS (Global Control
  Store), but also handles scheduling (which Ray delegates to raylets).

- **Worker node**: Connects to head, polls for ready tasks, executes them
  locally, reports results. Analogous to a simplified Ray raylet without
  local scheduling.

- **Client (RemoteRuntime)**: Connects to head, submits tasks and retrieves
  results. Same interface as the local `Runtime`, enabling transparent
  switching between local and cluster modes.

### 7.3 Task Dispatch: Pull Model

Worker nodes poll the head for tasks (`poll_task` → `pop_ready_task`), rather
than the head pushing tasks to workers. This is simpler because:
- No need for the head to maintain per-worker queues
- Workers self-regulate based on their capacity
- Disconnected workers naturally stop receiving tasks

**Tradeoff**: Polling introduces latency (up to `poll_interval` delay). For
an educational project, 50ms poll interval is acceptable.

**Compared to Ray**: Ray's raylets receive tasks via gRPC push from the driver
or other raylets. This eliminates polling latency but requires connection
management and flow control.

### 7.4 ObjectRef Resolution

A key design decision: ObjectRefs are resolved on the head node, not on
worker nodes. When a task with ObjectRef dependencies becomes ready:

1. Head resolves each ObjectRef to its actual value (`get_or_wait`)
2. Head serializes the fully resolved `(func, args, kwargs)` tuple
3. Head sends the serialized payload to the worker

This means workers are stateless — they receive fully resolved tasks and
never need to contact the object store. The tradeoff is that all data flows
through the head, creating a potential bandwidth bottleneck for large objects.

**Compared to Ray**: Ray workers can directly fetch objects from other workers'
object stores, bypassing the metadata layer entirely.

---

## 8. Process Model

**Decision**: `multiprocessing.get_context("fork")` for worker processes.

### 8.1 Why Fork

Python's `multiprocessing` supports three start methods:
- `fork`: Child inherits parent's memory. Fast but unsafe with threads.
- `spawn`: Child re-imports modules. Safe but re-executes `__main__`.
- `forkserver`: Compromise between fork and spawn.

We use `fork` because:
- `spawn` causes `__main__` to be re-executed in child processes, which breaks
  when the user's script has top-level code (e.g., `nanoray.init()`)
- `fork` gives us copy-on-write sharing of the function registry
- Worker processes don't create threads, so fork safety is less of a concern

**Compared to Ray**: Ray uses fork on Linux and spawn on macOS (where fork is
deprecated for multi-threaded processes). We suppress the macOS deprecation
warning with `warnings.catch_warnings()`.

### 8.2 IPC: multiprocessing.Queue

Workers communicate with the driver via two `multiprocessing.Queue` instances:
- `task_queue`: Driver → Workers (task dispatch)
- `result_queue`: Workers → Driver (result collection)

This is simple and correct, leveraging Python's built-in IPC. The queues use
pipes internally, with automatic serialization via pickle.

**Compared to Ray**: Ray uses shared memory (Plasma object store) for
zero-copy data sharing between workers on the same node, plus gRPC for
control messages. Our queue-based approach copies data on every transfer
but is much simpler to implement.

---

## 9. Dashboard

**Decision**: Single-file HTML dashboard served by Python's `http.server`.

The dashboard provides real-time cluster monitoring:
- Task statistics (submitted, completed, failed, running)
- Worker count
- Object store size
- Latency histogram
- Recent task completions

It uses a simple polling architecture: the browser fetches `/api/metrics`
every second and updates the UI. No WebSocket or server-sent events needed.

**Compared to Ray**: Ray's dashboard is a full React application with a
backend API, supporting detailed profiling, memory analysis, and log viewing.
nano-ray's dashboard is intentionally minimal — a single HTML file with inline
CSS and JavaScript, demonstrating the concept without framework dependencies.

---

## 10. Lineage-Based Fault Tolerance

**Decision**: Store complete task lineage in OwnershipTable; reconstruct lost
objects by re-executing their producer tasks via the lineage chain.

### 10.1 Why Lineage Instead of Checkpointing

Distributed systems traditionally use checkpointing: periodically save the
full state to durable storage. This is expensive for fine-grained task systems
like Ray, where millions of small objects are created per second.

Ray's insight: since tasks are (ideally) deterministic and objects are
immutable, you don't need to save the data — you just need to remember
*how to recompute it*. This is the task's lineage: the function, arguments,
and dependency relationships that produced it.

**Tradeoff**: Lineage recovery requires re-execution time proportional to
the depth of the lost object's dependency chain. Checkpointing has constant
recovery time but constant overhead. For short-lived, fine-grained tasks,
lineage wins.

### 10.2 What We Store

Each `TaskEntry` in the `OwnershipTable` stores:

```
task_id:          unique identifier
serialized_args:  cloudpickle.dumps((func, args, kwargs))  ← full lineage
dependencies:     [object_id, ...]  ← upstream objects this task needed
result_id:        object_id  ← the object this task produced
```

Each `ObjectEntry` stores:

```
object_id:       unique identifier
producer_task:   task_id  ← which task created this object (reverse lookup)
```

The `serialized_args` contains the *original* arguments including `ObjectRef`s
(not resolved values). This is critical: during reconstruction, `ObjectRef`s
in the arguments trigger recursive dependency reconstruction.

### 10.3 Reconstruction Algorithm

When `get(ref)` discovers an object has been evicted from the object store
but its lineage still exists in the ownership table:

```
reconstruct(object_id):
    1. if object_store.contains(object_id): return  # still available

    2. task_id = ownership.get_producer_task(object_id)
       lineage = ownership.get_task_lineage(task_id)

    3. for dep_id in lineage.dependencies:         # recursive step
           if not object_store.contains(dep_id):
               reconstruct(dep_id)

    4. func, args, kwargs = deserialize(lineage.serialized_args)
       resolve ObjectRefs in args → actual values from object_store
       re-submit (task_id, object_id, func, args, kwargs) to worker queue

    5. object_store.get_or_wait(object_id)          # block until rebuilt
```

**Key properties**:
- **Recursive**: if a dependency is also missing, reconstruct it first
- **Idempotent**: re-executing a deterministic task produces the same result
- **Lazy**: only reconstructs what is actually needed, not the entire DAG

### 10.4 User API

```python
# Simulate object eviction (for testing/demonstration)
nanoray.evict(ref)                    # remove value from object store

# Explicit reconstruction
value = nanoray.reconstruct(ref)      # rebuild via lineage, return value

# Automatic reconstruction (transparent to user)
value = nanoray.get(ref)              # if evicted, auto-reconstructs
```

The `evict()` API exists for educational purposes: it lets users simulate
object loss and observe the reconstruction process. In production Ray,
eviction happens automatically under memory pressure or node failures.

### 10.5 Compared to Ray

| Aspect | Ray | nano-ray |
|--------|-----|----------|
| Trigger | Automatic (node failure, OOM eviction) | Manual `evict()` + auto `get()` |
| Scope | Distributed (cross-node reconstruction) | Single-node only |
| Ownership | Decentralized (owner re-executes) | Centralized (driver re-executes) |
| GC interaction | Lost lineage when owner dies | Lineage never lost (driver is sole owner) |
| Non-deterministic tasks | Marks as non-reconstructable | Assumes determinism |

**SIMPLIFICATION**: Ray handles non-deterministic tasks (e.g., reading from
network, random sampling with different seeds) by marking them as
non-reconstructable via lineage. nano-ray assumes all tasks are deterministic.
This is acceptable for the educational examples but would need refinement for
production use.
