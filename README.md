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

`facehugger crawl` advances a durable public-model catalog and reverse index.
After the first catalog pass, a new pass is due on the UTC day after the
previous enumeration finishes, regardless of whether the index has been published.
Passes scan newest-created repositories first, checkpoint their pagination
cursor, and reuse unchanged inspections. During refreshes, part of each
invocation is reserved for inspecting known pending work, including new and
changed repositories discovered in partial passes. A completed pass reconciles
repositories the Hub no longer lists. An interrupted pass retains observations
from the previous pass until reconciliation.

Successful inspection reconciles gate status with the latest Hub metadata.
Each invocation stops after its configured time limit; the hosted
workflow caps batches at 240 minutes, leaving
90 minutes to publish durable state before its 330-minute job deadline.
Cataloging and inspection share that deadline, so ordinary request-latency
variation cannot turn a complete checkpoint into a job timeout. Each
replacement is atomic, so interrupted inspections retain the prior verified records.

Metadata reads use bounded concurrency behind one shared Hub request limiter.
When the Hub confirms that a cataloged repository or revision is no longer
available, Facehugger removes it from the active generation; a later catalog
can reintroduce it if it returns.

The crawler stages a new complete static index only after the catalog is fully
enumerated and no pending inspections remain. Compilation streams database
records and holds one bounded shard in memory at a time. It chooses a three- or
four-hex-character shard prefix and refuses any shard over 128 KiB. The
workflow publishes a verified immutable SQLite checkpoint for every bounded
invocation and retains the eight most recent checkpoints. Checkpoint archives
are split into verified release assets before they approach GitHub's per-asset
limit. It restores the
newest verified checkpoint before using the `full-crawl-state` prerelease as a
legacy fallback, then deploys only a complete staged index. Publication also
refuses a staged site larger than 900 MiB, below the GitHub Pages 1 GiB limit.

After each successful incomplete checkpoint, the workflow queues the next
time-bounded invocation itself. The scheduled workflow is a recovery backstop.

The README badges show the current crawl state plus the latest durable count of
known eligible repositories inspected and remaining. Zero remaining means
there are no pending inspections in the recorded catalog; it does not measure
live Hub coverage. The state badge updates when a
batch starts, completes, or fails; the count badges update whenever a batch
checkpoints verified state.

Publication failures retain the completed inspection counts and verified state,
while the workflow reports failure rather than publishing an incomplete index.

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
