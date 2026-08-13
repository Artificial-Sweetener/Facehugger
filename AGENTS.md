# Repository Engineering Standards

These standards apply throughout the repository. A nested `AGENTS.md` may add
local ownership and verification requirements; it does not replace this file.

Committed files must not contain developer-specific absolute paths, usernames,
hostnames, shell profiles, hardware details, credentials, tokens, or machine
configuration. Keep reproducible project requirements explicit and portable.

## Ownership and boundaries

Each concern has one authoritative owner. Place code by ownership rather than
by proximity or the smallest possible diff.

- The domain layer owns hash validation, typed records, and domain errors.
- The lookup protocol owns manifest and shard schemas, parsing, and integrity
  validation.
- The client and CLI own user-facing lookup behavior and persistent cache use.
- The indexer owns Hub metadata access, cataloging, filtering, throttling,
  SQLite state, compilation, verification, and reports.
- Workflows orchestrate supported commands; they do not contain business logic.

The client must depend only on the public lookup protocol and domain layer. It
must not import indexer code or require Hugging Face or GitHub API clients.
Collaborators use explicit public interfaces and injected dependencies rather
than reaching into private state.

Do not add behavior to a mixed-responsibility module. When a touched module has
mixed ownership, characterize its current behavior, extract the touched
responsibility into a cohesive module, migrate every caller, remove replaced
code, and verify the resulting boundary. Internal compatibility layers,
forwarders, aliases, and dual canonical implementations are not acceptable.

Production modules should have one concern and one reason to change. Use 350
nonblank, noncomment lines as a soft ceiling and 500 as a hard ceiling. A
waiver never permits mixed ownership.

## Python engineering

- Support only the Python versions declared in package metadata.
- Add accurate type annotations and docstrings to every new or changed public
  module, class, function, method, property, and value. Keep internal
  docstrings concise and ownership-focused.
- Validate untrusted data at every external boundary. Preserve exception causes,
  use stable domain exceptions for expected failures, and catch broad exceptions
  only where recovery, translation, or reporting is possible.
- Avoid mutable default arguments, import-time work, implicit process-global
  state, service locators, and stringly typed internal contracts.
- Keep module order predictable: module docstring, imports, logger, public
  values, primary implementations, then private details.
- Use expressive names. Comments explain constraints that cannot be made clear
  in code.

## Data integrity, security, and operations

- Treat data schemas, hash semantics, state transitions, cache behavior, and
  publication as public correctness contracts.
- Make state-changing operations atomic, resumable, and idempotent. A failed
  operation must preserve the last verified state and must not publish partial
  output.
- Validate generated artifacts before publication and verify restoration before
  use. Keep integrity checks close to the boundary that can act on a failure.
- Never commit secrets, authorization headers, signed URLs, raw exception
  payloads, local databases, caches, generated sites, downloaded artifacts, or
  benchmark output. Redact sensitive context from logs and reports.
- Use bounded concurrency, finite timeouts, and explicit cancellation or retry
  behavior for all network and long-running operations.
- Do not add telemetry, hidden network calls, or an undeclared external service.

## Public contracts and documentation

Each public-contract change updates the typed contract and exports,
implementation, reference and narrative documentation, and public-boundary
tests in the same change. Examples and consumer tests use supported public APIs,
not private implementation details.

Documentation describes the current product directly. Do not retain removed
architecture, migration history, or nonexistent alternatives as user guidance.

## Tests and verification

- Treat tests, fixtures, and harnesses as production code with clear ownership.
- Add or update regression protection with every behavior or structural change.
  A behavior change is incomplete unless a test would fail before the change and
  passes afterward; use a focused regression test when no existing test already
  demonstrates that distinction.
- Unit tests isolate an owner; contract tests use supported public boundaries;
  integration tests prove collaboration among owners.
- Tests are deterministic and order-independent. Use controlled clocks, seeds,
  schedules, and fixtures instead of arbitrary sleeps, retry luck, shared global
  state, or collection order.
- Derive expected results from explicit contracts, independent oracles, fixed
  canonical fixtures, or externally observable behavior. Do not duplicate the
  production algorithm in a test and call agreement proof.
- Live network tests are opt-in, use a fixed small corpus, never run for
  untrusted pull requests, and must not require credentials in routine CI.
- Treat correctness, integrity, performance, recovery, and cache behavior as
  contracts. Fix the authoritative owner; do not weaken a test or budget merely
  to make a change pass.
- Before reporting any source, test, packaging, workflow, or tool-configuration
  change complete, run and report this required quality gate from the repository
  root:

  ```powershell
  uv lock --check
  uv run ruff format --check .
  uv run ruff check .
  uv run pyright
  uv run pytest
  ```

  Do not call a turn complete when any required command has not run or fails.
  Documentation-only changes require `uv lock --check`, the Ruff formatter and
  linter checks, and `git diff --check`; run the full gate whenever the
  documentation changes a public contract or executable example.

## Workspace and Git

- Preserve unrelated worktree changes. Never use destructive reset or checkout
  commands to remove work that is not yours.
- Use `apply_patch` for source and documentation edits. Do not let formatters
  rewrite unrelated files.
- Do not commit generated environments, build output, caches, downloaded
  artifacts, local configuration, or editor-specific state.
- Commit only when explicitly asked. Each commit must be a coherent,
  independently releasable outcome with its supporting tests, documentation,
  cleanup, and refactoring.
- Use changelog-ready commit subjects in the form
  `type(scope): outcome`. Mark public breaking changes with `!` and explain the
  compatibility impact and migration in the commit body.
