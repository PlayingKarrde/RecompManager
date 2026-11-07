"""Recomp Manager: a self-hosted library server for game recompilations."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("recompmanager")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+dev"
