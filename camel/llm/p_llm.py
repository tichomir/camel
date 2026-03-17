"""P-LLM wrapper — execution plan generation with isolation contract.

The :class:`PLLMWrapper` is the primary entry-point for the Privileged LLM
(P-LLM) planning path.  It:

1. Builds a structured system prompt with five required sections (CaMeL Python
   subset spec, tool signatures, user context, opaque-variable instruction
   M2-F13, and ``print()`` usage guidance M2-F10).
2. Sends the user query as the human turn via
   :class:`~camel.llm.backend.LLMBackend`.
3. Parses the Markdown-fenced ``python`` code block from the response into a
   :class:`CodePlan`.
4. Retries up to ``max_retries`` times on :class:`CodeBlockNotFoundError` or
   ``SyntaxError``; each retry appends the previous assistant response and a
   **redacted** error-feedback message (type + line number only — never the
   error message text, which may echo back attacker-controlled content).
5. Enforces the core isolation invariant:
   **tool return values (CaMeLValue instances) must never reach the P-LLM**.

Isolation guarantee (three levels)
-----------------------------------
* **Architectural** — :meth:`PLLMWrapper.generate_plan` accepts only ``str``
  parameters for content sent to the model.  :class:`~camel.value.CaMeLValue`
  instances (interpreter-managed, capability-tagged values) have no path into
  any parameter.
* **Type-system** — ``user_query`` and each ``user_context`` value are typed
  as ``str``; passing a :class:`~camel.value.CaMeLValue` is a static type
  error in mypy strict mode.
* **Runtime** — :meth:`PLLMWrapper._guard_no_camel_values` raises
  :class:`PLLMIsolationError` if a caller bypasses the type checker (e.g. via
  ``# type: ignore``).

Usage example
-------------
.. code-block:: python

    import asyncio
    from camel.llm.backend import get_backend
    from camel.llm.p_llm import PLLMWrapper, ToolSignature

    backend = get_backend("claude", api_key="sk-...")
    wrapper = PLLMWrapper(backend)

    tools = [
        ToolSignature(
            name="get_email",
            signature="",
            return_type="EmailMessage",
            description="Retrieve the most recent email from the inbox.",
        ),
        ToolSignature(
            name="send_email",
            signature="recipient: str, subject: str, body: str",
            return_type="None",
            description="Send an email to recipient.",
        ),
    ]

    async def main() -> None:
        plan = await wrapper.generate_plan(
            user_query="Forward the last email to alice@example.com",
            tool_signatures=tools,
            user_context={"user_email": "bob@example.com"},
        )
        print(plan.source)

    asyncio.run(main())
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from camel.llm.backend import LLMBackend
from camel.llm.protocols import Message

if TYPE_CHECKING:
    pass

__all__ = [
    # Data classes
    "ToolSignature",
    "CodePlan",
    # Exceptions
    "PLLMError",
    "CodeBlockNotFoundError",
    "PLLMRetryExhaustedError",
    "PLLMIsolationError",
    # Core helpers
    "CodeBlockParser",
    # Wrapper
    "PLLMWrapper",
    # Type alias
    "UserContext",
]

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Trusted key-value metadata injected into the user-context section of the
#: system prompt.  Values MUST come from trusted platform sources — never from
#: tool return values.
UserContext = Mapping[str, str]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSignature:
    """Human-readable description of a CaMeL tool for P-LLM prompt injection.

    Attributes
    ----------
    name:
        The Python function name the P-LLM will use in generated code.
    signature:
        Parameter list string, e.g. ``"recipient: str, subject: str"``.
        Uses Python type-annotation syntax so the P-LLM can generate correctly
        typed calls.  Pass an empty string for zero-parameter tools.
    return_type:
        Nominal return type name shown to the P-LLM.  Intentionally a plain
        string (not a schema) — the P-LLM must treat returned objects as
        opaque per the M2-F13 instruction.
    description:
        One-sentence description of what the tool does.  Shown verbatim in the
        system prompt's tool-signatures section.
    """

    name: str
    signature: str
    return_type: str
    description: str


@dataclass(frozen=True)
class CodePlan:
    """A validated CaMeL pseudo-Python execution plan extracted from a P-LLM response.

    Attributes
    ----------
    source:
        The extracted Python source code (without Markdown fence markers).
        This is the string passed to the CaMeL interpreter for execution.
    """

    source: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PLLMError(Exception):
    """Base class for all P-LLM wrapper errors."""


class CodeBlockNotFoundError(PLLMError):
    """Raised when no fenced code block is found in a P-LLM response.

    Attributes
    ----------
    response:
        The raw P-LLM response text.  Stored for debugging; never forwarded
        to the model as-is (redaction rules apply during retries).
    """

    def __init__(self, response: str) -> None:
        self.response = response
        super().__init__(
            "No ```python … ``` fenced code block found in P-LLM response."
        )


class PLLMRetryExhaustedError(PLLMError):
    """Raised when all retry attempts fail to produce a valid execution plan.

    Attributes
    ----------
    attempts:
        The number of generation attempts made before giving up.
    """

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            f"P-LLM retry loop exhausted after {attempts} attempt(s) without "
            "producing a parseable, syntactically valid code block."
        )


class PLLMIsolationError(PLLMError):
    """Raised when a caller attempts to pass a CaMeLValue into the P-LLM.

    This is a programming error — tool return values (capability-tagged
    :class:`~camel.value.CaMeLValue` instances) must never reach the P-LLM
    prompt.  The interpreter's variable store is the only sanctioned holder
    of these values.
    """


# ---------------------------------------------------------------------------
# Code-block parser
# ---------------------------------------------------------------------------


class CodeBlockParser:
    """Extract a CaMeL pseudo-Python code block from a P-LLM response.

    The parser accepts both `` ```python `` and bare `` ``` `` fences.  Only
    the **first** fenced block is extracted; additional blocks are ignored.
    The extracted code is stripped of leading/trailing whitespace.
    """

    #: Regex matching the first Markdown-fenced code block (python or bare).
    FENCE_PATTERN: re.Pattern[str] = re.compile(
        r"```(?:python)?\s*\n(.*?)\n```",
        re.DOTALL | re.IGNORECASE,
    )

    @classmethod
    def extract(cls, response: str) -> str:
        """Return the content of the first ```python … ``` block in *response*.

        Parameters
        ----------
        response:
            Raw text returned by the P-LLM backend.

        Returns
        -------
        str
            The code content (without the fence markers), stripped of
            leading/trailing whitespace.

        Raises
        ------
        CodeBlockNotFoundError
            When no fenced code block is found or the extracted block is empty
            after stripping.
        """
        match = cls.FENCE_PATTERN.search(response)
        if match is None:
            raise CodeBlockNotFoundError(response=response)
        code = match.group(1).strip()
        if not code:
            raise CodeBlockNotFoundError(response=response)
        return code


# ---------------------------------------------------------------------------
# System-prompt section builders (module-level constants + helpers)
# ---------------------------------------------------------------------------

_SECTION_SUBSET_SPEC = textwrap.dedent("""\
    ## CaMeL RESTRICTED PYTHON SUBSET

    You are a planning assistant for the CaMeL (Capabilities for Machine
    Learning) secure agent runtime.  You generate execution plans expressed as
    restricted Python code.

    SUPPORTED CONSTRUCTS ONLY — do not use any other Python syntax:

    Statements:
      - Assignment:         result = tool_call(arg)
      - Augmented assign:   counter += 1
      - Conditional:        if condition: / else:
      - For loop:           for item in collection:
      - Expression stmt:    tool_call(arg)   # for side-effects

    Expressions:
      - Literals:           42, "hello", True, None, [1, 2], {"k": v}, (a, b)
      - Variable reference: my_var
      - Binary operators:   a + b, a - b, a * b, a / b, a // b, a % b, a ** b
      - Unary operators:    -x, not x
      - Boolean operators:  a and b, a or b
      - Comparisons:        ==, !=, <, >, <=, >=, in, not in, is, is not
      - Attribute access:   obj.field
      - Subscript access:   container[key]
      - f-strings:          f"Hello {name}"
      - Function calls:     func(positional, keyword=value)
      - Tuple unpack:       a, b = pair

    FORBIDDEN (raises an interpreter error if generated):
      while, def, class, lambda, import, try/except, with, async/await,
      list/dict/set comprehensions, generators, yield, assert, raise, pass,
      break, continue, return, delete, global, nonlocal, starred (*args),
      double-starred (**kwargs).\
