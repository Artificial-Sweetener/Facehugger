"""SQLite reverse-index replacement and duplicate tests."""

from pathlib import Path

from facehugger.models import InspectedFile, InspectedRepo
from facehugger.state import IndexState

_DIGEST = bytes.fromhex("ab" * 32)


def test_state_returns_duplicate_occurrences_and_replaces_a_repo(tmp_path: Path) -> None:
    """A changed repository replaces only its own current occurrences."""
    state = IndexState(tmp_path / "state.sqlite")
    try:
        first = InspectedRepo(
            "one/model",
            "1" * 40,
            (InspectedFile("model.safetensors", 10, "git-1", _DIGEST, None, "lfs"),),
        )
        second = InspectedRepo(
            "two/model",
            "2" * 40,
            (InspectedFile("copy.safetensors", 10, "git-2", _DIGEST, None, "lfs"),),
        )
        state.replace_repo(first, first.files)
        state.replace_repo(second, second.files)
        assert [item.repo_id for item in state.lookup(_DIGEST)] == ["one/model", "two/model"]

        replacement = InspectedRepo("one/model", "3" * 40, ())
        state.replace_repo(replacement, replacement.files)
        assert [item.repo_id for item in state.lookup(_DIGEST)] == ["two/model"]
        state.validate()
    finally:
        state.close()
