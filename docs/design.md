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

---

## 2. Ownership Model

*To be written in Phase 1–2 as the implementation takes shape.*

---

## 3. Object Store

*To be written in Phase 1–2.*

---

## 4. Scheduler

*To be written in Phase 1–2.*

---

## 5. Serialization Strategy

*To be written in Phase 2.*

---

## 6. Actor Model

*To be written in Phase 3.*

---

## 7. Transport Layer

*To be written in Phase 4.*
