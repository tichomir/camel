"""Unit tests for the CaMeL Capability Assignment Engine.

Covers:
- CaMeLValue.merge() — union semantics for sources and readers
- default_capability_annotation() — default provenance tagging
- ToolRegistry — registration, unregistration, annotation dispatch
- Interpreter integration via ToolRegistry.as_interpreter_tools()
- Public readers preserved through merge operations
- annotate_read_email — inner_source, per-field wrapping
- annotate_read_document / annotate_get_file — permission-based readers
- register_built_in_tools — registry integration helper
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from camel.capabilities import (
    CaMeLValue,
    CapabilityAnnotationFn,
    Public,
    annotate_get_file,
    annotate_read_document,
    annotate_read_email,
    default_capability_annotation,
    register_built_in_tools,
)
from camel.interpreter import CaMeLInterpreter
from camel.tools import ToolRegistry
from camel.value import wrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cv(
    value: Any,
    sources: frozenset[str],
    readers: Any = None,
    inner_source: str | None = None,
) -> CaMeLValue:
    """Convenience factory."""
    return CaMeLValue(
        value=value,
        sources=sources,
        inner_source=inner_source,
        readers=readers if readers is not None else Public,
    )


# ---------------------------------------------------------------------------
# CaMeLValue.merge()
# ---------------------------------------------------------------------------


class TestCaMeLValueMerge:
    """Tests for CaMeLValue.merge()."""

    def test_merge_sources_union(self) -> None:
        """merge() unions sources from both operands."""
        a = _make_cv("hello", frozenset({"tool_a"}))
        b = _make_cv("world", frozenset({"tool_b"}))
        merged = a.merge(b)
        assert merged.sources == frozenset({"tool_a", "tool_b"})

    def test_merge_preserves_self_value(self) -> None:
        """merge() keeps the value from self, not from other."""
        a = _make_cv(42, frozenset({"a"}))
        b = _make_cv(99, frozenset({"b"}))
        merged = a.merge(b)
        assert merged.value == 42

    def test_merge_readers_union_frozensets(self) -> None:
        """merge() unions frozenset readers from both operands."""
        a = _make_cv("x", frozenset({"t"}), readers=frozenset({"alice@x.com"}))
        b = _make_cv("y", frozenset({"t"}), readers=frozenset({"bob@x.com"}))
        merged = a.merge(b)
        assert merged.readers == frozenset({"alice@x.com", "bob@x.com"})

    def test_merge_public_absorbs_frozenset(self) -> None:
        """Public ∪ frozenset = Public (Public is absorbing)."""
        a = _make_cv("x", frozenset({"t"}), readers=Public)
        b = _make_cv("y", frozenset({"t"}), readers=frozenset({"alice@x.com"}))
        merged = a.merge(b)
        assert merged.readers is Public

    def test_merge_frozenset_absorbs_into_public(self) -> None:
        """frozenset ∪ Public = Public."""
        a = _make_cv("x", frozenset({"t"}), readers=frozenset({"alice@x.com"}))
        b = _make_cv("y", frozenset({"t"}), readers=Public)
        merged = a.merge(b)
        assert merged.readers is Public

    def test_merge_both_public(self) -> None:
        """Public ∪ Public = Public."""
        a = _make_cv("x", frozenset({"t"}), readers=Public)
        b = _make_cv("y", frozenset({"t"}), readers=Public)
        assert a.merge(b).readers is Public

    def test_merge_inner_source_cleared(self) -> None:
        """merge() always sets inner_source=None on the result."""
        a = _make_cv("x", frozenset({"t"}), inner_source="sender")
        b = _make_cv("y", frozenset({"t"}), inner_source="recipient")
        merged = a.merge(b)
        assert merged.inner_source is None

    def test_merge_overlapping_sources(self) -> None:
        """merge() with overlapping sources produces correct union (no duplicates)."""
        a = _make_cv("x", frozenset({"shared", "only_a"}))
        b = _make_cv("y", frozenset({"shared", "only_b"}))
        merged = a.merge(b)
        assert merged.sources == frozenset({"shared", "only_a", "only_b"})

    def test_merge_empty_sources(self) -> None:
        """merge() with empty sources on either side."""
        a = _make_cv("x", frozenset())
        b = _make_cv("y", frozenset({"tool_b"}))
        merged = a.merge(b)
        assert merged.sources == frozenset({"tool_b"})

    def test_merge_both_empty_readers(self) -> None:
        """merge() with empty frozenset readers on both sides = empty frozenset."""
        a = _make_cv("x", frozenset({"t"}), readers=frozenset())
        b = _make_cv("y", frozenset({"t"}), readers=frozenset())
        merged = a.merge(b)
        assert merged.readers == frozenset()

    def test_merge_returns_new_instance(self) -> None:
        """merge() must return a new CaMeLValue, not mutate self."""
        a = _make_cv("x", frozenset({"a"}))
        b = _make_cv("y", frozenset({"b"}))
        merged = a.merge(b)
        assert merged is not a
        assert merged is not b
        # Original values unchanged
        assert a.sources == frozenset({"a"})
        assert b.sources == frozenset({"b"})


# ---------------------------------------------------------------------------
# default_capability_annotation()
# ---------------------------------------------------------------------------


class TestDefaultCapabilityAnnotation:
    """Tests for default_capability_annotation()."""

    def test_sources_set_to_tool_id(self) -> None:
        """sources is {tool_id}."""
        cv = default_capability_annotation("result", {}, "my_tool")
        assert cv.sources == frozenset({"my_tool"})

    def test_readers_is_public(self) -> None:
        """readers is Public."""
        cv = default_capability_annotation("result", {}, "my_tool")
        assert cv.readers is Public

    def test_inner_source_is_none(self) -> None:
        """inner_source is None."""
        cv = default_capability_annotation("result", {}, "my_tool")
        assert cv.inner_source is None

    def test_value_preserved(self) -> None:
        """The raw value is stored unchanged."""
        raw = {"key": "value"}
        cv = default_capability_annotation(raw, {}, "read_email")
        assert cv.value is raw

    def test_dict_return_value(self) -> None:
        """Works with dict return values."""
        raw = {"subject": "Hello", "sender": "bob@example.com"}
        cv = default_capability_annotation(raw, {"email_id": 1}, "read_email")
        assert cv.sources == frozenset({"read_email"})
        assert cv.readers is Public
        assert cv.value == raw

    def test_none_return_value(self) -> None:
        """Works when tool returns None."""
        cv = default_capability_annotation(None, {}, "send_email")
        assert cv.value is None
        assert cv.sources == frozenset({"send_email"})

    def test_tool_kwargs_not_used_by_default(self) -> None:
        """tool_kwargs are accepted but do not affect default annotation."""
        cv1 = default_capability_annotation("v", {"a": 1, "b": 2}, "t")
        cv2 = default_capability_annotation("v", {}, "t")
        assert cv1.sources == cv2.sources
        assert cv1.readers is cv2.readers

    def test_different_tool_ids_produce_different_sources(self) -> None:
        """Each tool_id produces a distinct sources set."""
        cv_a = default_capability_annotation("v", {}, "tool_a")
        cv_b = default_capability_annotation("v", {}, "tool_b")
        assert cv_a.sources != cv_b.sources


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class TestToolRegistryRegister:
    """Tests for ToolRegistry.register()."""

    def test_register_and_contains(self) -> None:
        """Registered tool is in the registry."""
        reg = ToolRegistry()
        reg.register("my_tool", lambda: "val")
        assert "my_tool" in reg

    def test_register_duplicate_raises(self) -> None:
        """Registering the same name twice raises ValueError."""
        reg = ToolRegistry()
        reg.register("t", lambda: None)
        with pytest.raises(ValueError, match="already registered"):
            reg.register("t", lambda: None)

    def test_register_non_callable_raises(self) -> None:
        """Registering a non-callable raises TypeError."""
        reg = ToolRegistry()
        with pytest.raises(TypeError, match="callable"):
            reg.register("t", "not_callable")  # type: ignore[arg-type]

    def test_unregister_removes_entry(self) -> None:
        """unregister() removes the tool."""
        reg = ToolRegistry()
        reg.register("t", lambda: None)
        reg.unregister("t")
        assert "t" not in reg

    def test_unregister_missing_raises(self) -> None:
        """unregister() raises KeyError for unknown name."""
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.unregister("nonexistent")

    def test_names_property(self) -> None:
        """names returns frozenset of registered tool ids."""
        reg = ToolRegistry()
        reg.register("a", lambda: None)
        reg.register("b", lambda: None)
        assert reg.names == frozenset({"a", "b"})

    def test_len(self) -> None:
        """__len__ returns number of registered tools."""
        reg = ToolRegistry()
        assert len(reg) == 0
        reg.register("t1", lambda: None)
        assert len(reg) == 1
        reg.register("t2", lambda: None)
        assert len(reg) == 2

    def test_repr(self) -> None:
        """__repr__ includes registered names."""
        reg = ToolRegistry()
        reg.register("my_tool", lambda: None)
        assert "my_tool" in repr(reg)


class TestToolRegistryDefaultAnnotation:
    """Tests verifying default capability annotation is applied."""

    def test_wrapped_tool_returns_camelvalue(self) -> None:
        """Wrapped callable returns CaMeLValue."""
        reg = ToolRegistry()
        reg.register("t", lambda: "result")
        tools = reg.as_interpreter_tools()
        result = tools["t"]()
        assert isinstance(result, CaMeLValue)

    def test_default_annotation_sources(self) -> None:
        """Default annotation sets sources={tool_id}."""
        reg = ToolRegistry()
        reg.register("my_tool", lambda: 42)
        tools = reg.as_interpreter_tools()
        cv = tools["my_tool"]()
        assert cv.sources == frozenset({"my_tool"})

    def test_default_annotation_readers_public(self) -> None:
        """Default annotation sets readers=Public."""
        reg = ToolRegistry()
        reg.register("my_tool", lambda: 42)
        tools = reg.as_interpreter_tools()
        cv = tools["my_tool"]()
        assert cv.readers is Public

    def test_default_annotation_preserves_value(self) -> None:
        """Default annotation preserves the raw return value."""
        reg = ToolRegistry()
        reg.register("t", lambda: {"key": "val"})
        tools = reg.as_interpreter_tools()
        cv = tools["t"]()
        assert cv.value == {"key": "val"}

    def test_default_annotation_args_passed_through(self) -> None:
        """Tool is called with the provided arguments."""
        received: dict[str, Any] = {}

        def my_tool(x: int, y: str) -> str:
            received["x"] = x
            received["y"] = y
            return f"{x}-{y}"

        reg = ToolRegistry()
        reg.register("my_tool", my_tool)
        tools = reg.as_interpreter_tools()
        cv = tools["my_tool"](10, "hello")
        assert received == {"x": 10, "y": "hello"}
        assert cv.value == "10-hello"


class TestToolRegistryCustomAnnotation:
    """Tests verifying custom capability annotators are called."""

    def _make_annotator(
        self, readers: Any
    ) -> CapabilityAnnotationFn:
        """Return an annotator that sets specific readers."""

        def _ann(return_value: Any, tool_kwargs: Mapping[str, Any]) -> CaMeLValue:
            return CaMeLValue(
                value=return_value,
                sources=frozenset({"custom_source"}),
                inner_source="custom_field",
                readers=readers,
            )

        return _ann

    def test_custom_annotator_called(self) -> None:
        """Custom annotator is invoked and its result is returned."""
        reg = ToolRegistry()
        ann = self._make_annotator(frozenset({"alice@x.com"}))
        reg.register("t", lambda: "result", capability_annotation=ann)
        tools = reg.as_interpreter_tools()
        cv = tools["t"]()
        assert cv.sources == frozenset({"custom_source"})
        assert cv.inner_source == "custom_field"
        assert cv.readers == frozenset({"alice@x.com"})

    def test_custom_annotator_receives_tool_kwargs(self) -> None:
        """Custom annotator receives the tool kwargs mapping."""
        received_kwargs: dict[str, Any] = {}

        def ann(return_value: Any, tool_kwargs: Mapping[str, Any]) -> CaMeLValue:
            received_kwargs.update(tool_kwargs)
            return CaMeLValue(
                value=return_value,
                sources=frozenset({"t"}),
                inner_source=None,
                readers=Public,
            )

        def tool(doc_id: str, owner: str) -> dict[str, str]:
            return {"content": "data"}

        reg = ToolRegistry()
        reg.register("t", tool, capability_annotation=ann)
        tools = reg.as_interpreter_tools()
        tools["t"]("doc-123", owner="alice@x.com")
        assert received_kwargs.get("doc_id") == "doc-123"
        assert received_kwargs.get("owner") == "alice@x.com"

    def test_tool_returning_camelvalue_skips_annotation(self) -> None:
        """If the tool already returns CaMeLValue, annotation is skipped."""
        expected = wrap("pre_annotated", sources=frozenset({"pre"}))

        def tool() -> CaMeLValue:
            return expected

        called: list[bool] = []

        def ann(rv: Any, kw: Mapping[str, Any]) -> CaMeLValue:
            called.append(True)
            return CaMeLValue(
                value=rv, sources=frozenset({"ann"}), inner_source=None, readers=Public
            )

        reg = ToolRegistry()
        reg.register("t", tool, capability_annotation=ann)
        tools = reg.as_interpreter_tools()
        result = tools["t"]()
        # Annotation must not have been called
        assert called == []
        # Pre-annotated value is returned as-is
        assert result is expected


class TestToolRegistryPublicReadersMerge:
    """Public readers preserved through merge after annotation."""

    def test_public_preserved_after_default_annotation(self) -> None:
        """Default annotation → Public; merge with frozenset stays Public."""
        reg = ToolRegistry()
        reg.register("t", lambda: "v")
        tools = reg.as_interpreter_tools()
        cv_tool = tools["t"]()
        assert cv_tool.readers is Public

        other = _make_cv("x", frozenset({"u"}), readers=frozenset({"alice@x.com"}))
        merged = cv_tool.merge(other)
        assert merged.readers is Public

    def test_public_preserved_after_custom_annotation(self) -> None:
        """Custom annotation returning Public; merge keeps Public."""

        def ann(rv: Any, kw: Mapping[str, Any]) -> CaMeLValue:
            return CaMeLValue(value=rv, sources=frozenset({"t"}), inner_source=None, readers=Public)

        reg = ToolRegistry()
        reg.register("t", lambda: "v", capability_annotation=ann)
        tools = reg.as_interpreter_tools()
        cv = tools["t"]()
        restricted = _make_cv("x", frozenset(), readers=frozenset({"bob@x.com"}))
        assert cv.merge(restricted).readers is Public


# ---------------------------------------------------------------------------
# Interpreter integration
# ---------------------------------------------------------------------------


class TestInterpreterIntegration:
    """End-to-end tests verifying ToolRegistry integrates with CaMeLInterpreter."""

    def test_interpreter_receives_camelvalue_from_registry(self) -> None:
        """Interpreter stores CaMeLValue with correct sources after tool call."""
        reg = ToolRegistry()
        reg.register("get_value", lambda: 100)
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec("result = get_value()")
        cv = interp.get("result")
        assert isinstance(cv, CaMeLValue)
        assert cv.sources == frozenset({"get_value"})
        assert cv.value == 100

    def test_interpreter_uses_custom_annotator(self) -> None:
        """Interpreter receives CaMeLValue from custom annotator."""

        def ann(rv: Any, kw: Mapping[str, Any]) -> CaMeLValue:
            return CaMeLValue(
                value=rv,
                sources=frozenset({"custom_read_email"}),
                inner_source="subject",
                readers=frozenset({"alice@x.com"}),
            )

        reg = ToolRegistry()
        reg.register("read_email", lambda: {"subject": "Hi"}, capability_annotation=ann)
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec("email = read_email()")
        cv = interp.get("email")
        assert cv.sources == frozenset({"custom_read_email"})
        assert cv.inner_source == "subject"
        assert cv.readers == frozenset({"alice@x.com"})

    def test_dependency_graph_stores_camelvalue(self) -> None:
        """The data flow graph tracks CaMeLValue provenance correctly."""
        reg = ToolRegistry()
        reg.register("fetch", lambda: "data")
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec("x = fetch()")
        interp.exec("y = x")
        cv_y = interp.get("y")
        assert "fetch" in cv_y.sources

    def test_multiple_tools_registered(self) -> None:
        """Multiple tools can be registered and called from the same interpreter."""
        reg = ToolRegistry()
        reg.register("tool_a", lambda: "a_result")
        reg.register("tool_b", lambda: "b_result")
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec("a = tool_a()")
        interp.exec("b = tool_b()")
        assert interp.get("a").sources == frozenset({"tool_a"})
        assert interp.get("b").sources == frozenset({"tool_b"})

    def test_tool_with_args(self) -> None:
        """Tool called with arguments inside interpreter code."""
        def add_tool(x: int, y: int) -> int:
            return x + y

        reg = ToolRegistry()
        reg.register("add", add_tool)
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec("result = add(3, 4)")
        cv = interp.get("result")
        assert cv.value == 7
        assert cv.sources == frozenset({"add"})

    def test_tool_result_public_readers_in_interpreter(self) -> None:
        """Tool result with Public readers is accessible in interpreter store."""
        reg = ToolRegistry()
        reg.register("open_tool", lambda: "public_data")
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec("v = open_tool()")
        cv = interp.get("v")
        assert cv.readers is Public


# ---------------------------------------------------------------------------
# annotate_read_email
# ---------------------------------------------------------------------------


class TestAnnotateReadEmail:
    """Tests for annotate_read_email()."""

    _RAW: dict[str, Any] = {
        "sender": "alice@example.com",
        "subject": "Meeting notes",
        "body": "Let's meet at 10am.",
        "recipients": ["bob@example.com"],
    }

    def _cv(self, raw: dict[str, Any] | None = None) -> CaMeLValue:
        return annotate_read_email(raw if raw is not None else dict(self._RAW), {})

    # -- top-level CaMeLValue --------------------------------------------------

    def test_sources_is_read_email(self) -> None:
        """sources = frozenset({"read_email"})."""
        cv = self._cv()
        assert cv.sources == frozenset({"read_email"})

    def test_inner_source_is_sender_email(self) -> None:
        """inner_source equals the sender's email address."""
        cv = self._cv()
        assert cv.inner_source == "alice@example.com"

    def test_readers_is_public(self) -> None:
        """readers = Public."""
        cv = self._cv()
        assert cv.readers is Public

    def test_returns_camelvalue(self) -> None:
        """Return type is CaMeLValue."""
        cv = self._cv()
        assert isinstance(cv, CaMeLValue)

    # -- nested field wrapping -------------------------------------------------

    def test_subject_wrapped_as_camelvalue(self) -> None:
        """subject field is wrapped as CaMeLValue."""
        cv = self._cv()
        subject_cv = cv.value["subject"]
        assert isinstance(subject_cv, CaMeLValue)

    def test_body_wrapped_as_camelvalue(self) -> None:
        """body field is wrapped as CaMeLValue."""
        cv = self._cv()
        body_cv = cv.value["body"]
        assert isinstance(body_cv, CaMeLValue)

    def test_sender_wrapped_as_camelvalue(self) -> None:
        """sender field is wrapped as CaMeLValue."""
        cv = self._cv()
        sender_cv = cv.value["sender"]
        assert isinstance(sender_cv, CaMeLValue)

    def test_subject_inner_source(self) -> None:
        """subject CaMeLValue has inner_source='subject'."""
        cv = self._cv()
        assert cv.value["subject"].inner_source == "subject"

    def test_body_inner_source(self) -> None:
        """body CaMeLValue has inner_source='body'."""
        cv = self._cv()
        assert cv.value["body"].inner_source == "body"

    def test_sender_inner_source(self) -> None:
        """sender CaMeLValue has inner_source='sender'."""
        cv = self._cv()
        assert cv.value["sender"].inner_source == "sender"

    def test_subject_sources(self) -> None:
        """subject CaMeLValue has sources={"read_email"}."""
        cv = self._cv()
        assert cv.value["subject"].sources == frozenset({"read_email"})

    def test_body_sources(self) -> None:
        """body CaMeLValue has sources={"read_email"}."""
        cv = self._cv()
        assert cv.value["body"].sources == frozenset({"read_email"})

    def test_subject_value_preserved(self) -> None:
        """subject CaMeLValue.value holds the original subject string."""
        cv = self._cv()
        assert cv.value["subject"].value == "Meeting notes"

    def test_body_value_preserved(self) -> None:
        """body CaMeLValue.value holds the original body string."""
        cv = self._cv()
        assert cv.value["body"].value == "Let's meet at 10am."

    def test_sender_value_preserved(self) -> None:
        """sender CaMeLValue.value holds the sender address."""
        cv = self._cv()
        assert cv.value["sender"].value == "alice@example.com"

    def test_extra_fields_not_wrapped(self) -> None:
        """Fields other than sender/subject/body are not wrapped."""
        cv = self._cv()
        assert not isinstance(cv.value["recipients"], CaMeLValue)

    # -- edge cases ------------------------------------------------------------

    def test_missing_sender_inner_source_is_none(self) -> None:
        """inner_source=None when sender key is absent."""
        raw: dict[str, Any] = {"subject": "Hi", "body": "Hello"}
        cv = annotate_read_email(raw, {})
        assert cv.inner_source is None

    def test_missing_body_not_wrapped(self) -> None:
        """No KeyError when body key is absent."""
        raw: dict[str, Any] = {"sender": "bob@example.com", "subject": "Hi"}
        cv = annotate_read_email(raw, {})
        assert isinstance(cv, CaMeLValue)
        assert "body" not in cv.value

    def test_non_dict_return_wrapped_without_field_annotation(self) -> None:
        """Non-dict return value is wrapped at top level without field tagging."""
        cv = annotate_read_email("raw string email", {})
        assert cv.sources == frozenset({"read_email"})
        assert cv.value == "raw string email"
        assert cv.inner_source is None

    def test_tool_kwargs_not_used(self) -> None:
        """tool_kwargs are accepted but do not affect the annotation."""
        cv1 = annotate_read_email(dict(self._RAW), {"email_id": 42})
        cv2 = annotate_read_email(dict(self._RAW), {})
        assert cv1.sources == cv2.sources
        assert cv1.inner_source == cv2.inner_source


