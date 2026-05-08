from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect, sync_playwright


DASHBOARD_HTML = Path("app/admin/dashboard.html")


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


@pytest.fixture()
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        yield page
        browser.close()


def _install_dashboard_routes(page: Page, overrides: dict[str, dict] | None = None, seen: list[str] | None = None) -> None:
    overrides = overrides or {}

    defaults: dict[str, dict] = {
        "/admin/api/tenants": {
            "tenants": [
                {
                    "tenant_id": "kf-steven-ai",
                    "name": "吴天骄微信AI版",
                    "platform": "wecom_kf",
                }
            ]
        },
        "/admin/api/usage": {"tenants": {}},
        "/admin/api/instances": {
            "instances": [
                {
                    "tenant_id": "kf-steven-ai",
                    "name": "吴天骄微信AI版",
                    "platform": "wecom_kf",
                    "port": 8103,
                    "status": "running",
                    "container_name": "bot-kf-steven-ai",
                    "co_tenants": [],
                }
            ]
        },
        "/admin/api/superadmin": {"name": "Steven", "identities": []},
        "/admin/api/identities": {"identities": [], "channels": []},
        "/admin/api/leadgen/projects": {"projects": []},
        "/admin/api/module-registry": {"modules": []},
        "/admin/api/provision-requests": {"requests": []},
    }

    def handle(route: Route) -> None:
        url = route.request.url
        parsed = urlparse(url)
        path = parsed.path
        if seen is not None:
            seen.append(f"{route.request.method} {url}")
        if path == "/admin/dashboard":
            route.fulfill(status=200, content_type="text/html", body=DASHBOARD_HTML.read_text(encoding="utf-8"))
            return
        if path.endswith(".css"):
            route.fulfill(status=200, content_type="text/css", body="")
            return
        if path.endswith("/approve") or path.endswith("/reject"):
            route.fulfill(status=200, content_type="application/json", body=_json({"ok": True, "provision_result": {"ok": True, "port": 9001}}))
            return
        if route.request.method == "DELETE":
            route.fulfill(status=200, content_type="application/json", body=_json({"ok": True}))
            return
        if path.startswith("/admin/api/instances/") and path.endswith("/logs"):
            delay_ms = int(overrides.get("/admin/api/instance-logs-delay-ms", 0))
            if delay_ms:
                page.wait_for_timeout(delay_ms)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=_json(
                    overrides.get(
                        "/admin/api/instance-logs",
                        {
                            "logs": "2026-05-01 [INFO] ok\n",
                            "total_lines": 1,
                            "tenant_id": "kf-steven-ai",
                            "error_count": 0,
                            "recent_errors": "",
                        },
                    )
                ),
            )
            return
        if path.startswith("/admin/api/trial/") and path.endswith("/users"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=_json(overrides.get("/admin/api/trial-users", {"users": [], "total": 0, "trial_duration_hours": 48})),
            )
            return
        if path.startswith("/admin/api/instances/") and path.endswith("/kf-accounts"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=_json(overrides.get("/admin/api/kf-accounts", {"accounts": []})),
            )
            return
        if path.startswith("/admin/api/identities/"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=_json(overrides.get("/admin/api/tenant-identities", {"identities": []})),
            )
            return
        if path.startswith("/admin/api/tenants/") and path.endswith("/channels"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=_json(overrides.get("/admin/api/tenant-channels", {"channels": []})),
            )
            return
        payload = overrides.get(path) or defaults.get(path)
        if payload is not None:
            if path == "/admin/api/instances":
                delay_ms = int(overrides.get("/admin/api/instances-delay-ms", 0))
                if delay_ms:
                    page.wait_for_timeout(delay_ms)
            route.fulfill(status=200, content_type="application/json", body=_json(payload))
            return
        route.fulfill(status=404, content_type="application/json", body=_json({"detail": f"unhandled {path}"}))

    page.route("**/*", handle)


def _open_dashboard(page: Page, overrides: dict[str, dict] | None = None, seen: list[str] | None = None) -> None:
    _install_dashboard_routes(page, overrides=overrides, seen=seen)
    page.goto("http://dashboard.test/admin/dashboard")
    page.evaluate("TOKEN = 'test-token'")


