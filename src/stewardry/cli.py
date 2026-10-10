"""Command-line entry point. Commands are added as milestone steps land."""

import argparse
from collections.abc import Sequence

from stewardry import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stewardry")
    parser.add_argument("--version", action="version", version=f"stewardry {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
