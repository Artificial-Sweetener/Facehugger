"""SQLite state and reverse-index operations."""

import sqlite3
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from facehugger.models import CatalogRepo, InspectedFile, InspectedRepo, Occurrence, PendingRepo

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    repo_id TEXT NOT NULL UNIQUE,
    revision_sha TEXT,
    private INTEGER NOT NULL DEFAULT 0,
    gated INTEGER NOT NULL DEFAULT 0,
    catalog_revision_sha TEXT,
    catalog_gated INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 0,
    catalog_generation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    last_scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    sha256 BLOB NOT NULL UNIQUE,
    size INTEGER
);

CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    repo_id_fk INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    revision_sha TEXT NOT NULL,
    path TEXT NOT NULL,
    git_blob_oid TEXT,
    xet_hash TEXT,
    storage TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    UNIQUE(repo_id_fk, revision_sha, path)
);

CREATE INDEX IF NOT EXISTS occurrences_artifact ON occurrences(artifact_id);
CREATE INDEX IF NOT EXISTS occurrences_repo ON occurrences(repo_id_fk);

CREATE TABLE IF NOT EXISTS candidate_files_without_sha256 (
    repo_id_fk INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    revision_sha TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    git_blob_oid TEXT,
    xet_hash TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY(repo_id_fk, revision_sha, path)
);
"""


class IndexState:
    """Persistent reverse index with replacement semantics per repository."""

    def __init__(self, path: Path) -> None:
        """Open or create the SQLite state at ``path``."""
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(_SCHEMA)
        self._migrate_schema()
        self.connection.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.connection.close()

    def set_metadata(self, key: str, value: str) -> None:
        """Store one small state metadata value."""
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.connection.commit()

    def get_metadata(self, key: str) -> str | None:
        """Return a metadata value, if present."""
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def start_catalog_generation(self) -> int:
        """Return the active catalog generation, creating one when needed."""
        active = self.get_metadata("active_catalog_generation")
        if active is not None:
            return int(active)
        completed = self.get_metadata("completed_catalog_generation")
        generation = 1 if completed is None else int(completed) + 1
        self.set_metadata("active_catalog_generation", str(generation))
        self.set_metadata("catalog_generation_complete", "false")
        return generation

    def catalog_generation_complete(self) -> bool:
        """Return whether the active catalog generation was fully enumerated."""
        return self.get_metadata("catalog_generation_complete") == "true"

    def record_catalog_repositories(
        self, observations: tuple[tuple[CatalogRepo, bool], ...], *, generation: int
    ) -> None:
        """Persist a bounded catalog batch without replacing prior verified occurrences."""
        if not observations:
            return
        with self.transaction():
            for repo, eligible in observations:
                self._record_catalog_repo(repo, eligible=eligible, generation=generation)
            self._remove_orphan_artifacts()

    def record_catalog_repo(self, repo: CatalogRepo, *, eligible: bool, generation: int) -> None:
        """Persist one catalog observation without replacing prior verified occurrences."""
        with self.transaction():
            self._record_catalog_repo(repo, eligible=eligible, generation=generation)
            self._remove_orphan_artifacts()

    def finish_catalog_generation(self, generation: int) -> None:
        """Reconcile removals after a complete catalog enumeration."""
        active = self.get_metadata("active_catalog_generation")
        if active is None or generation != int(active):
            raise ValueError("Catalog generation is not active.")
        with self.transaction():
            self.connection.execute(
                "DELETE FROM repos WHERE catalog_generation != ?", (generation,)
            )
            self._remove_orphan_artifacts()
        self.set_metadata("catalog_generation_complete", "true")
        self.connection.execute("DELETE FROM metadata WHERE key = 'catalog_next_url'")
        self.connection.commit()

    def pending_repositories(self, limit: int) -> tuple[PendingRepo, ...]:
        """Return a bounded deterministic batch whose indexed state is stale or absent."""
        if limit <= 0:
            raise ValueError("Pending repository limit must be positive.")
        rows = self.connection.execute(
            "SELECT repo_id, catalog_revision_sha, catalog_gated FROM repos "
            "WHERE eligible = 1 AND (status != 'indexed' "
            "OR revision_sha IS NOT catalog_revision_sha OR gated != catalog_gated) "
            "ORDER BY repo_id LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(
            PendingRepo(
                repo_id=str(row["repo_id"]),
                revision=None
                if row["catalog_revision_sha"] is None
                else str(row["catalog_revision_sha"]),
                gated=bool(row["catalog_gated"]),
            )
            for row in rows
        )

    def pending_repository_count(self) -> int:
        """Return the number of cataloged repositories awaiting inspection."""
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM repos WHERE eligible = 1 AND (status != 'indexed' "
                "OR revision_sha IS NOT catalog_revision_sha OR gated != catalog_gated)"
            ).fetchone()[0]
        )

    def eligible_repository_count(self) -> int:
        """Return the number of cataloged repositories eligible for hash inspection."""
        return int(
            self.connection.execute("SELECT COUNT(*) FROM repos WHERE eligible = 1").fetchone()[0]
        )

    def indexed_repository_count(self) -> int:
        """Return the number of eligible repositories with verified current metadata."""
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM repos WHERE eligible = 1 AND status = 'indexed'"
            ).fetchone()[0]
        )

    def record_inspection_failure(self, repo_id: str) -> None:
        """Record a recoverable inspection failure without disturbing verified occurrences."""
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            "UPDATE repos SET status = 'error', last_scanned_at = ? WHERE repo_id = ?",
            (now, repo_id),
        )
        self.connection.commit()

    def complete_catalog_generation(self) -> None:
        """Mark a fully inspected active generation as the current catalog snapshot."""
        if not self.catalog_generation_complete() or self.pending_repository_count():
            raise RuntimeError("Catalog generation cannot complete while work remains.")
        active = self.get_metadata("active_catalog_generation")
        if active is None:
            raise RuntimeError("No catalog generation is active.")
        self.set_metadata("completed_catalog_generation", active)
        self.connection.execute("DELETE FROM metadata WHERE key = 'active_catalog_generation'")
        self.connection.execute("DELETE FROM metadata WHERE key = 'catalog_generation_complete'")
        self.connection.commit()

    def replace_repo(
        self, inspected: InspectedRepo, candidate_files: tuple[InspectedFile, ...]
    ) -> None:
        """Atomically replace all indexed occurrences for one repository revision."""
        now = datetime.now(UTC).isoformat()
        with self.transaction():
            self.connection.execute(
                "INSERT INTO repos(repo_id, revision_sha, gated, status, last_scanned_at) "
                "VALUES (?, ?, ?, 'indexed', ?) "
                "ON CONFLICT(repo_id) DO UPDATE SET revision_sha = excluded.revision_sha, "
                "gated = excluded.gated, "
                "status = excluded.status, last_scanned_at = excluded.last_scanned_at",
                (inspected.repo_id, inspected.revision, int(inspected.gated), now),
            )
            repo_row = self.connection.execute(
                "SELECT id FROM repos WHERE repo_id = ?", (inspected.repo_id,)
            ).fetchone()
            if repo_row is None:
                raise RuntimeError("Repository row was not persisted.")
            repo_key = int(repo_row["id"])
            self.connection.execute("DELETE FROM occurrences WHERE repo_id_fk = ?", (repo_key,))
            self.connection.execute(
                "DELETE FROM candidate_files_without_sha256 WHERE repo_id_fk = ?", (repo_key,)
            )
            for file in candidate_files:
                if file.content_sha256 is None:
                    self.connection.execute(
                        "INSERT INTO candidate_files_without_sha256 "
                        "(repo_id_fk, revision_sha, path, size, git_blob_oid, xet_hash, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            repo_key,
                            inspected.revision,
                            file.path,
                            file.size,
                            file.git_blob_oid,
                            file.xet_hash,
                            "no_exact_sha256",
                        ),
                    )
                    continue
                self.connection.execute(
                    "INSERT INTO artifacts(sha256, size) VALUES (?, ?) "
                    "ON CONFLICT(sha256) DO UPDATE SET "
                    "size = COALESCE(artifacts.size, excluded.size)",
                    (file.content_sha256, file.size),
                )
                artifact_row = self.connection.execute(
                    "SELECT id FROM artifacts WHERE sha256 = ?", (file.content_sha256,)
                ).fetchone()
                if artifact_row is None:
                    raise RuntimeError("Artifact row was not persisted.")
                self.connection.execute(
                    "INSERT INTO occurrences "
                    "(artifact_id, repo_id_fk, revision_sha, path, git_blob_oid, xet_hash, "
                    "storage, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(artifact_row["id"]),
                        repo_key,
                        inspected.revision,
                        file.path,
                        file.git_blob_oid,
                        file.xet_hash,
                        file.storage,
                        now,
                    ),
                )
            self.connection.execute(
                "DELETE FROM artifacts WHERE id NOT IN "
                "(SELECT DISTINCT artifact_id FROM occurrences)"
            )

    def lookup(self, digest: bytes) -> tuple[Occurrence, ...]:
        """Return all current occurrences for one binary SHA-256 digest."""
        rows = self.connection.execute(
            "SELECT repos.repo_id, repos.gated, occurrences.path, occurrences.revision_sha, "
            "artifacts.size, occurrences.storage "
            "FROM artifacts "
            "JOIN occurrences ON occurrences.artifact_id = artifacts.id "
            "JOIN repos ON repos.id = occurrences.repo_id_fk "
            "WHERE artifacts.sha256 = ? "
            "ORDER BY repos.repo_id, occurrences.path, occurrences.revision_sha",
            (digest,),
        ).fetchall()
        return tuple(
            Occurrence(
                repo_id=str(row["repo_id"]),
                path=str(row["path"]),
                revision=str(row["revision_sha"]),
                size=None if row["size"] is None else int(row["size"]),
                storage=str(row["storage"]),
                gated=bool(row["gated"]),
            )
            for row in rows
        )

    def iter_artifacts(self) -> Iterator[tuple[bytes, tuple[Occurrence, ...]]]:
        """Yield every artifact digest and its sorted occurrences."""
        rows = self.connection.execute("SELECT sha256 FROM artifacts ORDER BY sha256").fetchall()
        for row in rows:
            digest = bytes(row["sha256"])
            yield digest, self.lookup(digest)

    def counts(self) -> dict[str, int]:
        """Return principal state row counts for reports and manifests."""
        return {
            "repos_inspected": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM repos WHERE status = 'indexed'"
                ).fetchone()[0]
            ),
            "unique_hashes": int(
                self.connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            ),
            "occurrences": int(
                self.connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
            ),
            "candidate_without_sha256": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM candidate_files_without_sha256"
                ).fetchone()[0]
            ),
            "inspection_errors": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM repos WHERE status = 'error'"
                ).fetchone()[0]
            ),
        }

    def validate(self) -> None:
        """Raise when SQLite integrity or foreign-key validation fails."""
        quick_check = self.connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_key_rows = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick_check != "ok" or foreign_key_rows:
            raise RuntimeError("SQLite state integrity validation failed.")

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """Run a transaction that rolls back completely on failure."""
        try:
            self.connection.execute("BEGIN")
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _migrate_schema(self) -> None:
        """Add crawl-state columns when opening a database created by an older release."""
        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(repos)").fetchall()
        }
        additions = {
            "catalog_revision_sha": "TEXT",
            "catalog_gated": "INTEGER NOT NULL DEFAULT 0",
            "eligible": "INTEGER NOT NULL DEFAULT 0",
            "catalog_generation": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE repos ADD COLUMN {name} {declaration}")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS repos_pending_catalog "
            "ON repos(eligible, status, catalog_generation)"
        )

    def _record_catalog_repo(self, repo: CatalogRepo, *, eligible: bool, generation: int) -> None:
        """Write one catalog row inside an existing transaction."""
        self.connection.execute(
            "INSERT INTO repos(repo_id, private, catalog_revision_sha, catalog_gated, eligible, "
            "catalog_generation, status, last_scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(repo_id) DO UPDATE SET private = excluded.private, "
            "catalog_revision_sha = excluded.catalog_revision_sha, "
            "catalog_gated = excluded.catalog_gated, eligible = excluded.eligible, "
            "catalog_generation = excluded.catalog_generation, "
            "status = CASE WHEN excluded.eligible = 0 THEN 'excluded' ELSE repos.status END",
            (
                repo.repo_id,
                int(repo.private),
                repo.revision,
                int(repo.gated),
                int(eligible),
                generation,
                "pending" if eligible else "excluded",
            ),
        )
        if not eligible:
            self._remove_repo_records(repo.repo_id)

    def _remove_repo_records(self, repo_id: str) -> None:
        """Delete current occurrences for a cataloged repository that is not eligible."""
        row = self.connection.execute(
            "SELECT id FROM repos WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        if row is None:
            return
        repo_key = int(row["id"])
        self.connection.execute("DELETE FROM occurrences WHERE repo_id_fk = ?", (repo_key,))
        self.connection.execute(
            "DELETE FROM candidate_files_without_sha256 WHERE repo_id_fk = ?", (repo_key,)
        )

    def _remove_orphan_artifacts(self) -> None:
        """Remove digest rows no longer referenced by any current repository occurrence."""
        self.connection.execute(
            "DELETE FROM artifacts WHERE id NOT IN (SELECT DISTINCT artifact_id FROM occurrences)"
        )
