################################################################################
# Copyright (c) 2017-2018, National Research Foundation (Square Kilometre Array)
#
# Licensed under the BSD 3-Clause License (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy
# of the License at
#
#   https://opensource.org/licenses/BSD-3-Clause
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

"""A store of chunks (i.e. N-dimensional arrays) based on the Amazon S3 API.

It does not support S3 authentication/signatures, relying instead on external
code to provide HTTP authentication.
"""

import contextlib
import io
import threading
import Queue
import sys
import urlparse
import urllib
import hashlib
import base64
import warnings
import xml.etree.cElementTree

import numpy as np
try:
    import requests
    from requests.adapters import HTTPAdapter as _HTTPAdapter
except ImportError as e:
    requests = None
    _HTTPAdapter = object
    _requests_import_error = e

from .chunkstore import ChunkStore, StoreUnavailable, ChunkNotFound, BadChunk


class _TimeoutHTTPAdapter(_HTTPAdapter):
    """Allow an HTTPAdapter to have a default timeout"""
    def __init__(self, *args, **kwargs):
        self._default_timeout = kwargs.pop('timeout', None)
        super(_TimeoutHTTPAdapter, self).__init__(*args, **kwargs)

    def send(self, request, stream=False, timeout=None, *args, **kwargs):
        if timeout is None:
            timeout = self._default_timeout
        return super(_TimeoutHTTPAdapter, self).send(request, stream, timeout, *args, **kwargs)


def _raise_for_status(response):
    """Like :meth:`requests.Response.raise_for_status`, but uses ChunkStore exception types."""
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        if response.status_code == 404:
            raise ChunkNotFound(str(error))
        else:
            raise StoreUnavailable(str(error))


