"""Deterministic bounded proof-corpus tests."""

from facehugger.indexer.catalog import select_proof_repos
from facehugger.models import CatalogRepo


def test_proof_selection_is_bounded_deterministic_and_includes_gated_public_repos() -> None:
    """Public gating does not exclude an otherwise eligible repository."""
    catalog = tuple(
        CatalogRepo(
            repo_id=f"owner/model-{index}",
            revision=None,
            last_modified=None,
            downloads=index,
            private=False,
            gated=index == 0,
            sibling_paths=("weights/model.safetensors",),
        )
        for index in range(20)
    )
    selected = select_proof_repos(
        catalog,
        (".safetensors",),
        ("owner/model-0",),
        popular_limit=3,
        random_limit=3,
        targeted_limit=3,
    )
    assert selected == select_proof_repos(
        catalog,
        (".safetensors",),
        ("owner/model-0",),
        popular_limit=3,
        random_limit=3,
        targeted_limit=3,
    )
    assert "owner/model-0" in selected
    assert len(selected) <= 9