""")

_SECTION_OPAQUE_VARIABLES = textwrap.dedent("""\
    ## CRITICAL — OPAQUE VARIABLES (M2-F13)

    You will NEVER see the actual values that tools return.  Every tool call
    result is stored in a variable whose contents are completely opaque to you.
    You MUST write code that passes these variables to subsequent tool calls
    without attempting to read, inspect, or reason about their contents.

    Examples of CORRECT usage:
      email = get_last_email()
      send_reply(email, body="Acknowledged.")    # ✓ pass opaque variable

    Examples of FORBIDDEN attempts to inspect values:
      if email.sender == "bob@example.com":      # ✗ do not branch on tool data
      body = email.subject.upper()               # ✗ do not assume field access

    The interpreter controls all data access.  Your job is to describe the
    control flow and tool call sequence — not to process data values.\
""")

_SECTION_PRINT_GUIDANCE = textwrap.dedent("""\
    ## OUTPUT TO USER (M2-F10)

    To surface a value to the user, call print() with the variable:

      result = compute_summary(data)
      print(result)              # ✓ instructs the runtime to display this value

    The runtime will display the raw value associated with the variable.
    Do NOT attempt to format or process the value before printing — pass the
    variable directly.

    print() is the ONLY side-effectful output mechanism available.  Do not use
    it to debug intermediate states; use it only for values that the user
    should see as final output.\
