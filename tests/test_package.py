"""Public package smoke tests."""

import facehugger


def test_package_imports() -> None:
    """The public package is importable."""
    assert facehugger.__name__ == "facehugger"
