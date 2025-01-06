# Copyright 2021, New York University and the TUF contributors
# SPDX-License-Identifier: MIT OR Apache-2.0

"""Provides an implementation of ``FetcherInterface`` using the urllib3 HTTP
library.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib import parse

# Imports
import urllib3

import tuf
from tuf.api import exceptions
from tuf.ngclient.fetcher import FetcherInterface

if TYPE_CHECKING:
    from collections.abc import Iterator

# Globals
logger = logging.getLogger(__name__)


# Classes
class Urllib3Fetcher(FetcherInterface):
    """An implementation of ``FetcherInterface`` based on the urllib3 library.

    Attributes:
        socket_timeout: Timeout in seconds, used for both initial connection
            delay and the maximum delay between bytes received.
        chunk_size: Chunk size in bytes used when downloading.
    """

    def __init__(
        self,
        socket_timeout: int = 30,
        chunk_size: int = 400000,
        app_user_agent: str | None = None,
    ) -> None:
        # NOTE: We use a separate urllib3.PoolManager per scheme+hostname
        # combination, in order to reuse connections to the same hostname to
        # improve efficiency, but avoiding sharing state between different
        # hosts-scheme combinations to minimize subtle security issues.
        # Some cookies may not be HTTP-safe.
        self._poolManagers: dict[tuple[str, str], urllib3.PoolManager] = {}

        # Default settings
        self.socket_timeout: int = socket_timeout  # seconds
        self.chunk_size: int = chunk_size  # bytes
        self.app_user_agent = app_user_agent

    def _fetch(self, url: str) -> Iterator[bytes]:
        """Fetch the contents of HTTP/HTTPS url from a remote server.

        Args:
            url: URL string that represents a file location.

        Raises:
            exceptions.SlowRetrievalError: Timeout occurs while receiving
                data.
            exceptions.DownloadHTTPError: HTTP error code is received.

        Returns:
            Bytes iterator
        """
        # Get a customized session for each new schema+hostname combination.
        poolmanager = self._get_poolmanager(url)

        # Get the urllib3.PoolManager object for this URL.
        #
        # Defer downloading the response body with preload_content=False.
        # Always set the timeout. This timeout value is interpreted by
        # urllib3 as:
        #  - connect timeout (max delay before first byte is received)
        #  - read (gap) timeout (max delay between bytes received)
        try:
            response = poolmanager.request("GET",
                url, preload_content=False,
                timeout=urllib3.Timeout(connect=self.socket_timeout)
            )
        except urllib3.exceptions.TimeoutError as e:
            raise exceptions.SlowRetrievalError from e

        # Check response status.
        try:
           if response.status >= 400:
               raise urllib3.exceptions.HTTPError
        except urllib3.exceptions.HTTPError as e:
            response.close()
            status = response.status
            raise exceptions.DownloadHTTPError(str(e), status) from e

        return self._chunks(response)

    def _chunks(
        self, response: urllib3.response.HTTPResponse
    ) -> Iterator[bytes]:
        """A generator function to be returned by fetch.

        This way the caller of fetch can differentiate between connection
        and actual data download.
        """

        try:
            yield from response.stream(self.chunk_size)
        except (
            urllib3.exceptions.ConnectionError,
            urllib3.exceptions.TimeoutError,
        ) as e:
            raise exceptions.SlowRetrievalError from e

        finally:
            response.close()

    def _get_poolmanager(self, url: str) -> urllib3.PoolManager:
        """Return a different customized urllib3.PoolManager per schema+hostname
        combination.

        Raises:
            exceptions.DownloadError: When there is a problem parsing the url.
        """
        # Use a different urllib3.PoolManager per schema+hostname
        # combination, to reuse connections while minimizing subtle
        # security issues.
        parsed_url = parse.urlparse(url)

        if not parsed_url.scheme:
            raise exceptions.DownloadError(f"Failed to parse URL {url}")

        poolmanager_index = (parsed_url.scheme, parsed_url.hostname or "")
        poolmanager = self._poolManagers.get(poolmanager_index)

        if not poolmanager:
            # no default User-Agent when creating a poolManager
            ua = f"python-tuf/{tuf.__version__}"
            if self.app_user_agent is not None:
                ua = f"{self.app_user_agent} {ua}"

            poolmanager = urllib3.PoolManager(headers={"User-Agent" : ua})
            self._poolManagers[poolmanager_index] = poolmanager

            logger.debug("Made new poolManager %s", poolmanager_index)
        else:
            logger.debug("Reusing poolManager %s", poolmanager_index)

        return poolmanager
