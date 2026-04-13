"""Security-focused tests for XSS, path traversal, and rate limiting."""

from __future__ import annotations

import time

from link_project_to_chat.auth import Authenticator
from link_project_to_chat.formatting import md_to_telegram
from link_project_to_chat.rate_limiter import RateLimiter
from link_project_to_chat.ui import sanitize_filename

# --- XSS Prevention in formatting.py ---


class TestXSSPrevention:
    def test_javascript_url_stripped(self) -> None:
        result = md_to_telegram("[click me](javascript:alert('xss'))")
        assert "javascript:" not in result
        assert "click me" in result

    def test_data_url_stripped(self) -> None:
        result = md_to_telegram("[link](data:text/html,<h1>xss</h1>)")
        assert "data:" not in result

    def test_vbscript_url_stripped(self) -> None:
        result = md_to_telegram("[link](vbscript:msgbox('xss'))")
        assert "vbscript:" not in result

    def test_safe_http_url_preserved(self) -> None:
        result = md_to_telegram("[link](https://example.com)")
        assert "https://example.com" in result
        assert '<a href="https://example.com">link</a>' in result

    def test_safe_mailto_preserved(self) -> None:
        result = md_to_telegram("[email](mailto:test@test.com)")
        assert "mailto:test@test.com" in result

    def test_code_block_lang_escaped(self) -> None:
        # Language attribute with special chars should be escaped
        result = md_to_telegram('```"><script>alert(1)</script>\ncode\n```')
        assert "<script>" not in result
        assert "&lt;script&gt;" in result or "script" not in result or "language-&quot;" in result


# --- Path Traversal Prevention ---


class TestPathTraversal:
    def test_sanitize_strips_dotdot(self) -> None:
        result = sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert "\\" not in result
        # Result should be safe alphanumeric + dots
        assert all(c.isalnum() or c in "._- " for c in result)

    def test_sanitize_strips_backslash_traversal(self) -> None:
        result = sanitize_filename("..\\..\\windows\\system32\\config")
        assert "\\" not in result

    def test_sanitize_null_byte(self) -> None:
        result = sanitize_filename("file\x00.txt")
        # Null byte should not be in result
        assert "\x00" not in result

    def test_sanitize_empty_produces_nonempty_or_empty(self) -> None:
        # Empty string after sanitization is fine - caller handles it
        result = sanitize_filename("")
        assert isinstance(result, str)


# --- Rate Limiter ---


class TestRateLimiter:
    def test_allows_up_to_limit(self) -> None:
        rl = RateLimiter(max_per_minute=5)
        for _ in range(5):
            assert rl.is_limited(1) is False
        assert rl.is_limited(1) is True

    def test_independent_per_user(self) -> None:
        rl = RateLimiter(max_per_minute=3)
        for _ in range(3):
            rl.is_limited(1)
        # User 2 should not be affected
        assert rl.is_limited(2) is False

    def test_cleanup_removes_stale_entries(self) -> None:
        rl = RateLimiter(max_per_minute=5)
        rl.is_limited(1)
        # Manually age the timestamps
        for ts in rl._timestamps.values():
            while ts:
                ts.popleft()
            ts.append(time.monotonic() - 400)  # 6+ minutes ago
        # Trigger cleanup
        rl.is_limited(2)
        assert 1 not in rl._timestamps


# --- Authenticator (DI version) ---


class TestAuthenticator:
    def test_fail_closed_no_username(self) -> None:
        auth = Authenticator(allowed_username="")
        user = type("U", (), {"id": 1, "username": "alice"})()
        assert auth.authenticate(user) is False

    def test_authenticate_none_user(self) -> None:
        auth = Authenticator(allowed_username="alice")
        assert auth.authenticate(None) is False

    def test_first_contact_locks_id(self) -> None:
        trusted_ids: list[int] = []
        auth = Authenticator(
            allowed_username="alice",
            on_trust=lambda uid: trusted_ids.append(uid),
        )
        user = type("U", (), {"id": 42, "username": "Alice"})()
        assert auth.authenticate(user) is True
        assert auth.trusted_user_id == 42
        assert trusted_ids == [42]

    def test_wrong_user_denied(self) -> None:
        auth = Authenticator(allowed_username="alice", trusted_user_id=42)
        bad_user = type("U", (), {"id": 99, "username": "mallory"})()
        assert auth.authenticate(bad_user) is False

    def test_brute_force_blocked(self) -> None:
        auth = Authenticator(allowed_username="alice", trusted_user_id=42, max_failed_attempts=3)
        bad = type("U", (), {"id": 99, "username": "x"})()
        for _ in range(3):
            auth.authenticate(bad)
        assert auth.authenticate(bad) is False


# --- RateLimiter memory cleanup ---


class TestRateLimiterCleanup:
    def test_stale_entries_cleaned(self) -> None:
        rl = RateLimiter(max_per_minute=30)
        rl.is_limited(1)
        # Age the entry
        for ts in rl._timestamps.values():
            while ts:
                ts.popleft()
            ts.append(time.monotonic() - 400)
        # Trigger cleanup via another user
        rl.is_limited(2)
        assert 1 not in rl._timestamps
