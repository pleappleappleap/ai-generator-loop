import json
from unittest.mock import MagicMock, patch

import pytest
import stomp

# Block heavy model constructors before any scorer module is imported.
# artifact_scorer and vlm_scorer instantiate models at module level; without
# these patches the import itself tries to download / load model weights.
#
# transformers uses _LazyModule which makes patch("transformers.pipeline")
# ineffective — eager import + direct assignment is required.
import transformers  # noqa: E402

transformers.pipeline = MagicMock()
patch("llama_cpp.Llama", MagicMock()).start()


@pytest.fixture
def mock_conn():
    """Mock STOMP connection."""
    return MagicMock(spec=stomp.Connection)


def make_frame(sub_id: str, msg_id: str, body: dict) -> MagicMock:
    """Build a mock stomp.utils.Frame with the given subscription and body."""
    frame = MagicMock(spec=stomp.utils.Frame)
    frame.headers = {"subscription": sub_id, "message-id": msg_id}
    frame.body = json.dumps(body)
    return frame
