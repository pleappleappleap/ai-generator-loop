import pytest
import fakeredis
import pika
from unittest.mock import MagicMock


@pytest.fixture
def fake_redis():
    """In-memory Redis with full Redis semantics."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def mock_channel():
    """Mock pika channel."""
    return MagicMock(spec=pika.channel.Channel)


@pytest.fixture
def mock_method():
    """Mock pika delivery method."""
    method = MagicMock(spec=pika.spec.Basic.Deliver)
    method.delivery_tag = 1
    return method


@pytest.fixture
def mock_props():
    """Mock pika basic properties."""
    return MagicMock(spec=pika.spec.BasicProperties)
