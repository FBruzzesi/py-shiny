from __future__ import annotations

import asyncio
import html
import http
import logging
import os
import secrets
import threading
import webbrowser
from typing import TYPE_CHECKING, Callable, Optional, cast

if TYPE_CHECKING:
    import starlette.types
    from asgiref.typing import (
        ASGI3Application,
        ASGIReceiveCallable,
        ASGISendCallable,
        ASGISendEvent,
        HTTPResponseStartEvent,
        Scope,
    )

from ._hostenv import get_proxy_url

# CHILD PROCESS ------------------------------------------------------------


def autoreload_url() -> Optional[str]:
    port = os.getenv("SHINY_AUTORELOAD_PORT")
    if not port:
        return None
    else:
        return get_proxy_url(f"ws://127.0.0.1:{port}/autoreload")


# Instantiated by Uvicorn in worker process when reload is enabled
class HotReloadHandler(logging.Handler):
    """Uvicorn log reader, detects reload events."""

    def __init__(self):
        logging.Handler.__init__(self)

    def emit(self, record: logging.LogRecord) -> None:
        # https://github.com/encode/uvicorn/blob/266db48888f3f1ba56710a49ec82e12eecde3aa3/uvicorn/supervisors/statreload.py#L46
        # https://github.com/encode/uvicorn/blob/266db48888f3f1ba56710a49ec82e12eecde3aa3/uvicorn/supervisors/watchgodreload.py#L148
        if "Reloading..." in record.getMessage():
            reload_begin()
        # https://github.com/encode/uvicorn/blob/926a8f5dc2c9265d5fb5daaafce13878f477e264/uvicorn/lifespan/on.py#L59
        elif "Application startup complete." in record.getMessage():
            reload_end()


# Called from child process when old application instance is shut down
def reload_begin():
    pass


# Called from child process when new application instance starts up
def reload_end():
    import websockets
    import websockets.asyncio.client

    # os.kill(os.getppid(), signal.SIGUSR1)

    port = os.getenv("SHINY_AUTORELOAD_PORT")
    if not port:
        return None

    url = f"ws://127.0.0.1:{port}/notify"

    async def _() -> None:
        options = {
            "additional_headers": {
                "Shiny-Autoreload-Secret": os.getenv("SHINY_AUTORELOAD_SECRET", ""),
            }
        }
        try:
            async with websockets.asyncio.client.connect(
                url, **options  # pyright: ignore[reportArgumentType]
            ) as websocket:
                await websocket.send("reload_end")
        except websockets.exceptions.ConnectionClosed:
            pass

    asyncio.create_task(_())


class InjectAutoreloadMiddleware:
    """Inserts shiny-autoreload.js into the head.

    It's necessary to do it using middleware instead of in a nice htmldependency,
    because we want autoreload to be effective even when displaying an error page.
    """

    def __init__(
        self,
        app: starlette.types.ASGIApp | ASGI3Application,
        *args: object,
        **kwargs: object,
    ):
        if len(args) > 0 or len(kwargs) > 0:
            raise TypeError(
                f"InjectAutoreloadMiddleware does not support positional or keyword arguments, received {args}, {kwargs}"
            )
        # The starlette types and the asgiref types are compatible, but we'll use the
        # latter internally. See the note in the __call__ method for more details.
        if TYPE_CHECKING:
            app = cast(ASGI3Application, app)
        self.app = app
        ws_url = autoreload_url()
        self.script = (
            f"""  <script src="__shared/shiny-autoreload.js" data-ws-url="{html.escape(ws_url)}"></script>
</head>""".encode(
                "ascii"
            )
            if ws_url
            else bytes()
        )

    async def __call__(
        self,
        scope: starlette.types.Scope | Scope,
        receive: starlette.types.Receive | ASGIReceiveCallable,
        send: starlette.types.Send | ASGISendCallable,
    ) -> None:
        # The starlette types and the asgiref types are compatible, but the latter are
        # more rigorous. In the call interface, we accept both types for compatibility
        # with both. But internally we'll use the more rigorous types.
        # See https://github.com/encode/starlette/blob/39dccd9/docs/middleware.md#type-annotations
        if TYPE_CHECKING:
            scope = cast(Scope, scope)
            receive = cast(ASGIReceiveCallable, receive)
            send = cast(ASGISendCallable, send)

        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] != "/" or len(self.script) == 0:
            return await self.app(scope, receive, send)

        def mangle_callback(body: bytes) -> tuple[bytes, bool]:
            if b"</head>" in body:
                return (body.replace(b"</head>", self.script, 1), True)
            else:
                return (body, False)

        mangler = ResponseMangler(send, mangle_callback)
        await self.app(scope, receive, mangler.send)