class S3ChunkStore(ChunkStore):
    """A store of chunks (i.e. N-dimensional arrays) based on the Amazon S3 API.

    This object encapsulates the S3 client / session and its underlying
    connection pool, which allows subsequent get and put calls to share the
    connections.

    The full identifier of each chunk (the "chunk name") is given by

      "<bucket>/<path>/<idx>"

    where "<bucket>" refers to the relevant S3 bucket, "<bucket>/<path>" is
    the name of the parent array of the chunk and "<idx>" is the index string
    of each chunk (e.g. "00001_00512"). The corresponding S3 key string of
    a chunk is "<path>/<idx>.npy" which reflects the fact that the chunk is
    stored as a string representation of an NPY file (complete with header).

    Parameters
    ----------
    session : :class:`requests.Session` object
        Pre-configured session
    url : str
        Base URL for the S3 service

    Raises
    ------
    ImportError
        If requests is not installed (it's an optional dependency otherwise)
    """

    def __init__(self, session, url):
        if not requests:
            raise _requests_import_error
        try:
            # Quick smoke test to see if the S3 server is available
            response = session.get(url)   # Lists buckets
            _raise_for_status(response)
        except requests.exceptions.RequestException as error:
            raise StoreUnavailable(str(error))

        error_map = {requests.exceptions.RequestException: StoreUnavailable,
                     xml.etree.cElementTree.ParseError: StoreUnavailable}
        super(S3ChunkStore, self).__init__(error_map)
        self._session = session
        self._url = url

    @classmethod
    def _from_url(cls, url, timeout, **kwargs):
        """Construct S3 chunk store from endpoint URL (see :meth:`from_url`)."""
        if not requests:
            raise ImportError('Please install requests for katdal S3 support')

        session = requests.Session()
        adapter = _TimeoutHTTPAdapter(pool_maxsize=200, max_retries=2, timeout=timeout)
        session.mount(url, adapter)
        if kwargs:
            warnings.warn('Ignoring unknown parameters {}'.format(kwargs.keys()))
        return cls(session, url)

    @classmethod
    def from_url(cls, url, timeout=10, extra_timeout=1, **kwargs):
        """Construct S3 chunk store from endpoint URL.

        Parameters
        ----------
        url : string
            Endpoint of S3 service, e.g. 'http://127.0.0.1:9000'
        timeout : int or float, optional
            Read / connect timeout, in seconds (set to None to leave unchanged)
        extra_timeout : int or float, optional
            Additional timeout, useful to terminate e.g. slow DNS lookups
            without masking read / connect errors (ignored if `timeout` is None)
        kwargs : dict
            Extra keyword arguments: config settings or create_client arguments

        Raises
        ------
        ImportError
            If requests is not installed (it's an optional dependency otherwise)
        :exc:`chunkstore.StoreUnavailable`
            If S3 server interaction failed (it's down, no authentication, etc)
        """
        # XXX This is a poor man's attempt at concurrent.futures functionality
        # (avoiding extra dependency on Python 2, revisit when Python 3 only)
        queue = Queue.Queue()

        def _from_url(url, timeout, **kwargs):
            """Construct chunk store and return it (or exception) via queue."""
            try:
                queue.put(cls._from_url(url, timeout, **kwargs))
            except BaseException:
                queue.put(sys.exc_info())

        thread = threading.Thread(target=_from_url, args=(url, timeout),
                                  kwargs=kwargs)
        thread.daemon = True
        thread.start()
        if timeout is not None:
            timeout += extra_timeout
        try:
            result = queue.get(timeout=timeout)
        except Queue.Empty:
            hostname = urlparse.urlparse(url).hostname
            raise StoreUnavailable('Timed out, possibly due to DNS lookup '
                                   'of {} stalling'.format(hostname))
        else:
            if isinstance(result, cls):
                return result
            else:
                # Assume result is (exception type, exception value, traceback)
                raise result[0], result[1], result[2]

    def _chunk_url(self, chunk_name):
        return urlparse.urljoin(self._url, urllib.quote(chunk_name + '.npy'))

    def get_chunk(self, array_name, slices, dtype):
        """See the docstring of :meth:`ChunkStore.get_chunk`."""
        dtype = np.dtype(dtype)
        chunk_name, shape = self.chunk_metadata(array_name, slices, dtype=dtype)
        url = self._chunk_url(chunk_name)
        with self._standard_errors(chunk_name):
            response = self._session.get(url)
            _raise_for_status(response)
            data = response.content
        chunk = np.lib.format.read_array(io.BytesIO(data), allow_pickle=False)
        if chunk.shape != shape or chunk.dtype != dtype:
            raise BadChunk('Chunk {!r}: dtype {} and/or shape {} in store '
                           'differs from expected dtype {} and shape {}'
                           .format(chunk_name, chunk.dtype, chunk.shape,
                                   dtype, shape))
        return chunk

    def put_chunk(self, array_name, slices, chunk):
        """See the docstring of :meth:`ChunkStore.put_chunk`."""
        chunk_name, _ = self.chunk_metadata(array_name, slices, chunk=chunk)
        url = self._chunk_url(chunk_name)
        fp = io.BytesIO()
        np.lib.format.write_array(fp, chunk, allow_pickle=False)
        md5 = base64.b64encode(hashlib.md5(fp.getvalue()).digest())
        fp.seek(0)
        with self._standard_errors(chunk_name):
            response = self._session.put(
                url,
                headers={'Content-MD5': md5},
                data=fp)

    def has_chunk(self, array_name, slices, dtype):
        """See the docstring of :meth:`ChunkStore.has_chunk`."""
        dtype = np.dtype(dtype)
        chunk_name, _ = self.chunk_metadata(array_name, slices, dtype=dtype)
        url = self._chunk_url(chunk_name)
        try:
            with self._standard_errors(chunk_name):
                response = self._session.head(url)
                _raise_for_status(response)
        except ChunkNotFound:
            return False
        else:
            return True

    list_max_keys = 100000

    def list_chunk_ids(self, array_name):
        """See the docstring of :meth:`ChunkStore.list_chunk_ids`."""
        NS = '{http://s3.amazonaws.com/doc/2006-03-01/}'
        bucket, prefix = self.split(array_name, 1)
        url = urlparse.urljoin(self._url, urllib.quote(bucket))
        params = {
            'prefix': prefix,
            'max-keys': self.list_max_keys
        }

        keys = []
        more = True
        while more:
            with self._standard_errors():
                response = self._session.get(url, params=params)
                _raise_for_status(response)
                root = xml.etree.cElementTree.fromstring(response.content)
                keys.extend(child.text for child in root.iter(NS + 'Key'))
                truncated = root.find(NS + 'IsTruncated')
                more = (truncated is not None and truncated.text == 'true')
                if more:
                    next_marker = root.find(NS + 'NextMarker')
                    if next_marker:
                        params['marker'] = next_marker.text
                    elif keys:
                        params['marker'] = keys[-1]
                    else:
                        warnings.warn('Result had no keys but was marked as truncated')
                        more = False
        # Strip the array name and .npy extension to get the chunk ID string
        return [key[len(prefix) + 1:-4] for key in keys if key.endswith('.npy')]

    get_chunk.__doc__ = ChunkStore.get_chunk.__doc__
    put_chunk.__doc__ = ChunkStore.put_chunk.__doc__
    has_chunk.__doc__ = ChunkStore.has_chunk.__doc__
    list_chunk_ids.__doc__ = ChunkStore.list_chunk_ids.__doc__
