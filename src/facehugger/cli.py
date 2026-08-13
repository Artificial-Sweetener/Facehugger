"""Command-line entry points for public lookup and bounded proof execution."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from platformdirs import user_cache_dir

from facehugger.client import FacehuggerClient
from facehugger.errors import FacehuggerError
from facehugger.indexer.proof import run_proof


def main() -> int:
    """Run the Facehugger command-line interface."""
    parser = argparse.ArgumentParser(prog="facehugger")
    commands = parser.add_subparsers(dest="command", required=True)
    lookup = commands.add_parser("lookup")
    lookup.add_argument("sha256")
    lookup.add_argument("--base-url", required=True)
    lookup.add_argument("--offline", action="store_true")
    lookup.add_argument("--json", action="store_true")
    cache = commands.add_parser("cache")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_commands.add_parser("clear")
    index = commands.add_parser("index")
    index_commands = index.add_subparsers(dest="index_command", required=True)
    status = index_commands.add_parser("status")
    status.add_argument("--base-url", required=True)
    status.add_argument("--offline", action="store_true")
    proof = commands.add_parser("proof")
    proof.add_argument("--root", type=Path, default=Path.cwd())
    proof.add_argument("--version", required=True)
    proof.add_argument("--catalog-limit", type=int)
    arguments = parser.parse_args()
    try:
        if arguments.command == "lookup":
            return _lookup(arguments)
        if arguments.command == "cache":
            return _clear_cache()
        if arguments.command == "index":
            return _index_status(arguments)
        token = os.environ["HF_TOKEN"]
        run_proof(
            root=arguments.root,
            token=token,
            version=arguments.version,
            catalog_limit=arguments.catalog_limit,
        )
        return 0
    except KeyError:
        print("HF_TOKEN is required for the proof command.", file=sys.stderr)
        return 3
    except FacehuggerError as error:
        print(str(error), file=sys.stderr)
        return 4


def _lookup(arguments: argparse.Namespace) -> int:
    client = FacehuggerClient(base_url=arguments.base_url, offline=arguments.offline)
    result = client.lookup(arguments.sha256)
    if arguments.json:
        print(
            json.dumps(
                {"digest": result.digest, "matches": [match.__dict__ for match in result.matches]}
            )
        )
    else:
        print(f"{len(result.matches)} match(es) for {result.digest}")
        for match in result.matches:
            print(f"{match.repo_id}@{match.revision}:{match.path}")
    return 0 if result.matches else 1


def _clear_cache() -> int:
    """Remove the client cache requested by the user."""
    cache_dir = Path(user_cache_dir("facehugger"))
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    print("Facehugger cache cleared.")
    return 0


def _index_status(arguments: argparse.Namespace) -> int:
    """Print the current index metadata."""
    index = FacehuggerClient(base_url=arguments.base_url, offline=arguments.offline).index_info()
    print(json.dumps(index.__dict__, default=str, sort_keys=True))
    return 0
