"""BEARER_TOKEN must default to EMPTY, never to a published value.

A code default of "change-me" silently enables legacy shared-token auth with
a publicly known secret for anyone who runs the image without setting the
variable. An empty value disables that auth path (auth._eq requires both
sides non-empty), which is the secure default; personal tokens (cm_...) or
ADMIN_TOKEN are the supported ways in.
"""

import requests

from config import Settings


def test_bearer_token_code_default_is_empty():
    assert Settings.model_fields["bearer_token"].default == ""


def test_empty_bearer_token_rejects_any_token(server, settings_override):
    settings_override(bearer_token="", admin_token="")
    for tok in ("change-me", "anything-goes"):
        r = requests.get(
            server + "/api/memories",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=20,
        )
        assert r.status_code == 401, (
            f"token {tok!r} was accepted with empty BEARER_TOKEN"
        )