def _accept_dialogs(page: Page, responses: list[str | None]) -> None:
    queue = list(responses)

    def handler(dialog):
        response = queue.pop(0) if queue else None
        if response is None:
            dialog.accept()
        else:
            dialog.accept(response)

    page.on("dialog", handler)


def test_dashboard_has_no_inline_event_handlers(page: Page):
    _open_dashboard(page)

    assert page.locator("[onclick], [onchange], [onkeydown], [onblur]").count() == 0


def test_provision_requests_render_untrusted_fields_as_text_and_encode_actions(page: Page):
    request_id = "req_1');alert(1)//"
    seen: list[str] = []
    _open_dashboard(
        page,
        seen=seen,
        overrides={
            "/admin/api/provision-requests": {
                "requests": [
                    {
                        "request_id": request_id,
                        "requester_name": '<img src=x onerror="window.__xss=1">',
                        "name": "<script>window.__xss=1</script>",
                        "platform": 'wecom_kf" autofocus onfocus="window.__xss=1',
                        "status": "pending",
                    }
                ]
            }
        },
    )
    page.on("dialog", lambda dialog: dialog.accept())

    page.evaluate("switchTab('superadmin')")
    expect(page.locator("#requests-tbody")).to_contain_text("<script>window.__xss=1</script>")

    assert page.locator("#requests-tbody script, #requests-tbody img, #requests-tbody [onerror]").count() == 0
    assert page.evaluate("window.__xss") is None

    page.get_by_role("button", name="Approve", exact=True).click()

    assert any(
        "/admin/api/provision-requests/req_1')%3Balert(1)%2F%2F/approve" in url
        for url in seen
    )


def test_logs_render_untrusted_content_as_text_while_highlighting_levels(page: Page):
    _open_dashboard(
        page,
        overrides={
            "/admin/api/instance-logs": {
                "logs": '<img src=x onerror="window.__xss=1"> [ERROR] boom',
                "total_lines": 1,
                "tenant_id": "kf-steven-ai",
                "error_count": 1,
                "recent_errors": '<script>window.__xss=1</script>',
            }
        },
    )

    page.evaluate("switchTab('logs')")
    expect(page.locator("#logs-output")).to_contain_text('<img src=x onerror="window.__xss=1"> [ERROR] boom')

    assert page.locator("#logs-output img, #logs-output script, #logs-output [onerror]").count() == 0
    assert page.locator("#logs-output .text-red-400").count() >= 1
    assert page.evaluate("window.__xss") is None


def test_dynamic_dashboard_tables_render_untrusted_fields_as_text(page: Page):
    payload = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>'
    _open_dashboard(
        page,
        overrides={
            "/admin/api/tenants": {
                "tenants": [
                    {
                        "tenant_id": "kf-steven-ai",
                        "name": payload,
                        "platform": "wecom_kf",
                        "trial_enabled": True,
                    }
                ]
            },
            "/admin/api/instances": {
                "instances": [
                    {
                        "tenant_id": "kf-steven-ai",
                        "name": payload,
                        "platform": "wecom_kf",
                        "port": 8103,
                        "status": "running",
                        "co_tenants": [
                            {
                                "tenant_id": "co-tenant",
                                "name": payload,
                                "wecom_kf_open_kfid": payload,
                            }
                        ],
                    }
                ]
            },
            "/admin/api/trial-users": {
                "users": [
                    {
                        "user_id": 'user" onmouseover="window.__xss=1',
                        "display_name": payload,
                        "status": "trial",
                        "notes": payload,
                    }
                ],
                "total": 1,
                "trial_duration_hours": 48,
            },
            "/admin/api/kf-accounts": {
                "accounts": [
                    {
                        "open_kfid": payload,
                        "name": payload,
                        "avatar": 'x" onerror="window.__xss=1',
                    }
                ]
            },
            "/admin/api/tenant-identities": {
                "identities": [
                    {
                        "identity_id": "ident-1",
                        "name": payload,
                        "linked_platforms": {"wecom_kf": payload},
                        "created_at": payload,
                    }
                ]
            },
            "/admin/api/tenant-channels": {
                "channels": [
                    {
                        "channel_id": payload,
                        "platform": payload,
                        "enabled": True,
                        "has_wecom_kf": True,
                    }
                ]
            },
        },
    )

    page.evaluate("loadOverview()")
    expect(page.locator("#tenant-cards")).to_contain_text(payload)
    assert page.locator("#tenant-cards script, #tenant-cards [onerror]").count() == 0

    page.evaluate("switchTab('users')")
    expect(page.locator("#users-tbody")).to_contain_text(payload)
    assert page.locator("#users-tbody script, #users-tbody [onerror]").count() == 0

    page.evaluate("switchTab('instances')")
    expect(page.locator("#instances-tbody")).to_contain_text(payload)
    assert page.locator("#instances-tbody script, #instances-tbody [onerror]").count() == 0

    page.get_by_role("button", name="KF Bots").click()
    expect(page.locator("#kf-accounts-body")).to_contain_text(payload)
    assert page.locator("#kf-accounts-body script, #kf-accounts-body [onerror]").count() == 0

    page.evaluate("switchTab('identities')")
    expect(page.locator("#identities-tbody")).to_contain_text(payload)
    expect(page.locator("#channels-tbody")).to_contain_text(payload)
    assert page.locator("#identities-tbody script, #identities-tbody [onerror]").count() == 0
    assert page.locator("#channels-tbody script, #channels-tbody [onerror]").count() == 0
    assert page.evaluate("window.__xss") is None


