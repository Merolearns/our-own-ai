"""Google OAuth sign-in (Authlib) for real Scoopy Doog customers.

Configured only when GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET
are set in the environment (VPS .env, never the repo). When they are absent
this module does nothing and logins stay PBKDF2 password accounts — the
build never blocks on the OAuth client existing. Creating the client is a
parent-side step in Google Cloud Console (~10 minutes); see
images/BUILD_REPORT_SCOOPY.md for the exact steps.
"""

import os

import store

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"


def is_configured():
    return bool(
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    )


def build_oauth(flask_app):
    """Register the Google client on the Flask app, or None when the client
    credentials are not set. Registration alone makes no network call; the
    discovery document is fetched lazily at the first sign-in."""
    if not is_configured():
        return None
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(flask_app)
    oauth.register(
        "google",
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def find_or_create_user(db_path, userinfo):
    """Map a verified Google identity to a local account.

    The Google subject id is the stable key. A verified email that already
    has a password account links to it, so a customer who registered with a
    password keeps one account. An unverified email never links or creates
    an account — Google only asserts ownership of verified addresses.
    """
    sub = userinfo.get("sub")
    email = (userinfo.get("email") or "").strip().lower()
    if not sub or not email:
        raise ValueError("Google did not return an id and email.")
    if not userinfo.get("email_verified", False):
        raise ValueError("Google email is not verified.")
    user = store.get_user_by_google_sub(db_path, sub)
    if user:
        return user
    existing = store.get_user(db_path, email)
    if existing:
        if not store.set_google_sub(db_path, existing["id"], sub):
            raise ValueError("That Google account is already linked elsewhere.")
        return store.get_user(db_path, email)
    user_id = store.create_user(db_path, email, "", "customer", google_sub=sub)
    return store.get_user_by_id(db_path, user_id)
