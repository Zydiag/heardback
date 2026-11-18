"""Typed maintenance commands, independent of the voice extra."""
import argparse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="heardback")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check prerequisites and show fixes")
    parser.parse_args(argv)
    from .doctor import run
    return run()
