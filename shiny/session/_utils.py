from __future__ import annotations

__all__ = (
    "get_current_session",
    "session_context",
    "require_active_session",
)

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

if TYPE_CHECKING:
    from ._session import Session

from .._docstring import no_example
from .._typing_extensions import TypedDict
from ..module import namespace_context


class RenderedDeps(TypedDict):
    deps: list[dict[str, Any]]
    html: str


# ==============================================================================
# Context manager for current session (AKA current reactive domain)
# ==============================================================================
_current_session: ContextVar[Optional[Session]] = ContextVar(
    "current_session", default=None
)
_default_session: Optional[Session] = None


@no_example()
def get_current_session() -> Optional[Session]:
    """
    Get the current user session.

    Returns
    -------
    :
        The current session if one is active, otherwise ``None``.

    Note
    ----
    Shiny apps should not need to call this function directly. Instead, it is intended to
    be used by Shiny developers who wish to create new functions that should only be
    called from within an active Shiny session.

    See Also
    --------
    * :func:`~shiny.session.require_active_session`
    """
    session = _current_session.get()
    return session if session is not None else _default_session


@contextmanager
def session_context(session: Session | None):
    """
    A context manager for current session.

    Parameters
    ----------
    session
        A :class:`~shiny.Session` instance. If not provided, the instance is inferred via
        :func:`~shiny.session.get_current_session`.
    """
    token: Token[Session | None] = _current_session.set(session)
    try:
        with namespace_context(session.ns if session is not None else None):
            yield
    finally:
        _current_session.reset(token)


@no_example()
def require_active_session(session: Optional[Session]) -> Session:
    """
    Raise an exception if no Shiny session is currently active.

    Parameters
    ----------
    session
        A :class:`~shiny.Session` instance. If not provided, the session is inferred via
        :func:`~shiny.session.get_current_session`.

    Returns
    -------
    :
        The session.

    Note
    ----
    Shiny apps should not need to call this function directly. Instead, it is intended to
    be used by Shiny developers who wish to create new functions that should only be
    called from within an active Shiny session.

    Raises
    ------
    ValueError
        If session is not active.

    See Also
    --------
    * :func:`~shiny.session.get_current_session`
    """

    if session is None:
        session = get_current_session()
    if session is None:
        import inspect

        call_stack = inspect.stack()
        if len(call_stack) > 1:
            caller = call_stack[1]
        else:
            # Uncommon case: this function is called from the top-level, so the caller
            # is just require_active_session.
            caller = call_stack[0]

        calling_fn_name = caller.function
        if calling_fn_name == "__init__":
            # If the caller is __init__, then we're most likely in the initialization of
            # an object. This will get the class name.
            calling_fn_name = caller.frame.f_locals["self"].__class__.__name__

        raise RuntimeError(
            f"{calling_fn_name}() must be called from within an active Shiny session."
        )
    return session


# Ideally I'd love not to limit the types for T, but if I don't, the type checker has
# trouble figuring out what `T` is supposed to be when run_thunk is actually used. For
# now, just keep expanding the possible types, as needed.
T = TypeVar("T", str, int)


def read_thunk(thunk: Callable[[], T] | T) -> T:
    if callable(thunk):
        return thunk()
    else:
        return thunk


def read_thunk_opt(thunk: Optional[Callable[[], T] | T]) -> Optional[T]:
    if thunk is None:
        return None
    elif callable(thunk):
        return thunk()
    else:
        return thunk