""")

_SECTION_OUTPUT_FORMAT = textwrap.dedent("""\
    ## OUTPUT FORMAT

    Respond with exactly ONE fenced Python code block containing the complete
    execution plan.  Do not add any explanation or commentary outside the
    code block.

    ```python
    # your plan here
    ```\
""")


def _build_tool_signatures_section(tools: list[ToolSignature]) -> str:
    """Render the tool-signatures section of the system prompt.

    Each tool is formatted as a Python function stub with a docstring.
    """
    if not tools:
        lines = ["## AVAILABLE TOOLS", "", "No tools are available for this task."]
        return "\n".join(lines)

    lines = [
        "## AVAILABLE TOOLS",
        "",
        "You may call ONLY the following tools.  Each is defined as a Python",
        "function signature.  Do not invent tools not listed here.",
        "",
    ]
    for tool in tools:
        stub = f"def {tool.name}({tool.signature}) -> {tool.return_type}:"
        lines.append(stub)
        lines.append(f'    """{tool.description}"""')
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_user_context_section(context: UserContext) -> str:
    """Render the user-context section of the system prompt."""
    lines = ["## CONTEXT"]
    if context:
        lines.append("")
        for key, value in context.items():
            lines.append(f"{key}: {value}")
    else:
        lines.append("")
        lines.append("(No additional context provided.)")
    return "\n".join(lines)


def _build_error_feedback(error_type: str, lineno: int | None) -> str:
    """Build a redacted error-feedback message for the retry prompt.

    Only ``error_type`` and ``lineno`` are included.  The error *message* text
    is deliberately omitted because it may echo back attacker-controlled
    content (e.g. a ``SyntaxError`` message that reproduces a literal from
    untrusted tool data embedded in the generated code).
    """
    location_part = f"line {lineno}" if lineno is not None else "unknown location"
    return textwrap.dedent(f"""\
        Your previous plan produced an error.

        Error type: {error_type}
        Location:   {location_part}

        Please revise the plan and emit a single corrected ```python``` code \
