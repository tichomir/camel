# CaMeL — Semantic Versioning Policy

**Document version:** 1.0
**Applies to:** `camel-security` (PyPI package), `camel` (internal implementation package)
**Effective from:** v0.5.0

---

## 1. Overview

CaMeL uses [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html):
`MAJOR.MINOR.PATCH`.

The key principle for a **security library** is that breaking changes to the
security model are treated as **major-version events**, even if the Python API
surface is technically compatible.

---

## 2. What Constitutes Each Release Type

### 2.1 Patch Release (0.5.x → 0.5.1)

A patch release contains **only**:

| Category | Examples |
|---|---|
| Bug fixes | Correcting incorrect capability propagation, fixing policy evaluation logic |
| Documentation improvements | Clarifying docstrings, fixing typos in docs |
| Internal refactors | Code reorganisation with no observable behaviour change |
| Test additions | New test cases covering existing behaviour |
| Dependency version pin updates | Widening or narrowing `pydantic` version range within the same major |

**Patch releases never:**
- Add, remove, or rename any exported name in `camel_security.__all__`
- Add, remove, or rename any field in `AgentResult`
- Change the default value of any parameter in `CaMeLAgent.__init__`
- Alter security model defaults (e.g. default `ExecutionMode`)

### 2.2 Minor Release (0.5.0 → 0.6.0)

A minor release may:

| Category | Examples |
|---|---|
| Add new exported names | New helper functions, new error types, new re-exports |
| Add new `AgentResult` fields | New field with a default value that does not break existing unpacking |
| Add new optional parameters | New kwarg with a default to `CaMeLAgent.__init__`, `Tool`, `agent.run()` |
| Add new `Tool` fields | New optional field with a default value |
| Add new `PolicyRegistry` methods | New query methods that don't affect `evaluate()` behaviour |
| Add new LLM backend adapters | New provider adapter in `camel.llm.adapters` |
| Add new reference policies | New entries in `camel.policy.reference_policies` |
| Expand the builtin allowlist | Adding a new safe builtin to `allowlist.yaml` |

**Minor releases never:**
- Remove or rename any exported name in `camel_security.__all__`
- Remove or rename any field in `AgentResult`
- Remove or rename any parameter of `CaMeLAgent.__init__` or `agent.run()`
- Change the semantics of existing parameters (see §2.3 for details)
- Change security model defaults without user opt-in

### 2.3 Major Release (0.5.0 → 1.0.0)

A major release is required for **any** of the following:

#### API Surface Changes

| Change | Rationale |
|---|---|
| Remove or rename any name from `camel_security.__all__` | Breaks existing import statements |
| Remove or rename any field from `AgentResult` | Breaks field access code |
| Change the type of any `AgentResult` field | Breaks code relying on the existing type |
| Remove or rename any parameter of `CaMeLAgent.__init__` | Breaks existing constructor calls |
| Change a required parameter to positional-only or vice versa | Changes call-site syntax |
| Change the return type of `agent.run()` | Breaks code using the return value |

#### Security Model Changes

| Change | Rationale |
|---|---|
| Change the default `ExecutionMode` from `STRICT` to `NORMAL` | Degrades security posture silently |
| Remove a propagation rule from STRICT mode (M4-F1 through M4-F4) | Removes security guarantee |
| Change policy evaluation semantics (e.g. from all-must-allow to first-wins) | Alters security contract |
| Change `TRUSTED_SOURCE_LABELS` set content | Alters trust boundary |
| Remove any side-channel mitigation | Degrades security guarantees |

#### Interpreter Protocol Changes

| Change | Rationale |
|---|---|
| Remove support for any currently-supported Python grammar construct | Breaks existing P-LLM plans |
| Change the exception type raised for an existing error condition | Breaks `except` clauses |
| Change the `CaMeLValue` dataclass fields | Breaks capability system consumers |
| Remove the `LLMBackend` protocol method `generate` or `generate_structured` | Breaks custom backend implementations |

---

## 3. AgentResult Stability Policy

`AgentResult` is a **frozen dataclass**.  The following rules apply:

1. **Fields listed in the v0.5.0 docstring are permanently stable.**
   They will not be removed or renamed in any minor or patch release.

2. **New fields added in minor releases MUST have default values.**
   This ensures that code constructing `AgentResult` manually (e.g. in tests)
   does not break.

3. **Field types will not be narrowed in minor releases.**
   A field typed `list[Any]` will not be changed to `list[TraceRecord]` in a
   minor release (even though it would be technically backwards-compatible for
   readers) because it changes the type contract for code that constructs
   `AgentResult` instances.

4. **Field semantic changes are major-version events.**
   Even if the field name and type stay the same, a change in what the field
   means (e.g. `loop_attempts` changing from 0-based to 1-based) is a major
   breaking change.

---

## 4. Deprecation Policy

Before removing or renaming any stable API name:

1. The deprecated name MUST be kept for **at least one minor release** with a
   `DeprecationWarning` emitted on use.
2. The deprecation notice MUST appear in the docstring and in `CHANGELOG.md`.
3. The removal MUST be announced in the major-version release notes.

Exception: immediate removal (without deprecation period) is permitted for:
- Security vulnerabilities where the deprecated code path is the vulnerability
- APIs marked `_experimental` or `_preview` in their docstrings

---

## 5. Pre-1.0 Stability Clause

While the package version is `0.x.y`:

- The **minor** version (`0.x`) MAY include breaking changes as defined in §2.3,
  provided they are clearly documented in `CHANGELOG.md` under a "Breaking
  Changes" heading.
- The intent is to reach a stable `1.0.0` API by Milestone 5 completion.
- Users who need stability before `1.0.0` should pin to an exact minor version
  (e.g. `camel-security>=0.5,<0.6`).

---

## 6. Version Bump Checklist

Before tagging a release, confirm:

- [ ] `camel_security/__init__.py` `__version__` is updated
- [ ] `camel/__init__.py` `__version__` is updated to match
- [ ] `pyproject.toml` `[project] version` is updated
- [ ] `CHANGELOG.md` has a dated entry for the new version
- [ ] All breaking changes are listed under "Breaking Changes" in `CHANGELOG.md`
- [ ] `README.md` version badge reflects the new version
- [ ] All deprecation warnings reference the target removal version
- [ ] CI passes on the release tag

---

## 7. Changelog Format

CaMeL follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).  Each
release entry uses the following section headings (omit empty sections):

```
## [X.Y.Z] — YYYY-MM-DD

### Breaking Changes  ← required for major releases
### Deprecated
### Added
### Changed
### Fixed
### Removed
### Security
```

---

_This document is maintained alongside the codebase.  Amendments require a PR
and sign-off from the project lead._