# ---------------------------------------------------------------------------
# annotate_read_document / annotate_get_file  (cloud storage)
# ---------------------------------------------------------------------------


class TestAnnotateCloudStorage:
    """Tests for annotate_read_document() and annotate_get_file()."""

    def _doc(self, permissions: Any) -> dict[str, Any]:
        return {"content": "Document text", "permissions": permissions}

    # -- annotate_read_document -----------------------------------------------

    def test_read_document_sources(self) -> None:
        """sources = frozenset({"read_document"})."""
        cv = annotate_read_document(self._doc({"type": "public"}), {})
        assert cv.sources == frozenset({"read_document"})

    def test_read_document_public_permissions(self) -> None:
        """readers=Public for public document permissions."""
        cv = annotate_read_document(self._doc({"type": "public"}), {})
        assert cv.readers is Public

    def test_read_document_restricted_permissions(self) -> None:
        """readers=frozenset for restricted document permissions."""
        perms = {"type": "restricted", "readers": ["alice@x.com", "bob@x.com"]}
        cv = annotate_read_document(self._doc(perms), {})
        assert cv.readers == frozenset({"alice@x.com", "bob@x.com"})

    def test_read_document_single_reader(self) -> None:
        """Single reader in permissions list."""
        perms = {"type": "restricted", "readers": ["carol@x.com"]}
        cv = annotate_read_document(self._doc(perms), {})
        assert cv.readers == frozenset({"carol@x.com"})

    def test_read_document_empty_readers_list(self) -> None:
        """Empty readers list → frozenset() (no readers)."""
        perms = {"type": "restricted", "readers": []}
        cv = annotate_read_document(self._doc(perms), {})
        assert cv.readers == frozenset()

    def test_read_document_missing_permissions_key(self) -> None:
        """Missing permissions key → Public (safe fallback)."""
        cv = annotate_read_document({"content": "text"}, {})
        assert cv.readers is Public

    def test_read_document_none_permissions(self) -> None:
        """permissions=None → Public (safe fallback)."""
        cv = annotate_read_document(self._doc(None), {})
        assert cv.readers is Public

    def test_read_document_value_preserved(self) -> None:
        """Raw return value is stored unchanged."""
        raw = self._doc({"type": "public"})
        cv = annotate_read_document(raw, {})
        assert cv.value is raw

    def test_read_document_inner_source_none(self) -> None:
        """inner_source=None for cloud storage annotators."""
        cv = annotate_read_document(self._doc({"type": "public"}), {})
        assert cv.inner_source is None

    # -- annotate_get_file ----------------------------------------------------

    def test_get_file_sources(self) -> None:
        """sources = frozenset({"get_file"})."""
        cv = annotate_get_file(self._doc({"type": "public"}), {})
        assert cv.sources == frozenset({"get_file"})

    def test_get_file_public_permissions(self) -> None:
        """readers=Public for publicly shared file."""
        cv = annotate_get_file(self._doc({"type": "public"}), {})
        assert cv.readers is Public

    def test_get_file_restricted_permissions(self) -> None:
        """readers=frozenset for restricted file."""
        perms = {"type": "restricted", "readers": ["dave@x.com"]}
        cv = annotate_get_file(self._doc(perms), {})
        assert cv.readers == frozenset({"dave@x.com"})

    def test_get_file_missing_permissions_fallback(self) -> None:
        """Missing permissions → Public fallback."""
        cv = annotate_get_file({"content": "binary data"}, {})
        assert cv.readers is Public

    # -- unrecognised permission type -----------------------------------------

    def test_unrecognised_permission_type_defaults_to_restricted(self) -> None:
        """An unrecognised 'type' value falls back to restricted with readers list."""
        perms = {"type": "unknown_type", "readers": ["eve@x.com"]}
        cv = annotate_read_document(self._doc(perms), {})
        # "unknown_type" is treated as restricted; readers list is extracted.
        assert cv.readers == frozenset({"eve@x.com"})


