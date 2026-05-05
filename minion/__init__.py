try:
    from importlib.metadata import version as _version, PackageNotFoundError
    __version__ = _version("minion-cli")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
