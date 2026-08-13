# Facehugger

Facehugger is a Python utility for exact reverse lookup of Hugging Face model
artifact SHA-256 digests.

Each exact match includes the repository page and a revision-pinned Hugging Face
file resolver URL. Both links are derived locally from the indexed repository,
revision, and file path; lookup does not call a Hugging Face API. Matches also
carry the public gate state observed while indexed. Applications can present a
gated repository page to the user or use the resolver URL directly.

## Index operations

`facehugger crawl` advances a durable, generation-based public-model crawl. It
persists the Hub pagination continuation and catalog observations before any
metadata inspection, then inspects only repositories that are new, revision
changed, or whose indexed gate state changed. A complete catalog generation
reconciles repositories no longer returned by the Hub. Each replacement is
atomic, so interrupted runs retain the prior verified records.

The crawler stages a new complete static index only after the catalog is fully
enumerated and no pending inspections remain. Compilation chooses a three- or
four-hex-character shard prefix and refuses any shard over 128 KiB. The
workflow preserves its compressed SQLite state in the `full-crawl-state`
prerelease between bounded GitHub Actions invocations, then deploys only a
complete staged index. Publication also refuses a staged site larger than
900 MiB, below the GitHub Pages 1 GiB limit.

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
