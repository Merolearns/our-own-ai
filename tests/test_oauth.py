"""Google OAuth sign-in: configuration, identity mapping, and routes.

The OAuth client is a parent-side step (Google Cloud Console); these tests
prove the code paths that do not need a real client: unconfigured means
password login only, and the callback maps a verified Google identity to a
local account with a mocked Authlib client.
"""

import pytest

import app as agent_app
import oauth
import store


def userinfo(sub="google-1", email="sam@example.com", verified=True):
    return {
        "sub": sub,
        "email": email,
        "email_verified": verified,
        "name": "Sam",
    }


def test_unconfigured_without_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert oauth.is_configured() is False


def test_unconfigured_with_only_one_var(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert oauth.is_configured() is False


def test_configured_registers_google(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    assert oauth.is_configured() is True
    from flask import Flask

    client = oauth.build_oauth(Flask(__name__))
    assert client is not None and hasattr(client, "google")


def test_build_oauth_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    from flask import Flask

    assert oauth.build_oauth(Flask(__name__)) is None


def test_find_or_create_user_creates_oauth_only_account(db):
    user = oauth.find_or_create_user(db, userinfo())
    assert user["username"] == "sam@example.com"
    assert user["google_sub"] == "google-1" and user["role"] == "customer"
    # An OAuth-only account has no password: it can never sign in by password.
    assert user["password_hash"] == ""
    again = oauth.find_or_create_user(db, userinfo())
    assert again["id"] == user["id"]


def test_find_or_create_user_links_existing_password_account(db):
    user_id = store.create_user(db, "sam@example.com", "hash", "customer")
    user = oauth.find_or_create_user(db, userinfo())
    assert user["id"] == user_id and user["google_sub"] == "google-1"
    assert store.get_user_by_google_sub(db, "google-1")["id"] == user_id


def test_find_or_create_user_rejects_unverified_email(db):
    with pytest.raises(ValueError, match="not verified"):
        oauth.find_or_create_user(db, userinfo(verified=False))


def test_find_or_create_user_needs_id_and_email(db):
    with pytest.raises(ValueError):
        oauth.find_or_create_user(db, {"sub": "", "email": ""})


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    application = agent_app.create_app(db_path=str(tmp_path / "web.db"))
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


def test_google_login_redirects_to_password_login_when_unconfigured(web):
    response = web.test_client().get("/login/google")
    assert response.status_code == 302 and "/login" in response.location
    assert "/login/google" not in response.location


def test_google_callback_signs_in_with_mocked_client(web, monkeypatch):
    class FakeGoogle:
        def authorize_redirect(self, uri):
            raise AssertionError("no real redirect in tests")

        def authorize_access_token(self):
            return {"userinfo": userinfo()}

    class FakeOAuth:
        google = FakeGoogle()

    monkeypatch.setattr(oauth, "build_oauth", lambda app: FakeOAuth())
    application = agent_app.create_app(db_path=str(web.config["DB_PATH"] + "-oauth"))
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    client = application.test_client()
    response = client.get("/auth/google/callback")
    assert response.status_code == 302 and response.location.endswith("/")
    assert client.get("/").status_code == 200
    assert store.get_user_by_google_sub(
        application.config["DB_PATH"], "google-1"
    ) is not None


def test_google_callback_failure_lands_on_login(web, monkeypatch):
    class FakeGoogle:
        def authorize_access_token(self):
            raise RuntimeError("google is down")

    class FakeOAuth:
        google = FakeGoogle()

    monkeypatch.setattr(oauth, "build_oauth", lambda app: FakeOAuth())
    application = agent_app.create_app(db_path=str(web.config["DB_PATH"] + "-bad"))
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    response = application.test_client().get("/auth/google/callback")
    assert response.status_code == 302 and "/login" in response.location
