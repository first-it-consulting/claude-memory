"""GET /api/version: auth required; serves the build identity."""

import re

import requests
from conftest import TOKEN

import version as ver_mod


def test_version_env_overrides(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    monkeypatch.setenv("GIT_SHA", "abc1234")
    assert ver_mod.info() == {"version": "9.9.9", "commit": "abc1234"}


def test_version_falls_back_to_version_txt(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    # release-please bumps version.txt every release - assert the fallback
    # path yields a version, never a pinned literal (pinned here it broke on
    # the 0.1.1 bump).
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver_mod.version())


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
    # outside container builds the endpoint serves version.txt - compare
    # against the module, not a literal that release-please bumps.
    assert body["version"] == ver_mod.version()
    assert body["commit"]  # 'unknown' when GIT_SHA build arg was not set
