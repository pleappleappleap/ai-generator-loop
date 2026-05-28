from unittest.mock import MagicMock, patch

import pytest
import transformers

# Block heavy model constructors before any scorer module is imported.
# Modules instantiate models at the top level; without these patches the
# import itself tries to download / load weights.
#
# transformers uses _LazyModule so patch("transformers.pipeline") is
# ineffective — eager import + direct attribute assignment is required.
transformers.pipeline = MagicMock()
patch("llama_cpp.Llama", MagicMock()).start()
patch("llama_cpp.llama_chat_format.Qwen25VLChatHandler", MagicMock()).start()
patch("open_clip.create_model_and_transforms", return_value=(MagicMock(), None, MagicMock())).start()
patch("open_clip.get_tokenizer", return_value=MagicMock()).start()


@pytest.fixture(scope="module")
def clip_client():
    from fastapi.testclient import TestClient
    from clip_scorer import app
    return TestClient(app)


@pytest.fixture(scope="module")
def artifact_client():
    from fastapi.testclient import TestClient
    from artifact_scorer import app
    return TestClient(app)


@pytest.fixture(scope="module")
def vlm_client():
    from fastapi.testclient import TestClient
    from vlm_scorer import app
    return TestClient(app)
