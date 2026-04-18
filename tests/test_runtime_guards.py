import time

import pytest

from app.harness.tool_runtime import invoke_tool_handler
from app.tools.file_export import _run_with_timeout_no_wait
from app.tools.tool_result import ToolResult


@pytest.mark.asyncio
async def test_invoke_tool_handler_awaits_coroutine_result_from_sync_wrapper():
    def handler(_args):
        async def _inner():
            return ToolResult.success("ok")
        return _inner()

    result = await invoke_tool_handler(handler, {})
    assert isinstance(result, ToolResult)
    assert result.ok
    assert result.content == "ok"


def test_run_with_timeout_no_wait_returns_quickly_on_timeout():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        _run_with_timeout_no_wait(lambda: time.sleep(0.4), timeout_s=0.05)
    elapsed = time.monotonic() - start
    assert elapsed < 0.25
    # 释放单线程执行器里的挂起任务，避免影响后续测试
    time.sleep(0.45)


def test_run_with_timeout_no_wait_rejects_new_job_while_previous_is_running():
    with pytest.raises(TimeoutError):
        _run_with_timeout_no_wait(lambda: time.sleep(0.6), timeout_s=0.05)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        _run_with_timeout_no_wait(lambda: "ok", timeout_s=0.05)
    elapsed = time.monotonic() - start
    assert elapsed < 0.2

    # 等前一个渲染线程完成后，可再次提交任务
    time.sleep(0.65)
    assert _run_with_timeout_no_wait(lambda: "ok", timeout_s=0.2) == "ok"
