import asyncio
import json
import logging
import os
import sys
import traceback
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

from lsprotocol import types
from lsprotocol.converters import get_converter
from pygls.exceptions import JsonRpcException
from pygls.exceptions import PyglsError
from pygls.lsp.client import BaseLanguageClient
from pygls.protocol import default_converter

from .protocol import LanguageClientProtocol

if sys.version_info.minor < 9:
    import importlib_resources as resources
else:
    import importlib.resources as resources  # type: ignore[no-redef]


__version__ = "0.3.0"
logger = logging.getLogger(__name__)


class LanguageClient(BaseLanguageClient):
    """Used to drive language servers under test."""

    protocol: LanguageClientProtocol

    def __init__(self, *args, **kwargs):
        if "protocol_cls" not in kwargs:
            kwargs["protocol_cls"] = LanguageClientProtocol

        super().__init__("pytest-lsp-client", __version__, *args, **kwargs)

        self.capabilities: Optional[types.ClientCapabilities] = None
        """The client's capabilities."""

        self.shown_documents: List[types.ShowDocumentParams] = []
        """Used to keep track of the documents requested to be shown via a
        ``window/showDocument`` request."""

        self.messages: List[types.ShowMessageParams] = []
        """Holds any received ``window/showMessage`` requests."""

        self.log_messages: List[types.LogMessageParams] = []
        """Holds any received ``window/logMessage`` requests."""

        self.diagnostics: Dict[str, List[types.Diagnostic]] = {}
        """Used to hold any recieved diagnostics."""

        self.error: Optional[Exception] = None
        """Indicates if the client encountered an error."""

        self._setup_log_index = 0
        """Used to keep track of which log messages occurred during startup."""

        self._last_log_index = 0
        """Used to keep track of which log messages correspond with which test case."""

    async def server_exit(self, server: asyncio.subprocess.Process):
        """Called when the server process exits."""
        logger.debug("Server process exited with code: %s", server.returncode)

        if self._stop_event.is_set():
            return

        stderr = ""
        if server.stderr is not None:
            stderr_bytes = await server.stderr.read()
            stderr = stderr_bytes.decode("utf8")

        loop = asyncio.get_running_loop()
        loop.call_soon(
            cancel_all_tasks,
            f"Server process exited with return code: {server.returncode}\n{stderr}",
        )

    def report_server_error(
        self, error: Exception, source: Union[PyglsError, JsonRpcException]
    ):
        """Called when the server does something unexpected, e.g. sending malformed
        JSON."""
        self.error = error
        tb = "".join(traceback.format_exc())

        message = f"{source.__name__}: {error}\n{tb}"  # type: ignore

        loop = asyncio.get_running_loop()
        loop.call_soon(cancel_all_tasks, message)

        if self._stop_event:
            self._stop_event.set()

    async def initialize_session(
        self, params: types.InitializeParams
    ) -> types.InitializeResult:
        """Make an ``initialize`` request to a lanaguage server.

        It will also automatically send an ``initialized`` notification once
        the server responds.

        Parameters
        ----------
        params
           The parameters to send to the client.

           The following fields will be automatically set if left blank.

           - ``process_id``: Set to the PID of the current process.

        Returns
        -------
        InitializeResult
           The result received from the client.
        """
        self.capabilities = params.capabilities

        if params.process_id is None:
            params.process_id = os.getpid()

        response = await self.initialize_async(params)
        self.initialized(types.InitializedParams())

        return response

    async def shutdown_session(self) -> None:
        """Shutdown the server under test.

        Helper method that handles sending ``shutdown`` and ``exit`` messages in the
        correct order.

        .. note::

           This method will not attempt to send these messages if a fatal error has
           occurred.

        """
        if self.error is not None or self.capabilities is None:
            return

        await self.shutdown_async(None)
        self.exit(None)

    async def wait_for_notification(self, method: str):
        """Block until a notification with the given method is received.

        Parameters
        ----------
        method
           The notification method to wait for, e.g. ``textDocument/publishDiagnostics``
        """
        return await self.protocol.wait_for_notification_async(method)


def cancel_all_tasks(message: str):
    """Called to cancel all awaited tasks."""

    for task in asyncio.all_tasks():
        if sys.version_info.minor < 9:
            task.cancel()
        else:
            task.cancel(message)


def make_test_lsp_client() -> LanguageClient:
    """Construct a new test client instance with the handlers needed to capture
    additional responses from the server."""

    client = LanguageClient(
        converter_factory=default_converter,
    )

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def publish_diagnostics(
        client: LanguageClient, params: types.PublishDiagnosticsParams
    ):
        client.diagnostics[params.uri] = params.diagnostics

    @client.feature(types.WINDOW_LOG_MESSAGE)
    def log_message(client: LanguageClient, params: types.LogMessageParams):
        client.log_messages.append(params)

        levels = [logger.error, logger.warning, logger.info, logger.debug]
        levels[params.type.value - 1](params.message)

    @client.feature(types.WINDOW_SHOW_MESSAGE)
    def show_message(client: LanguageClient, params):
        client.messages.append(params)

    @client.feature(types.WINDOW_SHOW_DOCUMENT)
    def show_document(
        client: LanguageClient, params: types.ShowDocumentParams
    ) -> types.ShowDocumentResult:
        client.shown_documents.append(params)
        return types.ShowDocumentResult(success=True)

    return client


def client_capabilities(client_spec: str) -> types.ClientCapabilities:
    """Find the capabilities that correspond to the given client spec.

    Parameters
    ----------
    client_spec
       A string describing the client to load the corresponding
       capabilities for.
    """

    # Currently, we only have a single version of each client so let's just return the
    # first one we find.
    #
    # TODO: Implement support for client@x.y.z
    # TODO: Implement support for client@latest?
    filename = None
    for resource in resources.files("pytest_lsp.clients").iterdir():
        # Skip the README or any other files that we don't care about.
        if not resource.name.endswith(".json"):
            continue

        if resource.name.startswith(client_spec.replace("-", "_")):
            filename = resource
            break

    if not filename:
        raise ValueError(f"Unknown client: '{client_spec}'")

    converter = get_converter()
    capabilities = json.loads(filename.read_text())
    return converter.structure(capabilities, types.ClientCapabilities)