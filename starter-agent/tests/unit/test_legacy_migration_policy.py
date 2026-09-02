from datetime import UTC, datetime, timedelta

from starter_agent.delegation.legacy_migration import LegacyMigrationPolicy


NOW = datetime(2026, 8, 13, tzinfo=UTC)


def test_legacy_migration_is_disabled_by_default() -> None:
    decision = LegacyMigrationPolicy().authorize(
        actor_subject="operator:1", actor_role="operator", reason="incident", now=NOW
    )
    assert decision.allowed is False
    assert decision.code == "legacy_path_disabled"


def test_legacy_migration_requires_trusted_operator_and_has_earliest_deadline() -> None:
    policy = LegacyMigrationPolicy(
        enabled=True,
        enabled_at=NOW,
        release_window_ends=(NOW + timedelta(days=3), NOW + timedelta(days=8)),
    )
    denied = policy.authorize(
        actor_subject="viewer:1", actor_role="viewer", reason="incident", now=NOW
    )
    allowed = policy.authorize(
        actor_subject="operator:1", actor_role="operator", reason="incident", now=NOW
    )
    assert denied.code == "legacy_path_operator_required"
    assert allowed.allowed is True
    assert allowed.delete_deadline == NOW + timedelta(days=8)


def test_legacy_migration_expires_without_enabling_normal_route_fallback() -> None:
    policy = LegacyMigrationPolicy(enabled=True, enabled_at=NOW - timedelta(days=15))
    decision = policy.authorize(
        actor_subject="operator:1", actor_role="operator", reason="incident", now=NOW
    )
    assert decision.allowed is False
    assert decision.code == "legacy_path_expired"
