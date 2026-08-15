# Facehugger

[![Crawl status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArtificial-Sweetener%2FFacehugger%2Fbadge-data%2Ffull-crawl-status-badge.json&cacheSeconds=300)](https://github.com/Artificial-Sweetener/Facehugger/actions/workflows/crawl.yml)
[![Eligible repositories inspected](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArtificial-Sweetener%2FFacehugger%2Fbadge-data%2Ffull-crawl-inspected-badge.json&cacheSeconds=300)](https://github.com/Artificial-Sweetener/Facehugger/actions/workflows/crawl.yml)
[![Eligible repositories remaining](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArtificial-Sweetener%2FFacehugger%2Fbadge-data%2Ffull-crawl-remaining-badge.json&cacheSeconds=300)](https://github.com/Artificial-Sweetener/Facehugger/actions/workflows/crawl.yml)

Facehugger is a Python utility for exact reverse lookup of Hugging Face model
artifact SHA-256 digests.

Each exact match includes the repository page, a revision-pinned Hugging Face
file resolver URL, and the Hub-hosted social thumbnail URL for its repository.
Facehugger derives these links locally from indexed repository, revision, and
file-path data; lookup does not call a Hugging Face API or store thumbnail
images. Matches also carry the public gate state observed while indexed.
Applications can present a gated repository page to the user, load the
thumbnail directly from the Hub, or use the resolver URL directly.

## Index operations

`facehugger crawl` advances a durable, generation-based public-model crawl. It
persists the Hub pagination continuation and catalog observations before any
metadata inspection, then inspects only repositories that are new, revision
changed, or whose indexed gate state changed. Each invocation enumerates at
most 1,000 catalog pages and inspects at most 25,000 repositories, leaving time
to publish durable state before the workflow deadline. A complete catalog
generation reconciles repositories no longer returned by the Hub. Each
replacement is atomic, so interrupted runs retain the prior verified records.

The crawler stages a new complete static index only after the catalog is fully
enumerated and no pending inspections remain. Compilation chooses a three- or
four-hex-character shard prefix and refuses any shard over 128 KiB. The
workflow preserves its compressed SQLite state in the `full-crawl-state`
prerelease between bounded GitHub Actions invocations, then deploys only a
complete staged index. Publication also refuses a staged site larger than
900 MiB, below the GitHub Pages 1 GiB limit.

After each successful incomplete checkpoint, the workflow queues the next
bounded invocation itself. The scheduled workflow is a recovery backstop.

The README badges show the current crawl state plus the latest durable count of
eligible repositories inspected and remaining. The state badge updates when a
batch starts, completes, or fails; the count badges update whenever a batch
checkpoints verified state.

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
