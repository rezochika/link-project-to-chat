from link_project_to_chat.team_safety import (
    AuthorityGrant,
    TeamAuthority,
    VALID_SCOPES,
    parse_auth_directives,
)


def test_parse_auth_directives_empty_and_unknown():
    assert parse_auth_directives("") == frozenset()
    assert parse_auth_directives(None) == frozenset()
    assert parse_auth_directives("--auth pushh") == frozenset()
    assert parse_auth_directives("--authentication push") == frozenset()
    assert parse_auth_directives("--auth-mode push") == frozenset()


def test_parse_auth_directives_valid_repeated_scopes():
    assert parse_auth_directives("@bot push --auth push") == frozenset({"push"})
    assert parse_auth_directives("--auth push --auth pr_create") == frozenset({"push", "pr_create"})
    assert parse_auth_directives("--auth all") == frozenset({"all"})


def test_authority_grant_covers_and_expires():
    grant = AuthorityGrant(user_message_id=1, scopes=frozenset({"push"}), granted_at=100.0)
    assert grant.covers("push") is True
    assert grant.covers("release") is False
    assert grant.is_expired(now=700.0, ttl=600.0) is False
    assert grant.is_expired(now=701.0, ttl=600.0) is True


def test_authority_grant_all_covers_any_scope():
    grant = AuthorityGrant(user_message_id=1, scopes=frozenset({"all"}), granted_at=0.0)
    assert grant.covers("push") is True
    assert grant.covers("release") is True
    assert grant.covers("anything") is True


def test_team_authority_records_checks_and_consumes_grants():
    auth = TeamAuthority(team_name="lpct")
    assert auth.record_user_message(msg_id=10, text="hello") == frozenset()
    assert auth.is_authorized("push") is False

    assert auth.record_user_message(msg_id=11, text="@bot --auth push") == frozenset({"push"})
    assert auth.is_authorized("push") is True
    assert auth.is_authorized("release") is False

    consumed = auth.consume_grant("push")
    assert consumed is not None
    assert consumed.user_message_id == 11
    assert auth.is_authorized("push") is False


def test_team_authority_all_grant_consumed_once():
    auth = TeamAuthority(team_name="lpct")
    auth.record_user_message(msg_id=12, text="--auth all")
    assert auth.consume_grant("release") is not None
    assert auth.is_authorized("push") is False


def test_team_authority_retains_only_four_grants():
    auth = TeamAuthority(team_name="lpct")
    for i in range(6):
        auth.record_user_message(msg_id=i, text="--auth push")
    assert len(auth._grants) == 4


def test_team_authority_status_snapshot():
    auth = TeamAuthority(team_name="lpct")
    auth.record_user_message(msg_id=42, text="--auth push")
    snap = auth.status_snapshot
    assert snap["team_name"] == "lpct"
    assert snap["active_grants"][0]["scopes"] == ["push"]
    assert snap["active_grants"][0]["user_message_id"] == 42


def test_valid_scopes_are_closed():
    assert VALID_SCOPES == frozenset({"push", "pr_create", "release", "network", "all"})
