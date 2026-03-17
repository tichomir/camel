# Changelog

All notable changes to CaMeL are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
CaMeL uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-03-17

### Milestone 1 — Foundation

This release delivers the complete Milestone 1 foundation: the capability
system, AST interpreter, and dependency graph — integrated into a single
coherent `camel` package with a stable public API.

#### Added

**CaMeLValue & Capability System** (`camel/value.py`)

- `CaMeLValue` — frozen dataclass wrapping any Python value with capability
  metadata: `sources` (origin labels), `inner_source` (sub-field label), and
  `readers` (authorised audience).
- `Public` singleton (`_PublicType`) — open-readers sentinel; absorbing under
  union semantics.
- `Readers` type alias — `frozenset[str] | _PublicType`.
- `wrap()` — convenience constructor with sensible defaults.
- `raw_value()` / `CaMeLValue.raw` — sanctioned accessors for stripping the
  capability wrapper before tool execution.
- Capability propagation functions:
  - `propagate_assignment` — preserves metadata across simple assignments.
  - `propagate_binary_op` — unions capabilities of both operands.
  - `propagate_list_construction` — unions capabilities of all list elements.
  - `propagate_dict_construction` — unions capabilities of all keys and values.
  - `propagate_subscript` — unions capabilities of container and key.

**AST Interpreter** (`camel/interpreter.py`)

- `CaMeLInterpreter` — AST-walking interpreter for the restricted CaMeL
  pseudo-Python grammar subset, using Python's `ast` library.
- Supported statement types: `Assign`, `AugAssign`, `If`, `For`, `Expr`.
- Supported expression types: `Constant`, `Name`, `BinOp`, `UnaryOp`,
  `BoolOp`, `Compare`, `Call`, `Attribute`, `Subscript`, `List`, `Tuple`,
  `Dict`, `JoinedStr` (f-strings).
- All runtime values stored as `CaMeLValue` throughout execution.
- Session state (variable store) persists across multiple `exec()` calls on
  the same interpreter instance.
- `UnsupportedSyntaxError` — raised for any grammar outside the supported
  subset, with offending node type and line number.
- `PolicyViolationError` — raised when a registered security policy blocks a
  tool call.
- `ExecutionMode` enum — `NORMAL` / `STRICT` per-session configuration flag.

**Dependency Graph** (`camel/dependency_graph.py`)

- `DependencyGraph` — immutable frozen snapshot of upstream variable
  dependencies: `variable`, `direct_deps`, `all_upstream`, `edges`.
- `_InternalGraph` — mutable session-scoped graph maintained by the
  interpreter; not part of the public API.
- `NORMAL` mode — records data-assignment dependencies only.
- `STRICT` mode — additionally propagates `if`-test and `for`-iterable
  variable references as dependencies on all variables assigned within those
  blocks, closing timing side-channel vectors.
- `CaMeLInterpreter.get_dependency_graph(variable)` — public query API
  returning a `DependencyGraph` snapshot.

**Q-LLM Wrapper** (`camel/llm/`)

- `QLLMWrapper` — schema-validated structured-output wrapper for the
  Quarantined LLM component; enforces Pydantic output schemas.
- `have_enough_information` field support with `NotEnoughInformationError`.
- Backend adapters: `ClaudeAdapter`, `GeminiAdapter`.
- `LLMProtocol` — structural protocol for backend interoperability.

**Package Integration** (`camel/__init__.py`, `pyproject.toml`)

- Unified `camel` package exporting all Milestone 1 public APIs from a single
  import path.
- `get_dependency_graph(interpreter, variable)` — module-level helper wrapping
  `CaMeLInterpreter.get_dependency_graph`.
- `__version__ = "0.1.0"`.
- `pyproject.toml` with `setuptools` build backend, pinned `requires-python
  >=3.11`, and `dev` extras group.

#### Architecture Decision Records

- `docs/adr/001-q-llm-isolation-contract.md`
- `docs/adr/002-camelvalue-capability-system.md`
- `docs/adr/003-ast-interpreter-architecture.md`
- `docs/adr/004-dependency-graph-architecture.md`

---

## [Unreleased]

_Milestone 2 — Dual LLM & Interpreter integration (P-LLM wrapper, tool
executor, end-to-end pipeline)._
