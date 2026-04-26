from types import SimpleNamespace

from app.services.base_agent import build_unknown_tool_result


def test_wecom_kf_feishu_tool_unavailable_is_capability_boundary():
    tenant = SimpleNamespace(tenant_id="kf-demo", platform="wecom_kf")

    result = build_unknown_tool_result(
        "send_feishu_message",
        tenant=tenant,
        available_tool_names={"set_reminder"},
        platform="wecom_kf",
    )

    assert not result.ok
    assert result.code == "tool_unavailable"
    assert "平台能力边界" in result.content
    assert "不是工具系统故障" in result.content
    assert "工具系统锁死" not in result.content


def test_internal_github_tool_unavailable_hides_ops_from_customer():
    tenant = SimpleNamespace(tenant_id="kf-demo", platform="wecom_kf")

    result = build_unknown_tool_result(
        "create_pull_request",
        tenant=tenant,
        available_tool_names=set(),
        platform="wecom_kf",
    )

    assert not result.ok
    assert result.code == "tool_unavailable"
    assert "内部工程操作" in result.content
    assert "不要向客户暴露" in result.content
    assert "分支" in result.content
