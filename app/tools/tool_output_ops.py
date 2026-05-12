"""Tools for reading stored tool outputs."""

from __future__ import annotations

from app.harness.tool_output_ledger import ToolOutputLedger, get_tool_output_ledger as _get_tool_output_ledger
from app.tenant.context import get_current_tenant
from app.tools.tool_result import ToolResult


TOOL_MANIFEST = {"group": "core", "platforms": ["all"]}


def get_tool_output_ledger() -> ToolOutputLedger:
    return _get_tool_output_ledger()


def read_tool_output(args: dict) -> ToolResult:
    output_id = str((args or {}).get("output_id", "")).strip()
    if not output_id:
        return ToolResult.invalid_param(
            "output_id is required",
            retry_hint="Pass the output_id returned in the prior tool output preview.",
        )

    tenant_id = str(getattr(get_current_tenant(), "tenant_id", "") or "").strip()
    if not tenant_id:
        return ToolResult.error("current tenant is unavailable", code="tenant_unavailable")

    record = get_tool_output_ledger().read(tenant_id, output_id)
    if record is None:
        return ToolResult.not_found("tool output not found or expired")
    return ToolResult.success(record.content)


TOOL_DEFINITIONS = [
    {
        "name": "read_tool_output",
        "description": "Read a full stored tool output by output_id for the current tenant.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_id": {
                    "type": "string",
                    "description": "The output_id from a prior tool output preview.",
                },
            },
            "required": ["output_id"],
        },
    },
]


TOOL_MAP = {
    "read_tool_output": read_tool_output,
}
