"""Shared fixtures and module-level mocks for tactical-llm tests.

tactical_llm.py instantiates a Llama model at module level. Without this
mock the import itself raises ValueError (model file does not exist).
The patch must run before any test file imports tactical_llm.
"""

from unittest.mock import MagicMock, patch

# llama_cpp.Llama is loaded at tactical_llm import time.
# patch() works here (unlike transformers, llama_cpp has no lazy-module override).
patch("llama_cpp.Llama", MagicMock()).start()
