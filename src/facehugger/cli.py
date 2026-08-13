"""Command-line entry points for public lookup and bounded proof execution."""

import argparse
import json
import os
import sys
from pathlib import Path

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
    proof = commands.add_parser("proof")
    proof.add_argument("--root", type=Path, default=Path.cwd())
    proof.add_argument("--version", required=True)
    proof.add_argument("--catalog-limit", type=int)
    arguments = parser.parse_args()
    try:
        if arguments.command == "lookup":
            return _lookup(arguments)
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