def test_logs_auto_refresh_stops_when_leaving_logs_tab(page: Page):
    page.add_init_script(
        """
        window.__intervals = [];
        window.__clearedIntervals = [];
        window.setInterval = (fn, ms) => {
          const id = 1000 + window.__intervals.length;
          window.__intervals.push({ id, ms });
          return id;
        };
        window.clearInterval = id => window.__clearedIntervals.push(id);
        """
    )
    _open_dashboard(page)

    page.evaluate("switchTab('logs')")
    page.wait_for_function("window.__intervals.length > 0")
    first_interval = page.evaluate("window.__intervals[window.__intervals.length - 1].id")

    page.evaluate("switchTab('overview')")

    assert first_interval in page.evaluate("window.__clearedIntervals")


def test_in_flight_logs_request_does_not_restart_auto_refresh_after_leaving_logs(page: Page):
    page.add_init_script(
        """
        window.__intervals = [];
        window.__clearedIntervals = [];
        window.setInterval = (fn, ms) => {
          const id = 2000 + window.__intervals.length;
          window.__intervals.push({ id, ms });
          return id;
        };
        window.clearInterval = id => window.__clearedIntervals.push(id);
        """
    )
    _open_dashboard(page, overrides={"/admin/api/instance-logs-delay-ms": 300})

    page.evaluate("switchTab('logs')")
    page.wait_for_timeout(50)
    page.evaluate("switchTab('overview')")
    page.wait_for_timeout(500)

    assert page.evaluate("window.__intervals.length") == 0


def test_leaving_logs_before_instance_list_returns_does_not_fetch_logs(page: Page):
    seen: list[str] = []
    _open_dashboard(page, overrides={"/admin/api/instances-delay-ms": 300}, seen=seen)

    page.evaluate("switchTab('logs')")
    page.wait_for_timeout(50)
    page.evaluate("switchTab('overview')")
    page.wait_for_timeout(500)

    assert not any("/admin/api/instances/kf-steven-ai/logs" in request for request in seen)


def test_destroy_instance_requires_exact_tenant_prompt_before_delete(page: Page):
    seen: list[str] = []
    _open_dashboard(page, seen=seen)

    _accept_dialogs(page, [None, "wrong-tenant", None, "kf-steven-ai"])
    page.evaluate("destroyInstance('kf-steven-ai')")
    assert not any("/admin/api/instances/kf-steven-ai" in url and "DELETE" in url for url in seen)

    page.evaluate("destroyInstance('kf-steven-ai')")
    assert any("/admin/api/instances/kf-steven-ai" in url and "DELETE" in url for url in seen)
