from pathlib import Path


def test_dashboard_has_readonly_memory_tab_and_safe_api_urls():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "switchTab('memory')" in dashboard
    assert 'id="panel-memory"' in dashboard
    assert "loadMemory()" in dashboard
    assert "/tenants/${encodeURIComponent(tid)}/memory?" in dashboard
    assert "/tenants/${encodeURIComponent(tid)}/memory/recall-preview?" in dashboard
    assert "escapeHtml(String(entry.summary || ''))" in dashboard
    assert "escapeHtml(String(user.name || user.user_id || '-'))" in dashboard
    assert "memoryDelete" not in dashboard
    assert "deleteMemory" not in dashboard
