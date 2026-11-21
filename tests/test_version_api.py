"""GET /api/version: auth required; serves the build identity."""

import requests
from conftest import TOKEN

import version as ver_mod


def test_version_env_overrides(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    monkeypatch.setenv("GIT_SHA", "abc1234")
    assert ver_mod.info() == {"version": "9.9.9", "commit": "abc1234"}


def test_version_falls_back_to_version_txt(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    # Repo default until release-please bumps version.txt with the first release.
    assert ver_mod.version() == "0.1.0"


def test_version_requires_auth(server):
    r = requests.get(server + "/api/version", timeout=20)
    assert r.status_code == 401


def test_version_reports_version_and_commit(server):
    r = requests.get(
        server + "/api/version",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=20,
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"version", "commit"}
    assert body["version"] == "0.1.0"  # outside container builds: version.txt
    assert body["commit"]  # 'unknown' when GIT_SHA build arg was not set
