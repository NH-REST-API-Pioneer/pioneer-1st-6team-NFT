"""운영 서버의 로그인. Google 계정만 받는다."""

from fastmcp.server.auth.providers.google import GoogleProvider

from nh_mcp.config import Settings

SCOPES = ["openid", "email"]


def google_provider(settings: Settings) -> GoogleProvider:
    return GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=settings.public_base_url,
        required_scopes=SCOPES,
    )
