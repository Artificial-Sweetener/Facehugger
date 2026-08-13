# Facehugger

Facehugger is a Python utility for exact reverse lookup of Hugging Face model
artifact SHA-256 digests.

Each exact match includes the repository page and a revision-pinned Hugging Face
file resolver URL. Both links are derived locally from the indexed repository,
revision, and file path; lookup does not call a Hugging Face API. Matches also
carry the public gate state observed while indexed. Applications can present a
gated repository page to the user or use the resolver URL directly.

## Development

The project requires Python 3.12 or later and uses [uv](https://docs.astral.sh/uv/)
to manage its development environment.

```powershell
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

## License

Facehugger is licensed under the GNU Affero General Public License v3.0 or
later. See [LICENSE](LICENSE).
