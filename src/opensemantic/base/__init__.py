from importlib.metadata import PackageNotFoundError, version  # pragma: no cover

try:
    # Change here if project is renamed and does not equal the package name
    dist_name = "opensemantic.base"
    __version__ = version(dist_name)
except PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"
finally:
    del version, PackageNotFoundError

from opensemantic.base._model import *  # noqa

try:
    from opensemantic.base._controller import *  # noqa
except ImportError as _error:  # pragma: no cover - an optional dependency is missing
    # Without this the controllers are simply absent and every lookup returns
    # the pure model, which reads as "no controller registered" rather than as
    # a broken install.
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "opensemantic.base controllers are unavailable: %s", _error
    )
