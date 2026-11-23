import pytest
from fastmcp.server.auth.auth import AccessToken

from nh_mcp.auth.access import AccessPolicy, Identity, identity_of

CLOSED = AccessPolicy(open_signup=False, admin_emails=frozenset({"admin@example.com"}))
OPEN = AccessPolicy(open_signup=True, admin_emails=frozenset())


def token(**claims: object) -> AccessToken:
    return AccessToken(token="t", client_id="c", scopes=[], claims=dict(claims))


@pytest.mark.parametrize(
    ("policy", "identity", "allowed"),
    [
        (CLOSED, Identity("s", "admin@example.com", True), True),
        (CLOSED, Identity("s", "ADMIN@example.com", True), True),
        (CLOSED, Identity("s", "other@example.com", True), False),
        (CLOSED, Identity("s", "admin@example.com", False), False),
        (OPEN, Identity("s", "anyone@example.com", True), True),
        (OPEN, Identity("s", "anyone@example.com", False), False),
    ],
)
def test_policy(policy: AccessPolicy, identity: Identity, allowed: bool) -> None:
    assert policy.allows(identity) is allowed


def test_identity_from_google_claims() -> None:
    assert identity_of(token(sub="g1", email="a@example.com", email_verified=True)) == Identity(
        "g1", "a@example.com", True
    )
    assert identity_of(token(sub="g1", email="a@example.com", email_verified="true")) == Identity(
        "g1", "a@example.com", True
    )


@pytest.mark.parametrize(
    "claims",
    [{}, {"sub": "g1"}, {"email": "a@example.com"}, {"sub": "", "email": "a@example.com"}],
)
def test_missing_identity_is_unknown(claims: dict[str, object]) -> None:
    assert identity_of(token(**claims)) is None
    assert identity_of(None) is None
