"""The explicit completion endpoint requests setup and reports safe errors."""
import importlib
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.browser import BrowserError


@pytest.fixture
def api_module(tmp_path, monkeypatch):
    monkeypatch.setenv("ELE_API_KEY", "test-api")
    monkeypatch.setenv("ELE_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("ELE_DATA_DIR", str(tmp_path / "unused-main"))
    module = importlib.import_module("app.main")
    monkeypatch.setattr(module, "ADMIN_TOKEN", "completion-admin")
    return module


def test_completion_endpoint_requires_admin_and_runs_the_setup_gate(api_module, monkeypatch):
    calls = []

    def check(account_id, **options):
        calls.append((account_id, options))
        return {"id": account_id, "status": "active", "enabled": True}

    monkeypatch.setattr(api_module, "service", SimpleNamespace(check_account=check))
    api = TestClient(api_module.app)
    assert api.post("/api/accounts/3/browser/complete").status_code == 401
    assert calls == []
    response = api.post("/api/accounts/3/browser/complete", headers={"X-Admin-Token": "completion-admin"})
    assert response.status_code == 200 and response.json()["enabled"]
    assert calls == [(3, {"complete_onboarding": True})]


@pytest.mark.parametrize("error,status", [(BrowserError("private account operation"), 409), (KeyError(3), 404)])
def test_completion_busy_or_missing_account_has_a_safe_http_error(api_module, monkeypatch, error, status):
    def check(*args, **kwargs):
        raise error

    monkeypatch.setattr(api_module, "service", SimpleNamespace(check_account=check))
    response = TestClient(api_module.app).post("/api/accounts/3/browser/complete", headers={"X-Admin-Token": "completion-admin"})
    assert response.status_code == status and "private" not in response.text