# PARENT PROCESS ------------------------------------------------------------


def start_server(port: int, app_port: int, launch_browser: bool):
    """Starts a websocket server that listens on its own port (separate from the main
    Shiny listener).

    Clients can connect on either the /autoreload or /notify path.

    Clients from the uvicorn worker process connect to the /notify path to notify us
    that a successful startup or reload has occurred.

    Clients from browsers (on localhost only) connect to the /autoreload path to be
    notified when a successful startup or reload has occurred.
    """

    # Store port and secret in environment variables so they are inherited by uvicorn
    # worker processes.
    secret = secrets.token_hex(32)
    os.environ["SHINY_AUTORELOAD_PORT"] = str(port)
    os.environ["SHINY_AUTORELOAD_SECRET"] = secret

    # websockets 14.0 (and presumably later) log an error if a connection is opened and
    # closed before any data is sent. Our VS Code extension does exactly this--opens a
    # connection to check if the server is running, then closes it. It's better that it
    # does this and doesn't actually perform an HTTP request because we can't guarantee
    # that the HTTP request will be cheap (we do the same ping on both the autoreload
    # socket and the main uvicorn socket). So better to just suppress all errors until
    # we think we have a problem. You can unsuppress by setting the environment variable
    # to DEBUG.
    loglevel = os.getenv("SHINY_AUTORELOAD_LOG_LEVEL", "CRITICAL")
    logging.getLogger("websockets").setLevel(loglevel)

    app_url = get_proxy_url(f"http://127.0.0.1:{app_port}/")

    # Run on a background thread so our event loop doesn't interfere with uvicorn.
    # Set daemon=True because we don't want to keep the process alive with this thread.
    threading.Thread(
        None, _thread_main, args=[port, app_url, secret, launch_browser], daemon=True
    ).start()


def _thread_main(port: int, app_url: str, secret: str, launch_browser: bool):
    asyncio.run(_coro_main(port, app_url, secret, launch_browser))


async def _coro_main(
    port: int, app_url: str, secret: str, launch_browser: bool
) -> None:
    import websockets
    import websockets.asyncio.server
    from websockets.http11 import Request, Response

    reload_now: asyncio.Event = asyncio.Event()

    def nudge():
        nonlocal launch_browser
        if launch_browser:
            # Only launch the browser once, not every time autoreload occurs
            launch_browser = False
            webbrowser.open(app_url, 1)
        reload_now.set()
        reload_now.clear()

    async def reload_server(conn: websockets.asyncio.server.ServerConnection):
        try:
            if conn.request is None:
                raise RuntimeError(
                    "Autoreload server received a connection with no request"
                )
            elif conn.request.path == "/autoreload":
                # The client wants to be notified when the app has reloaded. The client
                # in this case is the web browser, specifically shiny-autoreload.js.
                while True:
                    await reload_now.wait()
                    await conn.send("autoreload")
            elif conn.request.path == "/notify":
                # The client is notifying us that the app has reloaded. The client in
                # this case is the uvicorn worker process (see reload_end(), above).
                req_secret = conn.request.headers.get("Shiny-Autoreload-Secret", "")
                if req_secret != secret:
                    # The client couldn't prove that they were from a child process
                    return
                data = await conn.recv()
                if isinstance(data, str) and data == "reload_end":
                    nudge()
        except websockets.exceptions.ConnectionClosed:
            pass

    # Handle non-WebSocket requests to the autoreload HTTP server. Without this, if you
    # happen to visit the autoreload endpoint in a browser, you get an error message
    # about only WebSockets being supported. This is not an academic problem as the
    # VSCode extension used in RSW sniffs out ports that are being listened on, which
    # leads to confusion if all you get is an error.
    async def process_request(
        connection: websockets.asyncio.server.ServerConnection,
        request: Request,
    ) -> Response | None:
        if request.headers.get("Upgrade") is None:
            return Response(
                status_code=http.HTTPStatus.MOVED_PERMANENTLY,
                reason_phrase="Moved Permanently",
                headers=websockets.Headers(Location=app_url),
            )
        else:
            return None

    async with websockets.asyncio.server.serve(
        reload_server, "127.0.0.1", port, process_request=process_request
    ):
        await asyncio.Future()  # wait forever