# ---------------------------------------------------------------------------
# register_built_in_tools
# ---------------------------------------------------------------------------


class TestRegisterBuiltInTools:
    """Tests for register_built_in_tools()."""

    def _make_email_fn(self) -> Any:
        def read_email() -> dict[str, Any]:
            return {
                "sender": "sender@example.com",
                "subject": "Test",
                "body": "Body text",
            }

        return read_email

    def _make_doc_fn(self, readers: list[str] | None = None) -> Any:
        _readers = readers or []

        def read_document(doc_id: str) -> dict[str, Any]:
            if _readers:
                return {
                    "content": "doc",
                    "permissions": {"type": "restricted", "readers": _readers},
                }
            return {"content": "doc", "permissions": {"type": "public"}}

        return read_document

    def test_read_email_registered_with_annotator(self) -> None:
        """read_email is registered and uses annotate_read_email."""
        reg = ToolRegistry()
        register_built_in_tools(reg, read_email_fn=self._make_email_fn())
        assert "read_email" in reg
        tools = reg.as_interpreter_tools()
        cv = tools["read_email"]()
        assert cv.inner_source == "sender@example.com"
        assert cv.sources == frozenset({"read_email"})

    def test_read_document_registered_with_annotator(self) -> None:
        """read_document is registered and uses annotate_read_document."""
        reg = ToolRegistry()
        register_built_in_tools(
            reg, read_document_fn=self._make_doc_fn(["alice@x.com"])
        )
        assert "read_document" in reg
        tools = reg.as_interpreter_tools()
        cv = tools["read_document"]("doc-1")
        assert cv.sources == frozenset({"read_document"})
        assert cv.readers == frozenset({"alice@x.com"})

    def test_get_file_registered_with_annotator(self) -> None:
        """get_file is registered and uses annotate_get_file."""
        reg = ToolRegistry()

        def get_file(file_id: str) -> dict[str, Any]:
            return {"content": "data", "permissions": {"type": "public"}}

        register_built_in_tools(reg, get_file_fn=get_file)
        assert "get_file" in reg
        tools = reg.as_interpreter_tools()
        cv = tools["get_file"]("f-1")
        assert cv.sources == frozenset({"get_file"})
        assert cv.readers is Public

    def test_omitted_tools_not_registered(self) -> None:
        """Tools omitted from the call are not added to the registry."""
        reg = ToolRegistry()
        register_built_in_tools(reg, read_email_fn=self._make_email_fn())
        assert "read_document" not in reg
        assert "get_file" not in reg

    def test_all_tools_registered_together(self) -> None:
        """All three tools can be registered in a single call."""
        reg = ToolRegistry()
        register_built_in_tools(
            reg,
            read_email_fn=self._make_email_fn(),
            read_document_fn=self._make_doc_fn(),
            get_file_fn=lambda fid: {"content": "x", "permissions": {"type": "public"}},
        )
        assert reg.names == frozenset({"read_email", "read_document", "get_file"})

    def test_read_email_in_interpreter(self) -> None:
        """read_email annotation is surfaced correctly through the interpreter."""
        reg = ToolRegistry()
        register_built_in_tools(reg, read_email_fn=self._make_email_fn())
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec("email = read_email()")
        cv = interp.get("email")
        assert cv.inner_source == "sender@example.com"
        assert cv.sources == frozenset({"read_email"})

    def test_read_document_readers_in_interpreter(self) -> None:
        """Cloud storage readers are tracked correctly through the interpreter."""
        reg = ToolRegistry()
        register_built_in_tools(
            reg, read_document_fn=self._make_doc_fn(["alice@x.com"])
        )
        interp = CaMeLInterpreter(tools=reg.as_interpreter_tools())
        interp.exec('doc = read_document("doc-1")')
        cv = interp.get("doc")
        assert cv.readers == frozenset({"alice@x.com"})
