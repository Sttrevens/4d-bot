from pathlib import Path


def test_dashboard_logs_uses_abort_controller_and_always_resets_loading():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "let logsAbortController" in dashboard
    assert "new AbortController()" in dashboard
    assert "signal: controller.signal" in dashboard
    assert "force_refresh=1" in dashboard
    assert "logsLoading = false;" in dashboard
    assert "logsAbortController = null;" in dashboard


def test_dashboard_stops_log_polling_when_leaving_logs_tab():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "function stopLogsAutoRefresh()" in dashboard
    assert "if (tab !== 'logs') stopLogsAutoRefresh();" in dashboard


def test_dashboard_escapes_provision_request_rows():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "requestIdCell.textContent = String(r.request_id || '')" in dashboard
    assert "requesterCell.textContent = String(r.requester_name || r.requester_id?.slice(0, 12) || '-')" in dashboard
    assert "nameCell.textContent = String(r.name || '-')" in dashboard
    assert "platformCell.textContent = String(r.platform || '-')" in dashboard
    assert "Object.prototype.hasOwnProperty.call(statusColors, rawStatus)" in dashboard
    assert "approveReqByIndex" in dashboard
    assert "/provision-requests/${encodeURIComponent(id)}/approve" in dashboard
    assert "/provision-requests/${encodeURIComponent(id)}/reject" in dashboard
    assert "approveReq('${r.request_id}')" not in dashboard


def test_dashboard_destroy_requires_exact_tenant_id_prompt():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "const confirmTenant = prompt(" in dashboard
    assert "confirmTenant !== tenantId" in dashboard


def test_dashboard_uses_delegated_actions_for_dynamic_buttons():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "function runDashboardAction(action, target, event)" in dashboard
    assert 'data-action="open-edit-modal"' in dashboard
    assert 'data-action="approve-user"' in dashboard
    assert 'data-action="user-action"' in dashboard
    assert "instanceAction('restart','${inst.tenant_id}')" not in dashboard
    assert "startAddCoTenant('${a.open_kfid}'" not in dashboard
    assert "unlinkIdentity('${tid}'" not in dashboard


def test_dashboard_escapes_dynamic_attribute_and_table_values():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "function escapeAttr(s)" in dashboard
    assert 'value="${escapeAttr(String(i.tenant_id || \'\'))}"' in dashboard
    assert 'title="${escapeAttr(uid)}"' in dashboard
    assert 'src="${escapeAttr(avatar)}"' in dashboard
    assert "${escapeHtml(String(ct.tenant_id || ''))}" in dashboard


def test_dashboard_dynamic_rendering_does_not_use_inline_handlers_or_inner_html():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "onclick=" not in dashboard
    assert "onchange=" not in dashboard
    assert "onkeydown=" not in dashboard
    assert "onblur=" not in dashboard
    assert "onsubmit=" not in dashboard
    assert ".innerHTML" not in dashboard
    assert "insertAdjacentHTML" not in dashboard


def test_dashboard_log_colorizer_uses_text_nodes_not_markup_fragments():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "function colorizeLogs(pre)" in dashboard
    assert "document.createTextNode" in dashboard
    assert "document.createElement('span')" in dashboard
    assert "span.textContent = match[0];" in dashboard
    assert "setTrustedMarkup(pre" not in dashboard


def test_dashboard_provision_requests_render_with_dom_nodes():
    dashboard = Path("app/admin/dashboard.html").read_text(encoding="utf-8")

    assert "function renderProvisionRequestRows(tbody, reqs)" in dashboard
    assert "document.createElement('tr')" in dashboard
    assert "requestIdCell.textContent = String(r.request_id || '')" in dashboard
    assert "setTrustedMarkup(tbody, reqs.map" not in dashboard
    assert "function setMarkup" not in dashboard
