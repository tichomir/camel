# CaMeL — Local Development Setup

**Version:** 0.2.0 (Milestone 2)
**Date:** 2026-03-17

This guide walks a new contributor through cloning the repository, installing
dependencies, configuring LLM backend credentials, and verifying the full test
suite passes locally.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone and Install](#2-clone-and-install)
3. [Environment Variable Configuration](#3-environment-variable-configuration)
4. [Running the Test Suite](#4-running-the-test-suite)
5. [Pre-commit Hooks](#5-pre-commit-hooks)
6. [Benchmarks](#6-benchmarks)

---

## 1. Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Python | **3.11** | `match`/`case`, `tomllib`, `Self` type alias are all 3.11+ |
| pip | 23+ | `pip install --upgrade pip` if unsure |
| git | any recent | For cloning and pre-commit hooks |

Verify your Python version:

```bash
python --version   # must print 3.11.x or later
```

---

## 2. Clone and Install

### 2.1 Clone

```bash
git clone <repo-url> camel
cd camel
```

### 2.2 Install the package with dev extras

```bash
pip install -e ".[dev]"
```

This installs the `camel` package in editable mode together with all
development tools: `pytest`, `pytest-asyncio`, `mypy`, `ruff`, `black`,
`pre-commit`, and `interrogate`.

**Provider SDKs** are optional at install time (the adapters lazy-import them):

```bash
# To use the Claude backend:
pip install anthropic>=0.25

# To use the Gemini backend:
pip install google-generativeai>=0.7
```

### 2.3 Verify the installation

```bash
python -c "import camel; print(camel.__version__)"
# expected output: 0.1.0 (or later)
```

---

## 3. Environment Variable Configuration

LLM backends read API keys from environment variables.  No key is needed to run
the pure-unit test suite (which uses mock backends); keys are only required for
integration/E2E tests that call real provider APIs.

### 3.1 Claude (Anthropic)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key. Required by `ClaudeBackend`. |
| `CAMEL_CLAUDE_MODEL` | Optional override for the model ID (default: `claude-opus-4-6`). |

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export CAMEL_CLAUDE_MODEL="claude-sonnet-4-6"   # optional
```

### 3.2 Gemini (Google)

| Variable | Description |
|---|---|
| `GOOGLE_API_KEY` | Google API key. Required by `GeminiBackend`. |
| `CAMEL_GEMINI_MODEL` | Optional override for the model ID (default: `gemini-2.0-flash`). |

```bash
export GOOGLE_API_KEY="AI..."
export CAMEL_GEMINI_MODEL="gemini-2.0-flash"    # optional
```

### 3.3 Selecting a backend at runtime

The `get_backend()` factory reads the provider string and any overrides from
the call site (or from env vars if the caller passes `None`):

```python
from camel.llm.backend import get_backend

# Picks up ANTHROPIC_API_KEY automatically:
claude_backend = get_backend("claude")

# Explicit key + model override:
gemini_backend = get_backend(
    "gemini",
    api_key="AI...",
    model="gemini-2.0-flash",
)
```

### 3.4 Switching backends without code changes

Set the environment variable `CAMEL_LLM_PROVIDER` and pass it into the
orchestrator or wrapper at construction time:

```bash
export CAMEL_LLM_PROVIDER=gemini
export GOOGLE_API_KEY="AI..."
```

```python
import os
from camel.llm.backend import get_backend

backend = get_backend(os.environ.get("CAMEL_LLM_PROVIDER", "claude"))
```

No other code changes are required — the `LLMBackend` protocol is identical for
both providers.

---

## 4. Running the Test Suite

All test commands assume you are in the repository root with the dev extras
installed.

### 4.1 Full suite (unit + integration + E2E)

```bash
pytest
```

`pyproject.toml` configures `testpaths = ["tests"]` and `asyncio_mode = "auto"`
so async tests run automatically without extra flags.

### 4.2 Unit tests only (no LLM calls)

```bash
pytest tests/test_value.py \
       tests/test_interpreter.py \
       tests/test_dependency_graph.py \
       tests/llm/test_backend.py \
       tests/llm/test_pllm.py \
       tests/llm/test_qllm.py \
       tests/test_qllm.py \
       tests/test_qllm_wrapper.py \
       tests/test_qllm_validation.py
```

### 4.3 Integration tests

```bash
pytest tests/test_execution_loop.py \
       tests/test_p_llm_isolation.py \
       tests/test_redaction_completeness.py \
       tests/test_isolation_harness.py
```

### 4.4 End-to-end scenario tests

```bash
pytest tests/test_e2e_scenarios.py
```

### 4.5 Multi-backend swap test

```bash
pytest tests/test_multi_backend_swap.py tests/test_backend_swap.py
```

### 4.6 Exit criteria validation suite

```bash
pytest tests/test_exit_criteria.py -v
```

### 4.7 With coverage

```bash
pytest --cov=camel --cov-report=term-missing
```

### 4.8 Linting and type-checking

```bash
ruff check .           # linting
black --check .        # formatting check
mypy camel/            # static type checking
interrogate camel/     # docstring coverage (must be ≥90%)
```

Run all checks in one shot (mirrors CI):

```bash
ruff check . && black --check . && mypy camel/ && interrogate camel/
```

---

## 5. Pre-commit Hooks

Pre-commit hooks enforce lint, formatting, and type checks before every commit.

### Install hooks once

```bash
pre-commit install
```

### Run hooks manually (without committing)

```bash
pre-commit run --all-files
```

Hooks configured: `ruff`, `black`, `mypy`.

---

## 6. Benchmarks

An interpreter performance benchmark script is included at
`scripts/benchmark_interpreter.py`.  It measures interpreter overhead per
simulated tool-call step (NFR-4 target: ≤100 ms).

```bash
python scripts/benchmark_interpreter.py
```

The script prints median and p95 execution times.  No LLM calls are made.
