"""Subprocess output is drained without retaining unbounded logs."""
import asyncio

import pytest

from app.safety.test_runner import _read_bounded


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_reader_retains_only_the_last_bounded_bytes():
    reader = asyncio.StreamReader()
    payload = b"a" * 1500 + b"tail"
    reader.feed_data(payload)
    reader.feed_eof()

    retained, truncated = await _read_bounded(reader, limit=1024)

    assert truncated is True
    assert len(retained) == 1024
    assert retained.endswith(b"tail")
