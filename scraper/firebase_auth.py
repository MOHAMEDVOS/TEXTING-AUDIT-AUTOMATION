"""
Firebase REST Authentication for SmarterContact HTTP client.

Handles sign-in, token refresh, and auto-expiry management.
No browser required — pure HTTP via Firebase REST API.
"""
import time
import logging
import httpx
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FIREBASE_API_KEY      = "AIzaSyApHXR2TXACsV0X0vZPVROvW6YZL9ylW38"
SIGN_IN_URL           = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
REFRESH_URL           = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
TOKEN_BUFFER_SECONDS  = 120   # refresh 2 min before expiry


@dataclass
class AuthSession:
    """Holds a Firebase JWT + refresh token with auto-refresh support."""
    id_token:      str
    refresh_token: str
    expires_at:    float  # unix timestamp

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - TOKEN_BUFFER_SECONDS)

    async def ensure_fresh(self, client: httpx.AsyncClient) -> str:
        """Return a valid id_token, refreshing if needed."""
        if self.is_expired:
            logger.debug("Firebase token near expiry — refreshing...")
            resp = await client.post(REFRESH_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            })
            data = resp.json()
            if "id_token" not in data:
                raise RuntimeError(f"Token refresh failed: {data}")
            self.id_token      = data["id_token"]
            self.refresh_token = data["refresh_token"]
            self.expires_at    = time.time() + int(data.get("expires_in", 3600))
            logger.debug("Firebase token refreshed successfully")
        return self.id_token


async def firebase_sign_in(email: str, password: str, client: httpx.AsyncClient) -> AuthSession:
    """
    Sign in with email/password via Firebase REST API.
    Returns an AuthSession with id_token, refresh_token, and expiry.
    """
    resp = await client.post(SIGN_IN_URL, json={
        "email": email,
        "password": password,
        "returnSecureToken": True,
    })
    data = resp.json()

    if "idToken" not in data:
        error = data.get("error", {}).get("message", "Unknown error")
        raise RuntimeError(f"Firebase sign-in failed for {email}: {error}")

    expires_in = int(data.get("expiresIn", 3600))
    session = AuthSession(
        id_token=data["idToken"],
        refresh_token=data["refreshToken"],
        expires_at=time.time() + expires_in,
    )
    logger.info(f"Firebase sign-in OK: {email} (token valid {expires_in}s)")
    return session
