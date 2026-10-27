import asyncio
import logging
from contextlib import suppress

from app.main import log_crash


async def test_a_crashed_background_task_is_logged(caplog):
    async def crash() -> None:
        raise RuntimeError("bug")

    task = asyncio.create_task(crash(), name="gateway")
    with suppress(RuntimeError):
        await task

    with caplog.at_level(logging.CRITICAL):
        log_crash(task)

    assert "gateway stopped" in caplog.text


async def test_a_cancelled_background_task_is_not_logged(caplog):
    task = asyncio.create_task(asyncio.Event().wait(), name="gateway")
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    log_crash(task)

    assert caplog.text == ""
