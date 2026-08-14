"""Public lookup-record contract tests."""

from facehugger.models import Occurrence


def test_gated_occurrence_exposes_local_repository_and_resolver_flows() -> None:
    """A caller gets both direct resolution and gated-page presentation links without a Hub call."""
    occurrence = Occurrence(
        repo_id="owner/gated model",
        path="weights/model file.safetensors",
        revision="1" * 40,
        size=42,
        storage="lfs",
        gated=True,
    )
    assert occurrence.repository_url == "https://huggingface.co/owner/gated%20model"
    assert occurrence.resolver_url.endswith("/weights/model%20file.safetensors")
    assert occurrence.thumbnail_url == (
        "https://cdn-thumbnails.huggingface.co/social-thumbnails/models/owner/gated%20model.png"
    )
    assert occurrence.as_dict() == {
        "repo_id": "owner/gated model",
        "path": "weights/model file.safetensors",
        "revision": "1" * 40,
        "size": 42,
        "storage": "lfs",
        "gated": True,
        "repository_url": "https://huggingface.co/owner/gated%20model",
        "resolver_url": "https://huggingface.co/owner/gated%20model/resolve/"
        + "1" * 40
        + "/weights/model%20file.safetensors",
        "thumbnail_url": "https://cdn-thumbnails.huggingface.co/social-thumbnails/models/"
        "owner/gated%20model.png",
    }
