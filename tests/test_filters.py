"""Artifact candidate selection tests."""

from pathlib import Path

from facehugger.filters import is_candidate_artifact, load_artifact_extensions


def test_artifact_extension_configuration_is_case_insensitive() -> None:
    """Configured artifact suffixes match nested paths regardless of case."""
    config = Path(__file__).parents[1] / "config" / "artifacts.toml"
    extensions = load_artifact_extensions(config)
    assert is_candidate_artifact("weights/MODEL.SAFETENSORS", extensions)
    assert is_candidate_artifact("snapshots/model.gguf", extensions)
    assert not is_candidate_artifact("tokenizer.json", extensions)
