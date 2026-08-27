from starter_agent.settings import ArtifactConfig


def test_artifact_retention_defaults_to_14_days_and_is_bounded() -> None:
    assert ArtifactConfig().retention_days == 14
    assert ArtifactConfig(retention_days=1).retention_days == 1
