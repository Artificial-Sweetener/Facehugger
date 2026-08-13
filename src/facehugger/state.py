"""SQLite state and reverse-index operations."""

import sqlite3
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from facehugger.models import InspectedFile, InspectedRepo, Occurrence

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
                self.connection.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
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
