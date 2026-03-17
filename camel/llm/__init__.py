"""camel.llm — LLM backend protocols, schemas, exceptions, and Q-LLM wrapper.

Public API
----------
Protocols
~~~~~~~~~
.. autoclass:: LLMBackend
.. autoclass:: QlLMBackend

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
"""

from camel.llm.exceptions import NotEnoughInformationError
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
    # Q-LLM wrapper
    "QLLMWrapper",
    "make_qllm_wrapper",
]