class ResponseMangler:
    """A class that assists with intercepting and rewriting response bodies being sent
    over ASGI. This would be easy if not for 1) response bodies are potentially sent in
    chunks, over multiple events; 2) the first response event we receive is the one that
    contains the Content-Length, which can be affected when we do rewriting later on.
    The ResponseMangler handles the buffering and content-length rewriting, leaving the
    caller to only have to worry about the actual body-modifying logic.
    """

    def __init__(
        self, send: ASGISendCallable, mangler: Callable[[bytes], tuple[bytes, bool]]
    ) -> None:
        # The underlying ASGI send function
        self._send = send
        # The caller-provided logic for rewriting the body. Takes a single `bytes`
        # argument that is _all_ of the body bytes seen _so far_, and returns a tuple of
        # (bytes, bool) where the bytes are the (possibly modified) body bytes and the
        # bool is True if the mangler does not care to see any more data.
        self._mangler = mangler

        # If True, the mangler is done and any further data can simply be passed along
        self._done: bool = False

        # Holds the http.response.start event, which may need its Content-Length header
        # rewritten before we send it
        self._response_start: Optional[HTTPResponseStartEvent] = None
        # All the response body bytes we have seen so far
        self._body: bytes = b""

    async def send(self, event: ASGISendEvent) -> None:
        if self._done:
            await self._send(event)
            return

        if event["type"] == "http.response.start":
            self._response_start = event
        elif event["type"] == "http.response.body":
            # This check is mostly to make pyright happy
            if self._response_start is None:
                raise AssertionError(
                    "http.response.body ASGI event sent before http.response.start"
                )

            # Add the newly received body data to what we've seen already
            self._body += event["body"]
            # Snapshot length before we mess with the body
            old_len = len(self._body)
            # Mangle away! If done is True, the mangler doesn't want to do any further
            # mangling.
            self._body, done = self._mangler(self._body)

            new_len = len(self._body)
            if new_len != old_len:
                # The mangling check changed the length of the body. Add the difference
                # to the content-length header (if content-length is even present)
                _add_to_content_length(self._response_start, new_len - old_len)

            more_body = event.get("more_body", False)

            if done or not more_body:
                # Either we've seen the whole body by now (`not more_body`) or the
                # mangler has seen all the data it cares to (`done`). Either way, we can
                # send all the data we have.
                self._done = True
                await self._send(self._response_start)
                await self._send(
                    {
                        "type": "http.response.body",
                        "body": self._body,
                        "more_body": more_body,
                    }
                )
                # Allow gc
                self._response_start = None
                self._body = b""
            else:
                # If we get here, then the mangler isn't done and we are expecting to
                # see more data. Do nothing.
                pass


def _add_to_content_length(event: HTTPResponseStartEvent, offset: int) -> None:
    """If event has a Content-Length header, add the specified number of bytes to it
    (may be negative)"""
    event["headers"] = [
        (
            (name, str(int(value) + offset).encode("latin-1"))
            if name.decode("ascii").lower() == "content-length"
            else (name, value)
        )
        for (name, value) in event["headers"]
    ]
