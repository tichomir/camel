"""Unit tests for camel/value.py — CaMeLValue, Public, and propagation functions."""

from __future__ import annotations

import pickle

import pytest

from camel.value import (
    CaMeLValue,
    Public,
    _PublicType,
    propagate_assignment,
    propagate_binary_op,
    propagate_dict_construction,
    propagate_list_construction,
    propagate_subscript,
    raw_value,
    wrap,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SOURCES_A = frozenset({"tool_a"})
SOURCES_B = frozenset({"tool_b"})
READERS_A = frozenset({"alice@example.com"})
READERS_B = frozenset({"bob@example.com"})


def make_cv(
    value: object = 0,
    sources: frozenset[str] = frozenset(),
    inner_source: str | None = None,
    readers: frozenset[str] | _PublicType = Public,
) -> CaMeLValue:
    return CaMeLValue(
        value=value,
        sources=sources,
        inner_source=inner_source,
        readers=readers,
    )


# ---------------------------------------------------------------------------
# _PublicType / Public singleton
# ---------------------------------------------------------------------------


class TestPublicSingleton:
    def test_singleton_identity(self) -> None:
        assert _PublicType() is Public

    def test_multiple_constructions_same_object(self) -> None:
        a = _PublicType()
        b = _PublicType()
        assert a is b

    def test_isinstance_check(self) -> None:
        assert isinstance(Public, _PublicType)

    def test_repr(self) -> None:
        assert repr(Public) == "Public"

    def test_pickle_preserves_singleton(self) -> None:
        restored = pickle.loads(pickle.dumps(Public))
        assert restored is Public

    def test_public_is_not_frozenset(self) -> None:
        assert not isinstance(Public, frozenset)

    def test_empty_frozenset_is_not_public(self) -> None:
        assert not isinstance(frozenset(), _PublicType)


# ---------------------------------------------------------------------------
# CaMeLValue construction and field access
# ---------------------------------------------------------------------------


class TestCaMeLValueConstruction:
    def test_basic_construction(self) -> None:
        cv = CaMeLValue(
            value=42,
            sources=frozenset({"src"}),
            inner_source="field",
            readers=READERS_A,
        )
        assert cv.value == 42
        assert cv.sources == frozenset({"src"})
        assert cv.inner_source == "field"
        assert cv.readers == READERS_A

    def test_construction_with_public_readers(self) -> None:
        cv = make_cv(value="hello", readers=Public)
        assert cv.readers is Public

    def test_construction_with_empty_readers(self) -> None:
        cv = make_cv(readers=frozenset())
        assert cv.readers == frozenset()

    def test_frozen_value_immutable(self) -> None:
        cv = make_cv(value=1)
        with pytest.raises((AttributeError, TypeError)):
            cv.value = 2  # type: ignore[misc]

    def test_frozen_sources_immutable(self) -> None:
        cv = make_cv(sources=frozenset({"a"}))
        with pytest.raises((AttributeError, TypeError)):
            cv.sources = frozenset({"b"})  # type: ignore[misc]

    def test_inner_source_none_by_default(self) -> None:
        cv = make_cv()
        assert cv.inner_source is None

    def test_value_can_be_any_type(self) -> None:
        for v in [None, 0, 3.14, "str", [1, 2], {"k": "v"}, object()]:
            cv = make_cv(value=v)
            assert cv.value is v


# ---------------------------------------------------------------------------
# raw / raw_value accessors
# ---------------------------------------------------------------------------


class TestRawAccessors:
    def test_raw_property(self) -> None:
        cv = make_cv(value=99)
        assert cv.raw == 99

    def test_raw_value_function(self) -> None:
        cv = make_cv(value="hello")
        assert raw_value(cv) == "hello"

    def test_raw_and_raw_value_equivalent(self) -> None:
        cv = make_cv(value=[1, 2, 3])
        assert cv.raw == raw_value(cv)

    def test_raw_none_value(self) -> None:
        cv = make_cv(value=None)
        assert cv.raw is None

    def test_raw_value_usable_with_map(self) -> None:
        cvs = [make_cv(value=i) for i in range(3)]
        assert list(map(raw_value, cvs)) == [0, 1, 2]


# ---------------------------------------------------------------------------
# wrap() convenience constructor
# ---------------------------------------------------------------------------


class TestWrap:
    def test_defaults_to_public_readers(self) -> None:
        cv = wrap("x")
        assert cv.readers is Public

    def test_defaults_to_empty_sources(self) -> None:
        cv = wrap("x")
        assert cv.sources == frozenset()

    def test_defaults_to_none_inner_source(self) -> None:
        cv = wrap("x")
        assert cv.inner_source is None

    def test_explicit_sources(self) -> None:
        cv = wrap("x", sources=frozenset({"tool"}))
        assert cv.sources == frozenset({"tool"})

    def test_explicit_readers(self) -> None:
        cv = wrap("x", readers=READERS_A)
        assert cv.readers == READERS_A

    def test_explicit_inner_source(self) -> None:
        cv = wrap("x", inner_source="subject")
        assert cv.inner_source == "subject"

    def test_value_stored_correctly(self) -> None:
        obj = object()
        cv = wrap(obj)
        assert cv.value is obj

    def test_explicit_frozenset_readers(self) -> None:
        readers = frozenset({"a@x.com", "b@x.com"})
        cv = wrap("secret", readers=readers)
        assert cv.readers == readers

    def test_explicit_public_readers(self) -> None:
        cv = wrap("open", readers=Public)
        assert cv.readers is Public


# ---------------------------------------------------------------------------
# propagate_assignment
# ---------------------------------------------------------------------------


class TestPropagateAssignment:
    def test_copies_sources(self) -> None:
        src = make_cv(sources=SOURCES_A)
        result = propagate_assignment(src, src.raw)
        assert result.sources == SOURCES_A

    def test_copies_readers_frozenset(self) -> None:
        src = make_cv(readers=READERS_A)
        result = propagate_assignment(src, src.raw)
        assert result.readers == READERS_A

    def test_copies_readers_public(self) -> None:
        src = make_cv(readers=Public)
        result = propagate_assignment(src, src.raw)
        assert result.readers is Public

    def test_clears_inner_source(self) -> None:
        src = make_cv(inner_source="subject")
        result = propagate_assignment(src, src.raw)
        assert result.inner_source is None

    def test_stores_new_value(self) -> None:
        src = make_cv(value=1)
        result = propagate_assignment(src, 99)
        assert result.raw == 99

    def test_returns_new_instance(self) -> None:
        src = make_cv(value=1)
        result = propagate_assignment(src, 1)
        assert result is not src


# ---------------------------------------------------------------------------
# propagate_binary_op
# ---------------------------------------------------------------------------


class TestPropagateBinaryOp:
    def test_unions_sources(self) -> None:
        left = make_cv(sources=SOURCES_A)
        right = make_cv(sources=SOURCES_B)
        result = propagate_binary_op(left, right, left.raw + right.raw)
        assert result.sources == SOURCES_A | SOURCES_B

    def test_unions_readers_frozensets(self) -> None:
        left = make_cv(readers=READERS_A)
        right = make_cv(readers=READERS_B)
        result = propagate_binary_op(left, right, 0)
        assert result.readers == READERS_A | READERS_B

    def test_public_left_absorbs(self) -> None:
        left = make_cv(readers=Public)
        right = make_cv(readers=READERS_B)
        result = propagate_binary_op(left, right, 0)
        assert result.readers is Public

    def test_public_right_absorbs(self) -> None:
        left = make_cv(readers=READERS_A)
        right = make_cv(readers=Public)
        result = propagate_binary_op(left, right, 0)
        assert result.readers is Public

    def test_both_public_stays_public(self) -> None:
        left = make_cv(readers=Public)
        right = make_cv(readers=Public)
        result = propagate_binary_op(left, right, 0)
        assert result.readers is Public

    def test_clears_inner_source(self) -> None:
        left = make_cv(inner_source="field_a")
        right = make_cv(inner_source="field_b")
        result = propagate_binary_op(left, right, 0)
        assert result.inner_source is None

    def test_stores_result_value(self) -> None:
        left = make_cv(value=2)
        right = make_cv(value=3)
        result = propagate_binary_op(left, right, 5)
        assert result.raw == 5

    def test_empty_sources_union(self) -> None:
        left = make_cv(sources=frozenset())
        right = make_cv(sources=SOURCES_B)
        result = propagate_binary_op(left, right, 0)
        assert result.sources == SOURCES_B

    def test_both_empty_sources(self) -> None:
        left = make_cv(sources=frozenset())
        right = make_cv(sources=frozenset())
        result = propagate_binary_op(left, right, 0)
        assert result.sources == frozenset()


# ---------------------------------------------------------------------------
# propagate_list_construction
# ---------------------------------------------------------------------------


class TestPropagateListConstruction:
    def test_unions_all_sources(self) -> None:
        a = make_cv(sources=frozenset({"a"}))
        b = make_cv(sources=frozenset({"b"}))
        c = make_cv(sources=frozenset({"c"}))
        result = propagate_list_construction([a, b, c], [a.raw, b.raw, c.raw])
        assert result.sources == frozenset({"a", "b", "c"})

    def test_unions_all_readers(self) -> None:
        a = make_cv(readers=frozenset({"x@x.com"}))
        b = make_cv(readers=frozenset({"y@y.com"}))
        result = propagate_list_construction([a, b], [a.raw, b.raw])
        assert result.readers == frozenset({"x@x.com", "y@y.com"})

    def test_public_reader_absorbs(self) -> None:
        a = make_cv(readers=Public)
        b = make_cv(readers=READERS_B)
        result = propagate_list_construction([a, b], [a.raw, b.raw])
        assert result.readers is Public

    def test_empty_list_produces_empty_caps(self) -> None:
        result = propagate_list_construction([], [])
        assert result.sources == frozenset()
        assert result.readers == frozenset()
        assert result.inner_source is None

    def test_clears_inner_source(self) -> None:
        a = make_cv(inner_source="field")
        result = propagate_list_construction([a], [a.raw])
        assert result.inner_source is None

    def test_stores_result_value(self) -> None:
        a = make_cv(value=1)
        b = make_cv(value=2)
        lst = [1, 2]
        result = propagate_list_construction([a, b], lst)
        assert result.raw == lst

    def test_single_element(self) -> None:
        a = make_cv(value="x", sources=SOURCES_A, readers=READERS_A)
        result = propagate_list_construction([a], ["x"])
        assert result.sources == SOURCES_A
        assert result.readers == READERS_A


# ---------------------------------------------------------------------------
# propagate_dict_construction
# ---------------------------------------------------------------------------


class TestPropagateDictConstruction:
    def test_unions_key_and_value_sources(self) -> None:
        k = make_cv(sources=frozenset({"key_src"}))
        v = make_cv(sources=frozenset({"val_src"}))
        result = propagate_dict_construction([k], [v], {k.raw: v.raw})
        assert result.sources == frozenset({"key_src", "val_src"})

    def test_unions_key_and_value_readers(self) -> None:
        k = make_cv(readers=frozenset({"alice@example.com"}))
        v = make_cv(readers=frozenset({"bob@example.com"}))
        result = propagate_dict_construction([k], [v], {})
        assert result.readers == frozenset({"alice@example.com", "bob@example.com"})

    def test_public_key_reader_absorbs(self) -> None:
        k = make_cv(readers=Public)
        v = make_cv(readers=READERS_B)
        result = propagate_dict_construction([k], [v], {})
        assert result.readers is Public

    def test_public_value_reader_absorbs(self) -> None:
        k = make_cv(readers=READERS_A)
        v = make_cv(readers=Public)
        result = propagate_dict_construction([k], [v], {})
        assert result.readers is Public

    def test_empty_dict_produces_empty_caps(self) -> None:
        result = propagate_dict_construction([], [], {})
        assert result.sources == frozenset()
        assert result.readers == frozenset()

    def test_clears_inner_source(self) -> None:
        k = make_cv(inner_source="k_field")
        v = make_cv(inner_source="v_field")
        result = propagate_dict_construction([k], [v], {})
        assert result.inner_source is None

    def test_mismatched_lengths_raises(self) -> None:
        k = make_cv()
        with pytest.raises(ValueError, match="same length"):
            propagate_dict_construction([k], [], {})

    def test_multiple_pairs(self) -> None:
        k1 = make_cv(sources=frozenset({"k1"}))
        k2 = make_cv(sources=frozenset({"k2"}))
        v1 = make_cv(sources=frozenset({"v1"}))
        v2 = make_cv(sources=frozenset({"v2"}))
        result = propagate_dict_construction([k1, k2], [v1, v2], {})
        assert result.sources == frozenset({"k1", "k2", "v1", "v2"})

    def test_stores_result_value(self) -> None:
        k = make_cv(value="key")
        v = make_cv(value="val")
        d = {"key": "val"}
        result = propagate_dict_construction([k], [v], d)
        assert result.raw == d


# ---------------------------------------------------------------------------
# propagate_subscript
# ---------------------------------------------------------------------------


class TestPropagateSubscript:
    def test_unions_container_and_key_sources(self) -> None:
        container = make_cv(sources=frozenset({"container_src"}))
        key = make_cv(sources=frozenset({"key_src"}))
        result = propagate_subscript(container, key, "extracted")
        assert result.sources == frozenset({"container_src", "key_src"})

    def test_unions_container_and_key_readers(self) -> None:
        container = make_cv(readers=READERS_A)
        key = make_cv(readers=READERS_B)
        result = propagate_subscript(container, key, None)
        assert result.readers == READERS_A | READERS_B

    def test_public_container_absorbs(self) -> None:
        container = make_cv(readers=Public)
        key = make_cv(readers=READERS_B)
        result = propagate_subscript(container, key, None)
        assert result.readers is Public

    def test_public_key_absorbs(self) -> None:
        container = make_cv(readers=READERS_A)
        key = make_cv(readers=Public)
        result = propagate_subscript(container, key, None)
        assert result.readers is Public

    def test_clears_inner_source(self) -> None:
        container = make_cv(inner_source="list_field")
        key = make_cv(inner_source="index_field")
        result = propagate_subscript(container, key, None)
        assert result.inner_source is None

    def test_stores_result_value(self) -> None:
        container = make_cv(value=[10, 20, 30])
        key = make_cv(value=1)
        result = propagate_subscript(container, key, container.raw[key.raw])
        assert result.raw == 20

    def test_container_sources_subset_of_result(self) -> None:
        container = make_cv(sources=SOURCES_A)
        key = make_cv(sources=frozenset())
        result = propagate_subscript(container, key, None)
        assert SOURCES_A <= result.sources

    def test_key_sources_subset_of_result(self) -> None:
        container = make_cv(sources=frozenset())
        key = make_cv(sources=SOURCES_B)
        result = propagate_subscript(container, key, None)
        assert SOURCES_B <= result.sources


# ---------------------------------------------------------------------------
# Public absorption — cross-function consistency
# ---------------------------------------------------------------------------


class TestPublicAbsorption:
    """Verify Public is absorbing under union across all propagation paths."""

    def test_binary_op_any_public_wins(self) -> None:
        public_cv = make_cv(readers=Public)
        restricted = make_cv(readers=frozenset({"x@x.com"}))
        assert propagate_binary_op(public_cv, restricted, 0).readers is Public
        assert propagate_binary_op(restricted, public_cv, 0).readers is Public

    def test_list_any_public_wins(self) -> None:
        elements = [
            make_cv(readers=frozenset({"a@a.com"})),
            make_cv(readers=Public),
            make_cv(readers=frozenset({"b@b.com"})),
        ]
        result = propagate_list_construction(elements, [])
        assert result.readers is Public

    def test_dict_key_public_wins(self) -> None:
        k = make_cv(readers=Public)
        v = make_cv(readers=frozenset({"c@c.com"}))
        assert propagate_dict_construction([k], [v], {}).readers is Public

    def test_subscript_key_public_wins(self) -> None:
        c = make_cv(readers=frozenset({"d@d.com"}))
        k = make_cv(readers=Public)
        assert propagate_subscript(c, k, None).readers is Public


# ---------------------------------------------------------------------------
# Import surface — camel package re-exports
# ---------------------------------------------------------------------------


class TestPackageExports:
    def test_import_from_camel(self) -> None:
        import camel

        assert hasattr(camel, "CaMeLValue")
        assert hasattr(camel, "Public")
        assert hasattr(camel, "wrap")
        assert hasattr(camel, "raw_value")
        assert hasattr(camel, "propagate_assignment")
        assert hasattr(camel, "propagate_binary_op")
        assert hasattr(camel, "propagate_list_construction")
        assert hasattr(camel, "propagate_dict_construction")
        assert hasattr(camel, "propagate_subscript")

    def test_public_from_camel_is_same_singleton(self) -> None:
        from camel import Public as PackagePublic
        from camel.value import Public as ValuePublic

        assert PackagePublic is ValuePublic
