# Facehugger

Facehugger is a Python utility for exact reverse lookup of Hugging Face model
artifact SHA-256 digests.

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
