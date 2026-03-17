"""camel.llm — LLM backend protocols, schemas, exceptions, and wrappers.

Public API
----------
Protocols
~~~~~~~~~
.. autoclass:: LLMBackend
.. autoclass:: QlLMBackend

Backend
~~~~~~~
.. autoclass:: LLMBackendError
.. autofunction:: get_backend

Schemas
~~~~~~~
.. autoclass:: QResponse

Exceptions
~~~~~~~~~~
.. autoclass:: NotEnoughInformationError

Q-LLM wrapper
~~~~~~~~~~~~~
.. autoclass:: QLLMWrapper
.. autofunction:: make_qllm_wrapper

P-LLM wrapper
~~~~~~~~~~~~~
.. autoclass:: PLLMWrapper
.. autoclass:: ToolSignature
.. autoclass:: CodePlan
.. autoclass:: PLLMError
.. autoclass:: CodeBlockNotFoundError
.. autoclass:: PLLMRetryExhaustedError
.. autoclass:: PLLMIsolationError
.. autoclass:: CodeBlockParser
"""

from camel.llm.backend import LLMBackendError, get_backend
from camel.llm.exceptions import NotEnoughInformationError
from camel.llm.p_llm import (
    CodeBlockNotFoundError,
    CodeBlockParser,
    CodePlan,
    PLLMError,
    PLLMIsolationError,
    PLLMRetryExhaustedError,
    PLLMWrapper,
    ToolSignature,
    UserContext,
)
from camel.llm.protocols import LLMBackend, Message, QlLMBackend, QResponseT
from camel.llm.qllm import QLLMWrapper, make_qllm_wrapper
from camel.llm.schemas import QResponse

__all__ = [
    # Protocols
    "LLMBackend",
    "QlLMBackend",
    # Type aliases / TypeVars
    "Message",
    "QResponseT",
    # Schemas
    "QResponse",
    # Exceptions
    "NotEnoughInformationError",
    # Backend factory & errors
    "LLMBackendError",
    "get_backend",
    # Q-LLM wrapper
    "QLLMWrapper",
    "make_qllm_wrapper",
    # P-LLM wrapper
    "PLLMWrapper",
    "ToolSignature",
    "CodePlan",
    "UserContext",
    "PLLMError",
    "CodeBlockNotFoundError",
    "PLLMRetryExhaustedError",
    "PLLMIsolationError",
    "CodeBlockParser",
]
