import asyncio

import pytest

from app.api import network_switch


@pytest.mark.asyncio
async def test_track_background_task_keeps_reference_until_done():
    async def _worker():
        await asyncio.sleep(0)
        return 1

    task = asyncio.create_task(_worker())
    network_switch._track_background_task(task)

    assert task in network_switch._background_tasks

    await task
    await asyncio.sleep(0)

    assert task not in network_switch._background_tasks
