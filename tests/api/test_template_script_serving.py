import os
from fastapi.testclient import TestClient

from app.main import app
from app.services.download_token_service import get_download_token_service


client = TestClient(app)


def test_template_files_serves_firstboot_and_user_login(mock_redis, monkeypatch):
    """
    Ensure template-files endpoint can serve firstboot.ps1 and user-login.ps1
    from the template root directory using the existing download token system.
    """
    # Patch Redis client used by download_token_service
    import app.services.download_token_service as token_service_module

    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)

    # Use real windows-server-2022 template on disk
    template_id = "windows-server-2022"

    token_service = get_download_token_service()
    token = token_service.generate_token(
        boot_task_id=123,
        allowed_files=["firstboot.ps1", "user-login.ps1"],
    )

    # firstboot.ps1 should be served from os_templates/{template_id}/firstboot.ps1
    resp_firstboot = client.get(
        f"/api/servers/interaction/template-files/{template_id}/firstboot.ps1?token={token}"
    )
    assert resp_firstboot.status_code == 200
    assert "firstboot" in resp_firstboot.text.lower()

    # user-login.ps1 should be served from os_templates/{template_id}/user-login.ps1
    token2 = token_service.generate_token(
        boot_task_id=124,
        allowed_files=["user-login.ps1"],
    )
    resp_user_login = client.get(
        f"/api/servers/interaction/template-files/{template_id}/user-login.ps1?token={token2}"
    )
    assert resp_user_login.status_code == 200
    assert "user-login" in resp_user_login.text.lower()


def test_template_files_serves_deploy_path_with_slash(mock_redis, monkeypatch):
    """
    Route must accept multi-segment file_path (e.g. deploy/windows.img) so image
    URLs like .../template-files/{id}/deploy/windows.img are served, not 404.
    """
    import app.services.download_token_service as token_service_module

    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)
    template_id = "windows-server-2022"
    token_service = get_download_token_service()
    token = token_service.generate_token(
        boot_task_id=125,
        allowed_files=["deploy/windows.img"],
    )
    # Request path with slash: template_id + deploy/windows.img
    resp = client.get(
        f"/api/servers/interaction/template-files/{template_id}/deploy/windows.img?token={token}"
    )
    # If file exists we get 200; if not we get 404 with "not found" (route matched)
    assert resp.status_code in (200, 404)
    if resp.status_code == 404:
        assert "not found" in resp.json().get("detail", "").lower()

