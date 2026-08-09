import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response
from starlette.requests import Request

from sub2gen import main as app_main
from sub2gen.api import admin
from sub2gen.core.config import Config


def make_request(
    path="/",
    *,
    cookie=None,
    authorization=None,
    forwarded_proto=None,
    scheme="http",
):
    headers = []
    if cookie:
        headers.append((b"cookie", f"admin_session={cookie}".encode("ascii")))
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    if forwarded_proto:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app_main.app,
        }
    )


class FakeAdminDb:
    def __init__(self, valid_tokens=None):
        self.valid_tokens = set(valid_tokens or [])
        self.insert_calls = []
        self.delete_calls = []
        self.delete_all_calls = 0
        self.update_config_calls = []
        self.reload_calls = 0

    async def is_admin_session_valid(self, token):
        return token in self.valid_tokens

    async def insert_admin_session(self, token, expires_at):
        self.insert_calls.append((token, expires_at))
        self.valid_tokens.add(token)

    async def delete_admin_session(self, token):
        self.delete_calls.append(token)
        self.valid_tokens.discard(token)

    async def delete_all_admin_sessions(self):
        self.delete_all_calls += 1
        self.valid_tokens.clear()

    async def update_admin_config(self, **changes):
        self.update_config_calls.append(changes)

    async def reload_config_to_memory(self):
        self.reload_calls += 1

    async def get_dashboard_stats(self):
        return {"total_tokens": 2}


class ConfigPathSafetyTests(unittest.TestCase):
    @staticmethod
    def write_config(path, api_key="file-key"):
        path.write_text(
            "[global]\n"
            f"api_key = \"{api_key}\"\n"
            "admin_username = \"admin\"\n"
            "admin_password = \"password\"\n",
            encoding="utf-8",
        )

    def test_regular_setting_file_loads_and_keeps_environment_overrides(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_dir = root / "config"
            config_dir.mkdir()
            self.write_config(config_dir / "setting.toml")

            with (
                patch("sub2gen.core.config.REPO_ROOT", root),
                patch.dict("os.environ", {"SUB2GEN_API_KEY": "environment-key"}),
            ):
                loaded = Config.__new__(Config)._load_config()

        self.assertEqual(loaded["global"]["api_key"], "environment-key")

    def test_setting_directory_falls_back_to_example_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "setting.toml").mkdir()
            self.write_config(config_dir / "setting_example.toml", api_key="fallback-key")

            with patch("sub2gen.core.config.REPO_ROOT", root):
                loaded = Config.__new__(Config)._load_config()

        self.assertEqual(loaded["global"]["api_key"], "fallback-key")

    def test_missing_setting_and_fallback_raise_concise_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "config").mkdir()

            with patch("sub2gen.core.config.REPO_ROOT", root):
                with self.assertRaisesRegex(FileNotFoundError, "Configuration file"):
                    Config.__new__(Config)._load_config()

    def test_unreadable_setting_and_fallback_raise_concise_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_dir = root / "config"
            config_dir.mkdir()
            self.write_config(config_dir / "setting.toml")
            self.write_config(config_dir / "setting_example.toml")

            with (
                patch("sub2gen.core.config.REPO_ROOT", root),
                patch("builtins.open", side_effect=PermissionError("denied")),
            ):
                with self.assertRaisesRegex(FileNotFoundError, "unreadable"):
                    Config.__new__(Config)._load_config()


class AdminCookieAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_sets_secure_remembered_cookie_and_persists_session(self):
        fake_db = FakeAdminDb()
        response = Response()
        request = make_request("/api/login", forwarded_proto="https")
        payload = admin.LoginRequest(username="admin", password="password", remember_me=True)

        with patch.object(admin.AuthManager, "verify_admin", return_value=True):
            result = await admin.admin_login(
                payload,
                request,
                response,
                container=SimpleNamespace(db=fake_db),
            )

        cookie = response.headers["set-cookie"].lower()
        self.assertTrue(result["success"])
        self.assertIn("admin_session=", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("secure", cookie)
        self.assertIn(f"max-age={admin._ADMIN_SESSION_TTL_REMEMBER}", cookie)
        self.assertEqual(fake_db.insert_calls[0][0], result["token"])
        self.assertGreaterEqual(
            fake_db.insert_calls[0][1],
            int(time.time()) + admin._ADMIN_SESSION_TTL_REMEMBER - 2,
        )

    async def test_non_remembered_login_uses_browser_cookie_and_24_hour_server_ttl(self):
        fake_db = FakeAdminDb()
        response = Response()
        request = make_request("/api/login")
        payload = admin.LoginRequest(username="admin", password="password", remember_me=False)

        with patch.object(admin.AuthManager, "verify_admin", return_value=True):
            await admin.admin_login(
                payload,
                request,
                response,
                container=SimpleNamespace(db=fake_db),
            )

        cookie = response.headers["set-cookie"].lower()
        self.assertNotIn("max-age=", cookie)
        self.assertNotIn("secure", cookie)
        self.assertGreaterEqual(
            fake_db.insert_calls[0][1],
            int(time.time()) + admin._ADMIN_SESSION_TTL_BROWSER - 2,
        )

    async def test_verifier_accepts_bearer_or_cookie_and_rejects_invalid_sessions(self):
        fake_db = FakeAdminDb({"header-session", "cookie-session"})
        container = SimpleNamespace(db=fake_db)
        self.assertEqual(
            await admin.verify_admin_token(
                make_request(authorization="Bearer header-session"),
                "Bearer header-session",
                container,
            ),
            "header-session",
        )
        self.assertEqual(
            await admin.verify_admin_token(
                make_request(cookie="cookie-session"), None, container
            ),
            "cookie-session",
        )
        with self.assertRaises(HTTPException) as invalid:
            await admin.verify_admin_token(make_request(cookie="invalid"), None, container)

        self.assertEqual(invalid.exception.status_code, 401)

    async def test_logout_invalidates_header_and_cookie_sessions_and_deletes_cookie(self):
        fake_db = FakeAdminDb({"header-session", "cookie-session"})
        request = make_request(cookie="cookie-session", forwarded_proto="https")
        response = Response()

        await admin.admin_logout(
            request,
            response,
            "header-session",
            container=SimpleNamespace(db=fake_db),
        )

        self.assertEqual(set(fake_db.delete_calls), {"header-session", "cookie-session"})
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("admin_session=", cookie)
        self.assertIn("max-age=0", cookie)
        self.assertIn("secure", cookie)

    async def test_password_change_invalidates_all_sessions_and_deletes_cookie(self):
        fake_db = FakeAdminDb({"cookie-session"})
        request = make_request(cookie="cookie-session")
        response = Response()
        payload = admin.ChangePasswordRequest(
            username="admin",
            old_password="old-password",
            new_password="new-password",
        )

        with patch.object(admin.AuthManager, "verify_admin", return_value=True):
            result = await admin.change_password(
                payload,
                request,
                response,
                "cookie-session",
                container=SimpleNamespace(db=fake_db),
            )

        self.assertTrue(result["success"])
        self.assertEqual(fake_db.delete_all_calls, 1)
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())

    async def test_stats_upgrades_valid_legacy_bearer_session_to_cookie(self):
        fake_db = FakeAdminDb({"legacy-session"})
        request = make_request(authorization="Bearer legacy-session", forwarded_proto="https")
        response = Response()

        result = await admin.get_stats(
            request,
            response,
            "legacy-session",
            container=SimpleNamespace(db=fake_db),
        )

        self.assertEqual(result["total_tokens"], 2)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("admin_session=legacy-session", cookie)
        self.assertIn("secure", cookie)


class AdminSpaGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthenticated_manage_and_test_routes_redirect_to_login(self):
        with patch.object(
            app_main.admin,
            "is_admin_session_token_valid",
            AsyncMock(return_value=False),
        ):
            for path in ("manage", "test"):
                response = await app_main.serve_spa(path, make_request(f"/{path}"))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/login")

    async def test_authenticated_spa_response_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tempdir:
            static_path = Path(tempdir)
            (static_path / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
            with (
                patch.object(app_main, "static_path", static_path),
                patch.object(
                    app_main.admin,
                    "is_admin_session_token_valid",
                    AsyncMock(return_value=True),
                ),
            ):
                response = await app_main.serve_spa(
                    "manage",
                    make_request("/manage", cookie="valid-session"),
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate")

    def test_obsolete_static_admin_pages_remain_deleted(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "static" / "login.html").exists())
        self.assertFalse((root / "static" / "manage.html").exists())


if __name__ == "__main__":
    unittest.main()
