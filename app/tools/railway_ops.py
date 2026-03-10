"""Railway 部署平台工具 —— 查看部署日志、状态，触发重新部署

需要配置环境变量：
- RAILWAY_API_TOKEN: Railway API token（从 dashboard → Settings → Tokens 获取）
- RAILWAY_PROJECT_ID: 项目 ID（服务页面 → Variables → Railway 提供的变量）
- RAILWAY_SERVICE_ID: 服务 ID
- RAILWAY_ENVIRONMENT_ID: 环境 ID（可选，留空取 production）
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.railway.api_token}",
        "Content-Type": "application/json",
    }


def _gql(query: str, variables: dict | None = None) -> dict | str:
    """执行 Railway GraphQL 查询"""
    if not settings.railway.api_token:
        return "[ERROR] Railway API 未配置（缺少 RAILWAY_API_TOKEN 环境变量）"

    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(_GRAPHQL_URL, headers=_headers(), json=payload)
        if resp.status_code >= 400:
            return f"[ERROR] Railway API {resp.status_code}: {resp.text[:500]}"
        data = resp.json()
        if "errors" in data:
            errs = "; ".join(e.get("message", str(e)) for e in data["errors"])
            return f"[ERROR] Railway GraphQL: {errs}"
        return data.get("data", {})
    except Exception as exc:
        logger.exception("Railway API request failed")
        return f"[ERROR] Railway API 请求失败: {exc}"


def get_deploy_status(count: int = 5) -> ToolResult:
    """查看最近几次部署的状态"""
    if not settings.railway.project_id or not settings.railway.service_id:
        return ToolResult.error("Railway 未配置（缺少 RAILWAY_PROJECT_ID 或 RAILWAY_SERVICE_ID）", code="api_error")

    query = """
    query deployments($input: DeploymentListInput!, $first: Int) {
      deployments(input: $input, first: $first) {
        edges {
          node {
            id
            status
            createdAt
            url
            staticUrl
          }
        }
      }
    }
    """
    variables: dict = {
        "input": {
            "projectId": settings.railway.project_id,
            "serviceId": settings.railway.service_id,
        },
        "first": count,
    }
    if settings.railway.environment_id:
        variables["input"]["environmentId"] = settings.railway.environment_id

    result = _gql(query, variables)
    if isinstance(result, str):
        return ToolResult.api_error(result)

    edges = result.get("deployments", {}).get("edges", [])
    if not edges:
        return ToolResult.success("没有找到部署记录。")

    lines = []
    for edge in edges:
        node = edge["node"]
        status = node.get("status", "?")
        created = node.get("createdAt", "?")[:19]
        deploy_id = node.get("id", "?")
        url = node.get("staticUrl") or node.get("url") or ""
        line = f"[{status}] {created} (id: {deploy_id[:12]}...)"
        if url:
            line += f" → {url}"
        lines.append(line)
    return ToolResult.success("\n".join(lines))


def get_deploy_logs(num_lines: int = 100) -> ToolResult:
    """获取最新部署的运行日志（用于排查错误）"""
    if not settings.railway.project_id or not settings.railway.service_id:
        return ToolResult.error("Railway 未配置（缺少 RAILWAY_PROJECT_ID 或 RAILWAY_SERVICE_ID）", code="api_error")

    # 第一步：获取最新部署的 ID
    deploy_query = """
    query deployments($input: DeploymentListInput!, $first: Int) {
      deployments(input: $input, first: $first) {
        edges {
          node {
            id
            status
          }
        }
      }
    }
    """
    variables: dict = {
        "input": {
            "projectId": settings.railway.project_id,
            "serviceId": settings.railway.service_id,
        },
        "first": 1,
    }
    if settings.railway.environment_id:
        variables["input"]["environmentId"] = settings.railway.environment_id

    result = _gql(deploy_query, variables)
    if isinstance(result, str):
        return ToolResult.api_error(result)

    edges = result.get("deployments", {}).get("edges", [])
    if not edges:
        return ToolResult.success("没有找到部署记录，无法获取日志。")

    deploy_id = edges[0]["node"]["id"]
    deploy_status = edges[0]["node"].get("status", "?")

    # 第二步：获取该部署的日志
    log_query = """
    query deploymentLogs($deploymentId: String!, $limit: Int) {
      deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
        timestamp
        message
        severity
      }
    }
    """
    log_result = _gql(log_query, {
        "deploymentId": deploy_id,
        "limit": num_lines,
    })
    if isinstance(log_result, str):
        return ToolResult.api_error(log_result)

    logs = log_result.get("deploymentLogs", [])
    if not logs:
        return ToolResult.success(f"部署 {deploy_id[:12]}... (status={deploy_status}) 暂无日志。")

    lines = [f"部署 {deploy_id[:12]}... (status={deploy_status}) 最近 {len(logs)} 条日志：\n"]
    for log in logs:
        ts = log.get("timestamp", "")[:19]
        severity = log.get("severity", "")
        msg = log.get("message", "")
        prefix = f"[{severity}]" if severity else ""
        lines.append(f"{ts} {prefix} {msg}")
    return ToolResult.success("\n".join(lines))


def redeploy() -> ToolResult:
    """触发最新部署的重新部署（代码修改后手动触发）"""
    if not settings.railway.project_id or not settings.railway.service_id:
        return ToolResult.error("Railway 未配置", code="api_error")

    # 获取最新部署 ID
    deploy_query = """
    query deployments($input: DeploymentListInput!, $first: Int) {
      deployments(input: $input, first: $first) {
        edges {
          node { id }
        }
      }
    }
    """
    variables: dict = {
        "input": {
            "projectId": settings.railway.project_id,
            "serviceId": settings.railway.service_id,
        },
        "first": 1,
    }
    if settings.railway.environment_id:
        variables["input"]["environmentId"] = settings.railway.environment_id

    result = _gql(deploy_query, variables)
    if isinstance(result, str):
        return ToolResult.api_error(result)

    edges = result.get("deployments", {}).get("edges", [])
    if not edges:
        return ToolResult.success("没有找到可重新部署的记录。")

    deploy_id = edges[0]["node"]["id"]

    # 触发 redeploy
    mutation = """
    mutation deploymentRedeploy($id: String!) {
      deploymentRedeploy(id: $id) {
        id
        status
      }
    }
    """
    redeploy_result = _gql(mutation, {"id": deploy_id})
    if isinstance(redeploy_result, str):
        return ToolResult.api_error(redeploy_result)

    new_deploy = redeploy_result.get("deploymentRedeploy", {})
    new_id = new_deploy.get("id", "?")
    new_status = new_deploy.get("status", "?")
    return ToolResult.success(f"已触发重新部署: id={new_id[:12]}... status={new_status}")


TOOL_DEFINITIONS = [
    {
        "name": "get_deploy_status",
        "description": "查看 bot 在 Railway 上最近几次部署的状态（成功/失败/进行中）。用于检查部署是否正常。",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "查看最近几次部署，默认5",
                    "default": 5,
                },
            },
        },
    },
    {
        "name": "get_deploy_logs",
        "description": "获取 bot 最新部署的运行日志。用于排查运行时错误、API 调用失败等问题。",
        "input_schema": {
            "type": "object",
            "properties": {
                "num_lines": {
                    "type": "integer",
                    "description": "获取多少条日志，默认100",
                    "default": 100,
                },
            },
        },
    },
    {
        "name": "redeploy",
        "description": "触发 Railway 重新部署。在修改自己的代码并推送到 main 后，如果 Railway 没有自动部署，可以手动触发。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

TOOL_MAP = {
    "get_deploy_status": lambda args: get_deploy_status(args.get("count", 5)),
    "get_deploy_logs": lambda args: get_deploy_logs(args.get("num_lines", 100)),
    "redeploy": lambda args: redeploy(),
}