block.
        Do NOT include any explanation.\
    """)


# ---------------------------------------------------------------------------
# PLLMWrapper
# ---------------------------------------------------------------------------


class PLLMWrapper:
    """Privileged LLM wrapper for CaMeL execution plan generation.

    Wraps a :class:`~camel.llm.backend.LLMBackend` to implement the full
    P-LLM planning path, including system-prompt construction, code-plan
    extraction, and retry logic.

    **Isolation contract** — this wrapper structurally enforces that tool
    return values never reach the P-LLM.  The :meth:`generate_plan` method
    accepts only ``str``-typed content parameters; passing a
    :class:`~camel.value.CaMeLValue` is a type error (mypy strict) and also
    raises :class:`PLLMIsolationError` at runtime.

    Parameters
    ----------
    backend:
        Any object satisfying the :class:`~camel.llm.backend.LLMBackend`
        structural protocol (e.g. :class:`~camel.llm.adapters.ClaudeBackend`
        or :class:`~camel.llm.adapters.GeminiBackend`).
    max_retries:
        Maximum number of code-generation attempts before raising
        :class:`PLLMRetryExhaustedError`.  Default: 10 (per PRD §6.1).
    """

    def __init__(
        self,
        backend: LLMBackend,
        max_retries: int = 10,
    ) -> None:
        self._backend = backend
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_system_prompt(
        self,
        tool_signatures: list[ToolSignature],
        user_context: UserContext | None = None,
    ) -> str:
        """Assemble the five-section P-LLM system prompt.

        The sections are assembled in this order:

        1. **CaMeL Python subset specification** — describes the restricted
           grammar the P-LLM must generate.
        2. **Opaque-variable instruction** (M2-F13) — instructs the P-LLM that
           tool return values are opaque handles; it must never inspect them.
        3. **Tool signatures** — Python function stubs with docstrings for
           each registered tool.
        4. **User context** — trusted key-value metadata (e.g. current date,
           session ID).
        5. **print() usage guidance** (M2-F10) — instructs the P-LLM to use
           ``print()`` for user-visible output only.

        An output-format instruction section is appended after the five
        required sections to guide the model's response structure.

        Parameters
        ----------
        tool_signatures:
            Ordered list of :class:`ToolSignature` objects describing the
            tools available in this session.
        user_context:
            Optional trusted key-value metadata.  Passed to the user-context
            section verbatim.  MUST NOT contain tool return values.

        Returns
        -------
        str
            The fully assembled system prompt string.
        """
        sections = [
            _SECTION_SUBSET_SPEC,
            _SECTION_OPAQUE_VARIABLES,
            _build_tool_signatures_section(tool_signatures),
            _build_user_context_section(user_context or {}),
            _SECTION_PRINT_GUIDANCE,
            _SECTION_OUTPUT_FORMAT,
        ]
        return "\n\n".join(sections)

    def parse_code_plan(self, response: str) -> CodePlan:
        """Extract a :class:`CodePlan` from a raw P-LLM response string.

        Delegates to :class:`CodeBlockParser` to locate the first
        ``python``-fenced block, then wraps the extracted source in a
        :class:`CodePlan` dataclass.

        Parameters
        ----------
        response:
            Raw text response from the P-LLM backend.

        Returns
        -------
        CodePlan
            A :class:`CodePlan` wrapping the extracted source string.

        Raises
        ------
        CodeBlockNotFoundError
            When no fenced code block is found or the block is empty.
        """
        source = CodeBlockParser.extract(response)
        return CodePlan(source=source)

    async def generate_plan(
        self,
        user_query: str,
        tool_signatures: list[ToolSignature],
        user_context: UserContext | None = None,
    ) -> CodePlan:
        """Generate a CaMeL pseudo-Python execution plan for *user_query*.

        Builds the system prompt, sends *user_query* as the human turn, and
        parses the fenced code block from the P-LLM response.  Retries up to
        ``max_retries`` times on :class:`CodeBlockNotFoundError` or
        ``SyntaxError``.  Each retry appends the previous assistant response
        and a **redacted** error-feedback message (error type + line number
        only — message text is never included, as it may echo attacker-
        controlled content from untrusted data embedded in generated code).

        Parameters
        ----------
        user_query:
            The trusted user request.  This is the ONLY content that describes
            what the agent should do.  Tool return values MUST NOT appear here.
        tool_signatures:
            Ordered list of :class:`ToolSignature` objects available in this
            session.  Injected into the system prompt's tool-signatures
            section.
        user_context:
            Optional trusted key-value metadata injected into the
            user-context section of the system prompt.  Values MUST come from
            trusted platform sources, never from tool return values.

        Returns
        -------
        CodePlan
            A :class:`CodePlan` wrapping the extracted, syntactically valid
            source string.

        Raises
        ------
        PLLMIsolationError
            When *user_query* or any *user_context* value is a
            :class:`~camel.value.CaMeLValue` instance (runtime isolation
            guard).
        PLLMRetryExhaustedError
            When all ``max_retries`` attempts fail to produce a parseable,
            syntactically valid code block.
        ~camel.llm.backend.LLMBackendError
            On any backend API failure.  Propagated immediately without
            retrying.
        """
        self._guard_no_camel_values(user_query, user_context)

        system_prompt = self.build_system_prompt(tool_signatures, user_context)
        messages: list[Message] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ]

        previous_raw: str | None = None
        previous_error_type: str | None = None
        previous_lineno: int | None = None

        for attempt in range(self._max_retries):
            if attempt > 0 and previous_raw is not None:
                # Append previous assistant turn + redacted error feedback.
                assert previous_error_type is not None  # always set when attempt > 0
                messages.append({"role": "assistant", "content": previous_raw})
                messages.append(
                    {
                        "role": "user",
                        "content": _build_error_feedback(
                            previous_error_type, previous_lineno
                        ),
                    }
                )

            raw_response = await self._backend.generate(messages)

            try:
                plan = self.parse_code_plan(raw_response)
                ast.parse(plan.source)  # lightweight syntax check
                return plan
            except CodeBlockNotFoundError as exc:
                previous_raw = raw_response
                previous_error_type = type(exc).__name__
                previous_lineno = None
            except SyntaxError as exc:
                previous_raw = raw_response
                previous_error_type = type(exc).__name__
                previous_lineno = exc.lineno

        raise PLLMRetryExhaustedError(attempts=self._max_retries)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _guard_no_camel_values(
        user_query: str,
        user_context: UserContext | None,
    ) -> None:
        """Raise :class:`PLLMIsolationError` if any input is a CaMeLValue.

        This is the runtime leg of the three-layer isolation contract.  It
        guards against callers that bypass the static type checker (e.g. via
        ``# type: ignore`` or dynamic dispatch) and accidentally pass a
        capability-tagged interpreter value into the P-LLM prompt.

        Parameters
        ----------
        user_query:
            The user query string to validate.
        user_context:
            The optional context mapping to validate.

        Raises
        ------
        PLLMIsolationError
            If *user_query* or any value in *user_context* is a
            :class:`~camel.value.CaMeLValue` instance.
        """
        # Import lazily to avoid a circular dependency between camel.llm and
        # camel.value at module load time.
        from camel.value import CaMeLValue  # noqa: PLC0415

        if isinstance(user_query, CaMeLValue):
            raise PLLMIsolationError(
                "user_query must be a plain str; received a CaMeLValue — "
                "tool return values must not reach the P-LLM prompt."
            )
        if user_context:
            for key, value in user_context.items():
                if isinstance(value, CaMeLValue):
                    raise PLLMIsolationError(
                        f"user_context[{key!r}] must be a plain str; received a "
                        "CaMeLValue — tool return values must not reach the P-LLM "
                        "prompt."
                    )
