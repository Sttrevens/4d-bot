"""Optional a13n Harness worker behind the existing runtime/tool contracts.

No SDK import belongs here: a13n's Python 3.13 worker is operator-provisioned,
while the bot stays on Python 3.12. SDK state is an opaque private checkpoint,
not a replay log or a second memory/tool system.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.agent_runtime.a13n_state import A13nStateStore
from app.harness.tool_runtime import invoke_tool_handler
from app.hermes_runtime.events import RuntimeEvent
from app.hermes_runtime.execution_policy import build_execution_policy
from app.hermes_runtime.observability import record_runtime_event, record_runtime_run_summary
from app.hermes_runtime.tool_bridge import (
    RuntimeToolResult,
    RuntimeToolset,
    build_runtime_toolset,
    execute_tool_call_async,
)
from app.hermes_runtime.tool_policy import side_effect_class_for_tool
from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse, RuntimeToolCallRecord, RuntimeUsage
from app.tenant.context import get_current_tenant

MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_WORKER_ENV = ("PATH", "OPENAI_API_KEY", "OPENAI_BASE_URL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")


class WorkerProtocolError(Exception):
    pass


def _identifier(value: Any) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else ""


def _effectful(name: str) -> bool:
    # Unclassified/custom tools cannot silently become read-only.
    return side_effect_class_for_tool(name) != "read_only"


def _thread_id(request: RuntimeRequest) -> str:
    scope = [request.tenant_id, request.channel_id, request.platform,
             request.conversation.history_key, request.sender.identity_id,
             request.sender.sender_id, "shadow" if request.policy.shadow_mode else "live"]
    return "bot_" + hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()


def _request_digest(request: RuntimeRequest) -> str:
    return hashlib.sha256(json.dumps(request.to_dict(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _permission(request: RuntimeRequest, tenant: Any, name: str) -> str:
    configured = getattr(tenant, "a13n_tool_permissions", {}) or {}
    mode = configured.get(name, "allow" if not _effectful(name) or request.runtime.auto_execute else "ask")
    if mode not in {"allow", "ask", "deny", "review"}:
        mode = "deny"
    if request.policy.shadow_mode and _effectful(name):
        return "deny"
    if _effectful(name) and not request.runtime.auto_execute and mode == "allow":
        return "ask"
    return mode


def _validators(toolset: RuntimeToolset, visible: set[str]) -> dict[str, Any]:
    from jsonschema import Draft202012Validator
    from referencing import Registry

    validators = {}
    for item in toolset.tools:
        if item.name not in visible:
            continue
        function = item.schema.get("function", item.schema)
        parameters = function.get("parameters", function)
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise WorkerProtocolError("a13n_tool_schema_invalid")
        Draft202012Validator.check_schema(parameters)
        # Tool schemas are Host definitions. Remote references may not cause
        # hidden network access during permission checks.
        validators[item.name] = Draft202012Validator(parameters, registry=Registry())
    return validators


async def _build_prompt(request: RuntimeRequest, visible: set[str]) -> str:
    from app.services.base_agent import _build_system_prompt

    return await _build_system_prompt(mode=request.conversation.mode,
        sender_id=request.sender.sender_id, sender_name=request.sender.sender_name,
        user_text=request.input.text, chat_id=request.conversation.chat_id,
        chat_type=request.conversation.chat_type, actual_tool_names=visible)


class A13nRuntimeClient:
    provider_name = "a13n_harness"

    def __init__(self, *, timeout_seconds: int = 180, state_dir: str | Path | None = None,
                 process_factory: Callable[..., Any] = asyncio.create_subprocess_exec,
                 toolset_builder: Callable[..., RuntimeToolset] = build_runtime_toolset,
                 prompt_builder: Callable[[RuntimeRequest, set[str]], Awaitable[str]] = _build_prompt) -> None:
        self.timeout_seconds = max(1, timeout_seconds)
        self.store = A13nStateStore(state_dir or os.getenv("A13N_STATE_DIR", ".runtime/a13n"))
        self.state_dir = self.store.root
        self.process_factory = process_factory
        self.toolset_builder = toolset_builder
        self.prompt_builder = prompt_builder

    async def run_turn(self, request: RuntimeRequest) -> RuntimeResponse:
        thread_id = _thread_id(request)
        progress: dict[str, Any] = {"locked": False, "effects_started": False, "records": [], "events": []}
        try:
            tenant = get_current_tenant()
            if tenant.tenant_id != request.tenant_id:
                return self._failure(request, thread_id, "tenant_mismatch", safe=False)
            command = shlex.split(request.runtime.command) + list(request.runtime.args)
            async with asyncio.timeout(self.timeout_seconds):
                async with self.store.lock(thread_id):
                    progress["locked"] = True
                    return await self._run_locked(request, tenant, thread_id, command, progress)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            response = self._failure(request, thread_id, "timed_out",
                                     safe=progress["locked"] and not progress["effects_started"])
            response.tool_calls = progress["records"]
            for record in response.tool_calls:
                if record.status == "running":
                    record.status = "unknown"
                    record.code = "timed_out"
            response.usage.tool_calls = len(response.tool_calls)
            response.events = progress["events"]
            return response
        except Exception:
            # Checkpoint reads/lock acquisition fail closed; do not launch a
            # second provider when an unresolved journal may be unreadable.
            return self._failure(request, thread_id, "a13n_state_unavailable", safe=False)

    async def _run_locked(self, request: RuntimeRequest, tenant: Any, thread_id: str,
                          command: list[str], progress: dict[str, Any]) -> RuntimeResponse:
        digest = _request_digest(request)
        run_filename = self.store.run_filename(request.run_id)
        journal = self.store.read(thread_id, "active.json")
        prior = self.store.read(thread_id, run_filename)
        if prior:
            if prior.get("request_digest") != digest:
                return self._failure(request, thread_id, "run_id_reused", safe=False)
            cached = RuntimeResponse.from_dict(prior["response"])
            if journal and journal.get("status") in {"running", "side_effect_started", "needs_confirmation"} \
                    and cached.resume.get("fallback_safe", False):
                return self._failure(request, thread_id, "reconciliation_required", safe=False)
            return cached
        if journal and journal.get("status") in {"running", "side_effect_started"}:
            return self._failure(request, thread_id, "reconciliation_required", safe=False)
        if journal and journal.get("status") == "needs_confirmation" and not request.conversation.fresh_start:
            return self._failure(request, thread_id, "approval_resume_unavailable", safe=False)
        if not request.runtime.command.strip() or not command:
            return self._failure(request, thread_id, "runtime_unavailable", safe=True)
        checkpoint = self.store.read(thread_id, "state.json")
        if checkpoint and checkpoint.get("thread_id") != thread_id:
            return self._failure(request, thread_id, "state_scope_mismatch", safe=False)
        saved_state = None if request.conversation.fresh_start else (checkpoint or {}).get("state")
        if request.conversation.fresh_start and checkpoint:
            self.store.write(thread_id, "state.json", {"thread_id": thread_id, "state": None})
        toolset = self.toolset_builder(tenant, user_text=request.input.text,
                                      override_groups=set(request.policy.allowed_tool_groups) or None)
        if toolset.tenant_id and toolset.tenant_id != request.tenant_id:
            return self._failure(request, thread_id, "tenant_mismatch", safe=False)
        visible = {item.name for item in toolset.tools}
        if request.policy.allowed_tool_names:
            visible &= set(request.policy.allowed_tool_names)
        permissions = {name: _permission(request, tenant, name) for name in visible}
        validators = _validators(toolset, visible)
        instructions = await self.prompt_builder(request, visible)
        execution_policy = build_execution_policy(tenant, admin=request.policy.admin)
        journal = {"run_id": request.run_id, "request_digest": digest, "status": "running",
                   "side_effect_started": False}
        self.store.write(thread_id, "active.json", journal)
        effects_started = False
        uncertain_effect = False
        checkpoint_failed = False
        records: list[RuntimeToolCallRecord] = []
        events: list[dict[str, Any]] = []
        progress.update(records=records, events=events)
        sdk_run_id = ""
        sdk_thread_id = ""
        process = None
        stderr_task = None
        active_call_id = ""

        def emit(event: str, payload: dict[str, Any] | None = None, *, tool: str = "") -> None:
            observed = RuntimeEvent(run_id=request.run_id, tenant_id=request.tenant_id,
                channel_id=request.channel_id, session_id=thread_id, platform=request.platform,
                runtime=self.provider_name, provider=self.provider_name, event=event, tool=tool,
                payload={"thread_id": thread_id, "sdk_run_id": sdk_run_id,
                         "sdk_thread_id": sdk_thread_id, **(payload or {})})
            if len(events) < 256:
                events.append(observed.to_dict())
            record_runtime_event(observed)

        def wrap(name: str, handler: Callable[..., Any]):
            async def dispatch(args: dict[str, Any]):
                nonlocal effects_started, uncertain_effect, checkpoint_failed
                # This wrapper runs only AFTER all existing bridge policy gates.
                if _effectful(name):
                    journal.update(status="side_effect_started", side_effect_started=True,
                                   tool_name=name, call_id=active_call_id,
                                   args_digest=hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest())
                    try:
                        self.store.write(thread_id, "active.json", journal)
                    except Exception:
                        checkpoint_failed = True
                        raise
                    effects_started = True
                    progress["effects_started"] = True
                emit("runtime.tool.started", {"side_effect": _effectful(name)}, tool=name)
                try:
                    return await invoke_tool_handler(handler, args)
                except Exception:
                    uncertain_effect = _effectful(name)
                    raise
            return dispatch

        bridged = RuntimeToolset(tools=toolset.tools, tenant_id=request.tenant_id,
            handlers={name: wrap(name, handler) for name, handler in toolset.handlers.items() if name in visible})
        ledger: list[dict[str, Any]] = []
        emit("runtime.started", {"tool_count": len(visible), "shadow_mode": request.policy.shadow_mode})
        try:
            process = await self.process_factory(*command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=request.runtime.workspace or None,
                env={**{key: os.environ[key] for key in _WORKER_ENV if key in os.environ}, "PYTHONUNBUFFERED": "1"},
                limit=MAX_LINE_BYTES + 1)
            stderr_task = asyncio.create_task(self._drain_stderr(process))
            await self._send(process, {"type": "start", "protocol_version": 1,
                "request": request.to_dict(), "thread_id": thread_id, "state": saved_state,
                "instructions": instructions,
                "tools": [{"name": item.name, "schema": item.schema, "permission": permissions[item.name]}
                          for item in toolset.tools if item.name in visible]})
            seen_calls: set[str] = set()
            output_bytes = 0
            for _ in range(4096):
                try:
                    raw = await process.stdout.readline()
                except ValueError:
                    raise WorkerProtocolError("a13n_protocol_error") from None
                output_bytes += len(raw)
                if not raw or len(raw) > MAX_LINE_BYTES or output_bytes > MAX_OUTPUT_BYTES:
                    raise WorkerProtocolError("a13n_protocol_error")
                try:
                    packet = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise WorkerProtocolError("a13n_protocol_error") from None
                if not isinstance(packet, dict) or packet.get("protocol_version", 1) != 1:
                    raise WorkerProtocolError("a13n_protocol_error")
                packet_run_id = _identifier(packet.get("sdk_run_id"))
                packet_thread_id = _identifier(packet.get("sdk_thread_id"))
                if packet_thread_id and packet_thread_id != thread_id:
                    raise WorkerProtocolError("a13n_thread_mismatch")
                if sdk_run_id and packet_run_id and packet_run_id != sdk_run_id:
                    raise WorkerProtocolError("a13n_run_mismatch")
                sdk_run_id = packet_run_id or sdk_run_id
                sdk_thread_id = packet_thread_id or sdk_thread_id
                kind = packet.get("type")
                if kind == "event":
                    data = packet.get("data") if isinstance(packet.get("data"), dict) else {}
                    safe_data = {key: value for key, value in data.items()
                                 if key in {"request_index", "message_count"} and isinstance(value, int) and 0 <= value < 10**9}
                    safe_data["worker_event"] = _identifier(packet.get("event"))
                    emit("runtime.a13n.event", safe_data)
                    continue
                if kind == "tool_call":
                    call_id = _identifier(packet.get("call_id"))
                    name = packet.get("name")
                    args = packet.get("args")
                    if not call_id or call_id in seen_calls or not isinstance(name, str) or not isinstance(args, dict):
                        raise WorkerProtocolError("a13n_protocol_error")
                    seen_calls.add(call_id)
                    active_call_id = call_id
                    record = RuntimeToolCallRecord(tool_name=name if name in visible else "unavailable",
                        status="running", side_effect=_effectful(name))
                    records.append(record)
                    mode = permissions.get(name, "deny")
                    if name not in visible or mode == "deny":
                        result = RuntimeToolResult(ok=False, content="Tool denied by host policy.",
                            code="policy_denied", outcome="blocked", side_effect=_effectful(name))
                    elif not validators[name].is_valid(args):
                        result = RuntimeToolResult(ok=False, content="Tool arguments do not match its schema.",
                            code="invalid_param", outcome="blocked", side_effect=_effectful(name))
                    elif mode in {"ask", "review"}:
                        result = RuntimeToolResult(ok=False, content="Tool requires explicit approval.",
                            code="confirmation_required", outcome="needs_confirmation", side_effect=_effectful(name))
                    else:
                        result = await execute_tool_call_async(bridged, request.policy, name, args,
                            run_id=request.run_id, tenant_id=request.tenant_id, execution_policy=execution_policy,
                            ledger=ledger)
                    record.status = "success" if result.ok else "failed"
                    record.duration_ms = result.duration_ms
                    record.code = "reconciliation_required" if uncertain_effect else result.code
                    emit("runtime.tool.completed", {"ok": result.ok, "duration_ms": result.duration_ms,
                         "side_effect": _effectful(name), "code": _identifier(result.code)},
                         tool=name if name in visible else "unavailable")
                    if checkpoint_failed:
                        raise WorkerProtocolError("a13n_state_unavailable")
                    if uncertain_effect or result.code == "side_effect_unknown":
                        raise WorkerProtocolError("reconciliation_required")
                    await self._send(process, {"type": "tool_result", "call_id": call_id, "result": result.to_dict()})
                    if result.outcome == "needs_confirmation":
                        emit("runtime.tool.gated", {"permission": mode, "pending_count": 1}, tool=name)
                        response = self._approval(request, thread_id, sdk_run_id,
                            [{"call_id": call_id, "name": name, "permission": mode}])
                        break
                    continue
                if kind != "result":
                    raise WorkerProtocolError("a13n_protocol_error")
                status = packet.get("status")
                usage = self._usage(packet.get("usage"), len(records))
                if status == "awaiting_approval":
                    pending = [{"call_id": _identifier(item.get("call_id")), "name": item.get("name"),
                                "permission": item.get("permission")}
                               for item in (packet.get("pending_approvals") or [])
                               if isinstance(item, dict) and item.get("name") in visible
                               and item.get("permission") in {"ask", "review"}]
                    for item in pending[:256]:
                        emit("runtime.tool.gated", {"permission": item["permission"], "pending_count": len(pending)},
                             tool=item["name"])
                    response = self._approval(request, thread_id, sdk_run_id, pending)
                elif status == "completed":
                    state = packet.get("state")
                    try:
                        parsed_state = json.loads(state) if isinstance(state, str) else None
                    except (ValueError, UnicodeError):
                        raise WorkerProtocolError("a13n_state_invalid") from None
                    if not isinstance(parsed_state, dict) or parsed_state.get("thread_id") != thread_id:
                        raise WorkerProtocolError("a13n_state_invalid")
                    if sdk_thread_id != thread_id or not sdk_run_id:
                        raise WorkerProtocolError("a13n_protocol_error")
                    if not isinstance(packet.get("final_text", ""), str):
                        raise WorkerProtocolError("a13n_protocol_error")
                    # Observe clean worker exit before committing its checkpoint.
                    if await asyncio.wait_for(process.wait(), 5) != 0:
                        raise WorkerProtocolError("runtime_failed")
                    try:
                        self.store.write(thread_id, "state.json", {"thread_id": thread_id,
                            "state": state, "sdk_run_id": sdk_run_id, "sdk_thread_id": sdk_thread_id})
                    except Exception:
                        raise WorkerProtocolError("a13n_state_unavailable") from None
                    response = RuntimeResponse(run_id=request.run_id, runtime=self.provider_name,
                        final_text=packet.get("final_text", ""), usage=usage,
                        resume={"resumable": True, "resume_token": thread_id, "thread_id": thread_id,
                                "sdk_run_id": sdk_run_id, "fallback_safe": False})
                elif status in {"failed", "cancelled"}:
                    response = self._failure(request, thread_id,
                        _identifier(packet.get("error_code")) or "runtime_failed", safe=not effects_started)
                else:
                    raise WorkerProtocolError("a13n_protocol_error")
                response.usage = usage
                break
            else:
                raise WorkerProtocolError("a13n_protocol_error")
        except asyncio.CancelledError:
            # Cancellation is propagated after cleanup. An effect journal stays
            # unresolved so another process cannot unknowingly replay the turn.
            if not effects_started:
                journal["status"] = "cancelled"
                self.store.write(thread_id, "active.json", journal)
            raise
        except Exception as exc:
            code = (str(exc) if isinstance(exc, WorkerProtocolError) else
                    "timed_out" if isinstance(exc, TimeoutError) else "runtime_unavailable")
            response = self._failure(request, thread_id, code,
                                     safe=not effects_started and code != "a13n_state_unavailable")
        finally:
            if process is not None:
                await self._stop(process)
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
        response.tool_calls = records
        response.events = events
        response.usage.tool_calls = max(response.usage.tool_calls, len(records))
        if effects_started and response.status != "completed":
            response.resume.update(fallback_safe=False, reconciliation_required=True)
            if response.error:
                response.error.retryable = False
        def terminal_event(value: RuntimeResponse) -> RuntimeEvent:
            return RuntimeEvent(run_id=request.run_id, tenant_id=request.tenant_id,
                channel_id=request.channel_id, session_id=thread_id, platform=request.platform,
                runtime=self.provider_name, provider=self.provider_name,
                event="runtime.completed" if value.status == "completed" else
                      "runtime.partial" if value.status == "needs_confirmation" else "runtime.failed",
                error=value.error.code if value.error else "",
                cost={"input_tokens": value.usage.input_tokens, "output_tokens": value.usage.output_tokens,
                      "api_calls": value.usage.api_calls, "tool_calls": value.usage.tool_calls},
                payload={"thread_id": thread_id, "sdk_run_id": sdk_run_id, "sdk_thread_id": sdk_thread_id,
                         "status": value.status, "tool_count": len(records)})

        terminal = terminal_event(response)
        response.events = [*events[:255], terminal.to_dict()]
        try:
            self.store.write(thread_id, run_filename, {"request_digest": digest, "response": response.to_dict()})
            journal["status"] = "side_effect_started" if response.resume.get("reconciliation_required") else response.status
            self.store.write(thread_id, "active.json", journal)
        except Exception:
            # This includes paused permission gates: a failed durable receipt
            # must not turn an unexecuted approval into a legacy execution.
            failed = self._failure(request, thread_id, "a13n_state_unavailable", safe=False)
            failed.tool_calls, failed.usage = records, response.usage
            response = failed
            terminal = terminal_event(response)
            response.events = [*events[:255], terminal.to_dict()]
        # Only publish terminal success after the durable receipt and journal.
        record_runtime_event(terminal)
        record_runtime_run_summary({"run_id": request.run_id, "tenant_id": request.tenant_id,
            "channel_id": request.channel_id, "session_id": thread_id, "platform": request.platform,
            "runtime": self.provider_name, "provider": self.provider_name, "last_event": terminal.event,
            "status": response.status, "error": terminal.error, "level": terminal.level,
            "sdk_run_id": sdk_run_id, "sdk_thread_id": sdk_thread_id,
            "usage": terminal.cost, "cost": terminal.cost})
        return response

    def _failure(self, request: RuntimeRequest, thread_id: str, code: str, *, safe: bool) -> RuntimeResponse:
        response = RuntimeResponse.failed(request.run_id, code, "a13n Harness could not complete the turn.",
                                          retryable=safe, runtime=self.provider_name)
        response.resume.update(thread_id=thread_id, fallback_safe=safe, reconciliation_required=not safe)
        return response

    def _approval(self, request: RuntimeRequest, thread_id: str, sdk_run_id: str,
                  pending: list[dict[str, Any]]) -> RuntimeResponse:
        return RuntimeResponse(run_id=request.run_id, runtime=self.provider_name, status="needs_confirmation",
            resume={"resumable": False, "resume_token": "", "thread_id": thread_id,
                    "sdk_run_id": sdk_run_id, "fallback_safe": False, "pending_approvals": pending})

    @staticmethod
    async def _send(process: Any, packet: dict[str, Any]) -> None:
        raw = (json.dumps(packet, ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_LINE_BYTES:
            raise WorkerProtocolError("a13n_protocol_error")
        process.stdin.write(raw)
        await process.stdin.drain()

    @staticmethod
    async def _drain_stderr(process: Any) -> None:
        # Discard, rather than retaining possibly sensitive SDK exception text.
        while await process.stderr.read(8192):
            pass

    @staticmethod
    async def _stop(process: Any) -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    @staticmethod
    def _usage(raw: Any, calls: int) -> RuntimeUsage:
        data = raw if isinstance(raw, dict) else {}
        return RuntimeUsage(**{name: value if isinstance(value := data.get(name), int)
                               and 0 <= value < 10**12 else 0
                               for name in ("input_tokens", "output_tokens", "api_calls")},
                            tool_calls=calls)
