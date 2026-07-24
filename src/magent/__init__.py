from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("magent-multi-ai-agents-manager")
except PackageNotFoundError:  # source tree without an installed dist
    __version__ = "0.0.0+unknown"
