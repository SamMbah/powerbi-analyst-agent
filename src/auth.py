"""
Azure AD authentication using MSAL device code flow.

Split into two stages so the browser never hangs:
  1. initiate_device_flow() → returns user_code + verification_uri immediately
  2. complete_device_flow()  → polls Azure until the user logs in (runs in background)

get_access_token() handles the silent refresh path for subsequent calls.
"""

import os
import msal


SCOPES    = ["https://analysis.windows.net/powerbi/api/.default"]
CACHE_FILE = os.path.expanduser("~/.powerbi_agent_token_cache.json")

# Module-level storage for the in-progress flow so the poll endpoint can access it
_pending_flow: dict | None = None
_pending_app:  msal.PublicClientApplication | None = None
_pending_cache: msal.SerializableTokenCache | None = None


def _build_app() -> tuple:
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            cache.deserialize(f.read())
    app = msal.PublicClientApplication(
        client_id=os.getenv("AZURE_CLIENT_ID"),
        authority=f"https://login.microsoftonline.com/{os.getenv('AZURE_TENANT_ID')}",
        token_cache=cache,
    )
    return app, cache


def _save_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        with open(CACHE_FILE, "w") as f:
            f.write(cache.serialize())


def is_authenticated() -> bool:
    """Return True if a valid cached token exists."""
    if not os.path.exists(CACHE_FILE):
        return False
    app, cache = _build_app()
    accounts = app.get_accounts()
    if not accounts:
        return False
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    return bool(result and "access_token" in result)


def start_device_flow() -> dict:
    """
    Stage 1 — initiate the flow and return the login code immediately.
    Returns: { "user_code": "ABCD-1234", "verification_uri": "https://...", "message": "..." }
    This returns in < 1 second so the browser never hangs.
    """
    global _pending_flow, _pending_app, _pending_cache

    app, cache = _build_app()
    flow = app.initiate_device_flow(scopes=SCOPES)

    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")

    _pending_flow  = flow
    _pending_app   = app
    _pending_cache = cache

    return {
        "user_code":        flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "message":          flow["message"],
    }


def poll_device_flow() -> str:
    """
    Stage 2 — blocks until the user completes login, then caches the token.
    Called from a background thread / SSE generator so the browser streams progress.
    Returns the access token on success, raises on failure.
    """
    global _pending_flow, _pending_app, _pending_cache

    if not _pending_flow or not _pending_app:
        raise RuntimeError("No pending device flow. Call start_device_flow() first.")

    result = _pending_app.acquire_token_by_device_flow(_pending_flow)

    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "Authentication failed"))

    _save_cache(_pending_cache)
    _pending_flow = _pending_app = _pending_cache = None

    return result["access_token"]


def get_access_token() -> str:
    """Return a valid token from cache (silent refresh). Raises if not authenticated."""
    app, cache = _build_app()
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]
    raise RuntimeError("Not authenticated. Complete device flow first.")
