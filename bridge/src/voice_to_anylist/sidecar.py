"""Launch the Node sidecar with the environment the bridge itself reads.

The two runtimes share one .env, and originally each parsed it: the bridge
through python-dotenv, the sidecar through Node's own ``--env-file``.  They
disagree about an unquoted ``#``.  Node reads it as the start of a comment and
silently truncates the value; python-dotenv keeps it.  So an AnyList password
containing a ``#`` arrived two characters short and AnyList answered with a
bare 401 naming no cause, while the same file authenticated Google fine.

Quoting the value would fix that instance.  One parser fixes the class, which
is what this does: read the file once, hand Node a plain environment, and let
``--env-file`` alone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from .config import default_state_dir


def build_env(base: dict[str, str], env_file: Path) -> dict[str, str]:
    """`base` plus anything `env_file` adds, without overriding `base`.

    The real environment wins because the launchd job sets deployment facts
    there -- ports, paths -- while .env carries credentials.  That is also the
    precedence pydantic-settings applies on the Python side, so the two
    processes resolve a given name the same way.
    """
    env = dict(base)
    if not env_file.exists():
        return env
    for key, value in dotenv_values(env_file).items():
        if value is not None and key not in env:
            env[key] = value
    return env


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 3:
        sys.exit(f"usage: {Path(argv[0]).name} <node-binary> <server.js> [args...]")

    node, script, extra = argv[1], argv[2], argv[3:]
    env = build_env(dict(os.environ), default_state_dir() / ".env")
    os.execve(node, [node, script, *extra], env)


if __name__ == "__main__":
    raise SystemExit(main())
