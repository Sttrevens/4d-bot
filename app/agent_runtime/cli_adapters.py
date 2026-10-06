from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent_runtime.providers import get_provider_manifest
from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse, RuntimeUsage

ProcessRunner = Callable[
    [list[str]],
    Awaitable[tuple[int, str, str]],
]


async def run_process(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    stdin: str,
    timeout_seconds: int,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
        env=env or None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode("utf-8") if stdin else None),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return (
        int(process.returncode or 0),
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


class LocalCliRuntimeClient:
    provider_name = "custom_command"

    def __init__(
        self,
        *,
        timeout_seconds: int = 180,
        process_runner: Callable[..., Awaitable[tuple[int, str, str]]] = run_process,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.process_runner = process_runner

    async def run_turn(self, request: RuntimeRequest) -> RuntimeResponse:
        command = self.build_command(request)
        if not command:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_bad_request",
                f"{self.provider_name} command is not configured",
                retryable=False,
                runtime=self.provider_name,
            )
        try:
            code, stdout, stderr = await self.process_runner(
                command,
                cwd=request.runtime.workspace,
                env=self._env(),
                stdin="",
                timeout_seconds=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return RuntimeResponse.failed(
                request.run_id,
                "timed_out",
                f"{self.provider_name} exceeded {self.timeout_seconds}s",
                retryable=True,
                runtime=self.provider_name,
            )
        except FileNotFoundError as exc:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_unavailable",
                f"{self.provider_name} command not found: {exc}",
                retryable=True,
                runtime=self.provider_name,
            )
        except Exception as exc:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_unavailable",
                f"{self.provider_name} unavailable: {exc}",
                retryable=True,
                runtime=self.provider_name,
            )
        if code != 0:
            message = (stderr or stdout or f"{self.provider_name} exited with {code}").strip()
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_failed",
                message,
                retryable=True,
                runtime=self.provider_name,
            )
        return self.parse_stdout(request, stdout)

    def build_command(self, request: RuntimeRequest) -> list[str]:
        manifest = get_provider_manifest(self.provider_name)
        command = request.runtime.command or (manifest.default_command if manifest else "")
        if not command:
            return []
        return shlex.split(command) + list(request.runtime.args) + [request.input.text]

    def parse_stdout(self, request: RuntimeRequest, stdout: str) -> RuntimeResponse:
        return RuntimeResponse(
            run_id=request.run_id,
            runtime=self.provider_name,
            status="completed",
            final_text=stdout.strip(),
        )

    def _env(self) -> dict[str, str]:
        return dict(os.environ)


class CodexCliRuntimeClient(LocalCliRuntimeClient):
    provider_name = "codex_cli"

    def build_command(self, request: RuntimeRequest) -> list[str]:
        command = request.runtime.command or "codex"
        args = shlex.split(command) + list(request.runtime.args)
        args += ["exec", "--json", "--sandbox", request.runtime.sandbox or "read-only"]
        if request.runtime.model:
            args += ["--model", request.runtime.model]
        args.append(request.input.text)
        return args

    def parse_stdout(self, request: RuntimeRequest, stdout: str) -> RuntimeResponse:
        final_text = ""
        thread_id = ""
        usage = RuntimeUsage()
        events: list[dict[str, Any]] = []
        for item in _iter_json_objects(stdout):
            events.append(item)
            if item.get("type") == "thread.started":
                thread_id = str(item.get("thread_id") or "")
            if item.get("type") == "item.completed":
                payload = item.get("item") if isinstance(item.get("item"), dict) else {}
                if payload.get("type") == "agent_message":
                    final_text = str(payload.get("text") or final_text)
            if item.get("type") == "turn.completed" and isinstance(item.get("usage"), dict):
                raw_usage = item["usage"]
                usage = RuntimeUsage(
                    input_tokens=int(raw_usage.get("input_tokens") or 0),
                    output_tokens=int(raw_usage.get("output_tokens") or 0),
                    api_calls=1,
                )
        return RuntimeResponse(
            run_id=request.run_id,
            runtime=self.provider_name,
            status="completed",
            final_text=final_text or stdout.strip(),
            usage=usage,
            events=events,
            resume={"resumable": bool(thread_id), "resume_token": thread_id},
        )


class ClaudeCliRuntimeClient(LocalCliRuntimeClient):
    provider_name = "claude_cli"

    def build_command(self, request: RuntimeRequest) -> list[str]:
        command = request.runtime.command or "claude"
        permission_mode = request.runtime.permission_mode or "plan"
        args = shlex.split(command) + list(request.runtime.args)
        args += [
            "-p",
            "--output-format",
            "stream-json",
            "--permission-mode",
            permission_mode,
            "--max-turns",
            str(max(1, int(request.runtime.max_rounds or 1))),
        ]
        if request.runtime.model:
            args += ["--model", request.runtime.model]
        args.append(request.input.text)
        return args

    def parse_stdout(self, request: RuntimeRequest, stdout: str) -> RuntimeResponse:
        final_text = ""
        session_id = ""
        events: list[dict[str, Any]] = []
        for item in _iter_json_objects(stdout):
            events.append(item)
            if item.get("type") == "result":
                final_text = str(item.get("result") or item.get("text") or final_text)
                session_id = str(item.get("session_id") or item.get("sessionId") or session_id)
            if item.get("type") == "assistant" and isinstance(item.get("message"), dict):
                final_text = _text_from_claude_message(item["message"]) or final_text
        return RuntimeResponse(
            run_id=request.run_id,
            runtime=self.provider_name,
            status="completed",
            final_text=final_text or stdout.strip(),
            events=events,
            resume={"resumable": bool(session_id), "resume_token": session_id},
        )


class CustomCommandRuntimeClient(LocalCliRuntimeClient):
    provider_name = "custom_command"


def _iter_json_objects(stdout: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
    if not items:
        try:
            parsed = json.loads(stdout)
        except ValueError:
            return []
        if isinstance(parsed, dict):
            return [parsed]
    return items


def _text_from_claude_message(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)
