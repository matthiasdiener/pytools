from __future__ import annotations


"""Generic persistent, concurrent dictionary-like facility."""


__copyright__ = """
Copyright (C) 2011,2014 Andreas Kloeckner
Copyright (C) 2017 Matt Wala
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import hashlib
import logging
import os
import pickle
import sqlite3
import sys
from dataclasses import fields as dc_fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Generator, Mapping, Optional, Protocol


if TYPE_CHECKING:
    from _typeshed import ReadableBuffer
    from typing_extensions import Self

try:
    import attrs
except ModuleNotFoundError:
    _HAS_ATTRS = False
else:
    _HAS_ATTRS = True


logger = logging.getLogger(__name__)

__doc__ = """
Persistent Hashing and Persistent Dictionaries
==============================================

This module contains functionality that allows hashing with keys that remain
valid across interpreter invocations, unlike Python's built-in hashes.

This module also provides a disk-backed dictionary that uses persistent hashing.

.. autoexception:: NoSuchEntryError
.. autoexception:: ReadOnlyEntryError

.. autoclass:: Hash
.. autoclass:: KeyBuilder
.. autoclass:: PersistentDict
.. autoclass:: WriteOncePersistentDict
"""


# {{{ key generation

class Hash(Protocol):
    """A protocol for the hashes from :mod:`hashlib`.

    .. automethod:: update
    .. automethod:: digest
    .. automethod:: hexdigest
    .. automethod:: copy
    """
    def update(self, data: ReadableBuffer) -> None:
        ...

    def digest(self) -> bytes:
        ...

    def hexdigest(self) -> str:
        ...

    def copy(self) -> Self:
        ...


class KeyBuilder:
    """A (stateless) object that computes persistent hashes of objects fed to it.
    Subclassing this class permits customizing the computation of hash keys.

    This class follows the same general rules as Python's built-in hashing:

    - Only immutable objects can be hashed.
    - If two objects compare equal, they must hash to the same value.
    - Objects with the same hash may or may not compare equal.

    In addition, hashes computed with :class:`KeyBuilder` have the following
    properties:

    - The hash is persistent across interpreter invocations.
    - The hash is the same across different Python versions and platforms.
    - The hash is invariant with respect to :envvar:`PYTHONHASHSEED`.
    - Hashes are computed using functionality from :mod:`hashlib`.

    Key builders of this type are used by :class:`PersistentDict`, but
    other uses are entirely allowable.

    .. automethod:: __call__
    .. automethod:: rec
    .. staticmethod:: new_hash()

        Return a new hash instance following the protocol of the ones
        from :mod:`hashlib`. This will permit switching to different
        hash algorithms in the future. Subclasses are expected to use
        this to create new hashes. Not doing so is deprecated and
        may stop working as early as 2022.

        .. versionadded:: 2021.2
    """

    # this exists so that we can (conceivably) switch algorithms at some point
    # down the road
    new_hash = hashlib.sha256

    def rec(self, key_hash: Hash, key: Any) -> Hash:
        """
        :arg key_hash: the hash object to be updated with the hash of *key*.
        :arg key: the (immutable) Python object to be hashed.
        :returns: the updated *key_hash*

        .. versionchanged:: 2021.2

            Now returns the updated *key_hash*.
        """

        digest = getattr(key, "_pytools_persistent_hash_digest", None)

        if digest is None and not isinstance(key, type):
            try:
                method = key.update_persistent_hash
            except AttributeError:
                pass
            else:
                inner_key_hash = self.new_hash()
                method(inner_key_hash, self)
                digest = inner_key_hash.digest()

        if digest is None:
            tp = type(key)
            tname = tp.__name__
            method = None
            try:
                method = getattr(self, "update_for_"+tname)
            except AttributeError:
                if "numpy" in sys.modules:
                    import numpy as np

                    # Hashing numpy dtypes
                    if (
                            # Handling numpy >= 1.20, for which
                            # type(np.dtype("float32")) -> "dtype[float32]"
                            tname.startswith("dtype[")
                            # Handling numpy >= 1.25, for which
                            # type(np.dtype("float32")) -> "Float32DType"
                            or tname.endswith("DType")
                            ):
                        if isinstance(key, np.dtype):
                            method = self.update_for_specific_dtype

                    # Hashing numpy scalars
                    elif isinstance(key, np.number):
                        # Non-numpy scalars are handled above in the try block.
                        method = self.update_for_numpy_scalar

                if method is None:
                    if issubclass(tp, Enum):
                        method = self.update_for_enum

                    elif is_dataclass(tp):
                        method = self.update_for_dataclass

                    elif _HAS_ATTRS and attrs.has(tp):
                        method = self.update_for_attrs

            if method is not None:
                inner_key_hash = self.new_hash()
                method(inner_key_hash, key)
                digest = inner_key_hash.digest()

        if digest is None:
            raise TypeError(
                    f"unsupported type for persistent hash keying: {type(key)}")

        if not isinstance(key, type):
            try:
                # pylint:disable=protected-access
                object.__setattr__(key, "_pytools_persistent_hash_digest",  digest)
            except AttributeError:
                pass
            except TypeError:
                pass

        key_hash.update(digest)
        return key_hash

    def __call__(self, key: Any) -> str:
        """Return the hash of *key*."""
        key_hash = self.new_hash()
        self.rec(key_hash, key)
        return key_hash.hexdigest()

    # {{{ updaters

    @staticmethod
    def update_for_type(key_hash: Hash, key: type) -> None:
        key_hash.update(
            f"{key.__module__}.{key.__qualname__}.{key.__name__}".encode("utf-8"))

    update_for_ABCMeta = update_for_type  # noqa: N815

    @staticmethod
    def update_for_int(key_hash: Hash, key: int) -> None:
        sz = 8
        while True:
            try:
                key_hash.update(key.to_bytes(sz, byteorder="little", signed=True))
                return
            except OverflowError:
                sz *= 2

    @classmethod
    def update_for_enum(cls, key_hash: Hash, key: Enum) -> None:
        cls.update_for_str(key_hash, str(key))

    @staticmethod
    def update_for_bool(key_hash: Hash, key: bool) -> None:
        key_hash.update(str(key).encode("utf8"))

    @staticmethod
    def update_for_float(key_hash: Hash, key: float) -> None:
        key_hash.update(key.hex().encode("utf8"))

    @staticmethod
    def update_for_complex(key_hash: Hash, key: float) -> None:
        key_hash.update(repr(key).encode("utf-8"))

    @staticmethod
    def update_for_str(key_hash: Hash, key: str) -> None:
        key_hash.update(key.encode("utf8"))

    @staticmethod
    def update_for_bytes(key_hash: Hash, key: bytes) -> None:
        key_hash.update(key)

    def update_for_tuple(self, key_hash: Hash, key: tuple) -> None:
        for obj_i in key:
            self.rec(key_hash, obj_i)

    def update_for_frozenset(self, key_hash: Hash, key: frozenset) -> None:
        from pytools import unordered_hash

        unordered_hash(
            key_hash,
            (self.rec(self.new_hash(), key_i).digest() for key_i in key))

    update_for_FrozenOrderedSet = update_for_frozenset  # noqa: N815

    @staticmethod
    def update_for_NoneType(key_hash: Hash, key: None) -> None:  # noqa: N802
        del key
        key_hash.update(b"<None>")

    @staticmethod
    def update_for_dtype(key_hash, key):
        key_hash.update(key.str.encode("utf8"))

    # Handling numpy >= 1.20, for which
    # type(np.dtype("float32")) -> "dtype[float32]"
    # Introducing this method allows subclasses to specially handle all those
    # dtypes.
    @staticmethod
    def update_for_specific_dtype(key_hash, key):
        key_hash.update(key.str.encode("utf8"))

    @staticmethod
    def update_for_numpy_scalar(key_hash: Hash, key) -> None:
        import numpy as np
        if hasattr(np, "complex256") and key.dtype == np.dtype("complex256"):
            key_hash.update(repr(complex(key)).encode("utf8"))
        elif hasattr(np, "float128") and key.dtype == np.dtype("float128"):
            key_hash.update(repr(float(key)).encode("utf8"))
        else:
            key_hash.update(np.array(key).tobytes())

    def update_for_dataclass(self, key_hash: Hash, key: Any) -> None:
        self.rec(key_hash, f"{type(key).__qualname__}.{type(key).__name__}")

        for fld in dc_fields(key):
            self.rec(key_hash, fld.name)
            self.rec(key_hash, getattr(key, fld.name, None))

    def update_for_attrs(self, key_hash: Hash, key) -> None:
        self.rec(key_hash, f"{type(key).__qualname__}.{type(key).__name__}")

        for fld in attrs.fields(key.__class__):
            self.rec(key_hash, fld.name)
            self.rec(key_hash, getattr(key, fld.name, None))

    def update_for_frozendict(self, key_hash: Hash, key: Mapping) -> None:
        from pytools import unordered_hash

        unordered_hash(
            key_hash,
            (self.rec(self.new_hash(), (k, v)).digest() for k, v in key.items()))

    update_for_immutabledict = update_for_frozendict
    update_for_constantdict = update_for_frozendict
    update_for_PMap = update_for_frozendict  # noqa: N815
    update_for_Map = update_for_frozendict  # noqa: N815

    # }}}

# }}}


# {{{ top-level

class NoSuchEntryError(KeyError):
    """Raised when an entry is not found in a :class:`PersistentDict`."""
    pass


class ReadOnlyEntryError(KeyError):
    """Raised when an attempt is made to overwrite an entry in a
    :class:`WriteOncePersistentDict`."""
    pass


class _PersistentDictBase:
    def __init__(self, identifier: str,
                 key_builder: Optional[KeyBuilder] = None,
                 container_dir: Optional[str] = None) -> None:
        self.identifier = identifier
        self.conn = None

        if key_builder is None:
            key_builder = KeyBuilder()

        self.key_builder = key_builder

        from os.path import join
        if container_dir is None:
            import platformdirs

            if sys.platform == "darwin" and os.getenv("XDG_CACHE_HOME") is not None:
                # platformdirs does not handle XDG_CACHE_HOME on macOS
                # https://github.com/platformdirs/platformdirs/issues/269
                container_dir = join(os.getenv("XDG_CACHE_HOME"), "pytools")
            else:
                container_dir = platformdirs.user_cache_dir("pytools", "pytools")

        self.filename = join(container_dir, f"pdict-v6-{identifier}"
                             + ".".join(str(i) for i in sys.version_info)
                             + ".sqlite")

        self.container_dir = container_dir
        self._make_container_dir()

        # isolation_level=None: enable autocommit mode
        # https://www.sqlite.org/lang_transaction.html#implicit_versus_explicit_transactions
        self.conn = sqlite3.connect(self.filename, isolation_level=None)

        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS dict (key text NOT NULL PRIMARY KEY, value);"
            )

        # WAL mode disabled for now
        # https://www.sqlite.org/wal.html
        # import platform
        # if platform.python_implementation() != "PyPy":
        #     self.conn.execute("PRAGMA journal_mode = 'WAL';")

        # temp_store=2: use in-memory temp store
        # https://www.sqlite.org/pragma.html#pragma_temp_store
        self.conn.execute("PRAGMA temp_store = 2;")

        # https://www.sqlite.org/pragma.html#pragma_synchronous
        self.conn.execute("PRAGMA synchronous = NORMAL;")

        # 64 MByte of cache
        # https://www.sqlite.org/pragma.html#pragma_cache_size
        self.conn.execute("PRAGMA cache_size = -64000;")

    def __del__(self) -> None:
        if self.conn:
            self.conn.close()

    def store_if_not_present(self, key: Any, value: Any) -> None:
        """Store (*key*, *value*) if *key* is not already present."""
        self.store(key, value, _skip_if_present=True)

    def store(self, key: Any, value: Any, _skip_if_present: bool = False) -> None:
        """Store (*key*, *value*) in the dictionary."""
        raise NotImplementedError()

    def fetch(self, key: Any) -> Any:
        """Return the value associated with *key* in the dictionary."""
        raise NotImplementedError()

    def _make_container_dir(self) -> None:
        """Create the container directory to store the dictionary."""
        os.makedirs(self.container_dir, exist_ok=True)

    def __getitem__(self, key: Any) -> Any:
        """Return the value associated with *key* in the dictionary."""
        return self.fetch(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        """Store (*key*, *value*) in the dictionary."""
        self.store(key, value)

    def __len__(self) -> int:
        """Return the number of entries in the dictionary."""
        return next(self.conn.execute("SELECT COUNT(*) FROM dict"))[0]

    def __iter__(self) -> Generator[str, None, None]:
        """Return an iterator over the key hashes in the dictionary."""
        return self.keys()

    def keys(self) -> Generator[str, None, None]:
        """Return an iterator over the key hashes in the dictionary."""
        for row in self.conn.execute("SELECT key FROM dict ORDER BY rowid"):
            yield row[0]

    def values(self) -> Generator[Any, None, None]:
        """Return an iterator over the values in the dictionary."""
        for row in self.conn.execute("SELECT value FROM dict ORDER BY rowid"):
            yield pickle.loads(row[0])

    def items(self) -> Generator[tuple[str, Any], None, None]:
        """Return an iterator over the items in the dictionary."""
        for row in self.conn.execute("SELECT key, value FROM dict ORDER BY rowid"):
            yield (row[0], pickle.loads(row[1]))

    def size(self) -> int:
        """Return the size of the dictionary in bytes."""
        return next(self.conn.execute("SELECT page_size * page_count FROM "
                          "pragma_page_size(), pragma_page_count();"))[0]

    def __repr__(self) -> str:
        """Return a string representation of the dictionary."""
        return f"{type(self).__name__}({self.filename}, nitems={len(self)})"

    def clear(self) -> None:
        """Remove all entries from the dictionary."""
        self.conn.execute("DELETE FROM dict;")


class WriteOncePersistentDict(_PersistentDictBase):
    """A concurrent disk-backed dictionary that disallows overwriting/deletion.

    Compared with :class:`PersistentDict`, this class has faster
    retrieval times because it uses an LRU cache to cache entries in memory.

    .. automethod:: __init__
    .. automethod:: __getitem__
    .. automethod:: __setitem__
    .. automethod:: clear
    .. automethod:: clear_in_mem_cache
    .. automethod:: store
    .. automethod:: store_if_not_present
    .. automethod:: fetch
    """
    def __init__(self, identifier: str,
                 key_builder: Optional[KeyBuilder] = None,
                 container_dir: Optional[str] = None,
                 in_mem_cache_size: int = 256) -> None:
        """
        :arg identifier: a file-name-compatible string identifying this
            dictionary
        :arg key_builder: a subclass of :class:`KeyBuilder`
        :arg container_dir: the directory in which to store this
            dictionary. If ``None``, the default cache directory from
            :func:`platformdirs.user_cache_dir` is used
        :arg in_mem_cache_size: retain an in-memory cache of up to
            *in_mem_cache_size* items (with an LRU replacement policy)
        """
        _PersistentDictBase.__init__(self, identifier, key_builder, container_dir)
        from functools import lru_cache
        self._fetch = lru_cache(maxsize=in_mem_cache_size)(self._fetch)

    def clear_in_mem_cache(self) -> None:
        """
        Clear the in-memory cache of this dictionary.

        .. versionadded:: 2023.1.1
        """
        self._fetch.cache_clear()

    def store(self, key: Any, value: Any, _skip_if_present: bool = False) -> None:
        k = self.key_builder(key)
        v = pickle.dumps(value)

        try:
            self.conn.execute("INSERT INTO dict VALUES (?, ?)", (k, v))
        except sqlite3.IntegrityError:
            if not _skip_if_present:
                raise ReadOnlyEntryError("WriteOncePersistentDict, "
                                         "tried overwriting key")

    def _fetch(self, keyhash: str) -> Any:  # pylint:disable=method-hidden
        # This is separate from fetch() to allow for LRU caching
        c = self.conn.execute("SELECT value FROM dict WHERE key=?", (keyhash,))
        row = c.fetchone()
        if row is None:
            raise KeyError
        return pickle.loads(row[0])

    def fetch(self, key: Any) -> Any:
        k = self.key_builder(key)

        try:
            return self._fetch(k)
        except KeyError:
            raise NoSuchEntryError(key)

    def clear(self) -> None:
        _PersistentDictBase.clear(self)
        self._fetch.cache_clear()


class PersistentDict(_PersistentDictBase):
    """A concurrent disk-backed dictionary.

    .. automethod:: __init__
    .. automethod:: __getitem__
    .. automethod:: __setitem__
    .. automethod:: __delitem__
    .. automethod:: clear
    .. automethod:: store
    .. automethod:: store_if_not_present
    .. automethod:: fetch
    .. automethod:: remove
    """
    def __init__(self,
                 identifier: str,
                 key_builder: Optional[KeyBuilder] = None,
                 container_dir: Optional[str] = None) -> None:
        """
        :arg identifier: a file-name-compatible string identifying this
            dictionary
        :arg key_builder: a subclass of :class:`KeyBuilder`
        :arg container_dir: the directory in which to store this
            dictionary. If ``None``, the default cache directory from
            :func:`platformdirs.user_cache_dir` is used
        """
        _PersistentDictBase.__init__(self, identifier, key_builder, container_dir)

    def store(self, key: Any, value: Any, _skip_if_present: bool = False) -> None:
        k = self.key_builder(key)
        v = pickle.dumps(value)

        if _skip_if_present:
            try:
                self.conn.execute("INSERT INTO dict VALUES (?, ?)", (k, v))
            except sqlite3.IntegrityError:
                # Key was already present
                return
        else:
            self.conn.execute("INSERT OR REPLACE INTO dict VALUES (?, ?)", (k, v))

    def fetch(self, key: Any) -> Any:
        k = self.key_builder(key)

        c = self.conn.execute("SELECT value FROM dict WHERE key=?", (k,))
        row = c.fetchone()
        if row is None:
            raise NoSuchEntryError(key)
        return pickle.loads(row[0])

    def remove(self, key: Any) -> None:
        """Remove the entry associated with *key* from the dictionary."""
        k = self.key_builder(key)

        self.conn.execute("DELETE FROM dict WHERE key=?", (k,))
        changes = next(self.conn.execute("SELECT changes()"))[0]
        if changes == 0:
            raise NoSuchEntryError(key)

    def __delitem__(self, key: Any) -> None:
        """Remove the entry associated with *key* from the dictionary."""
        self.remove(key)

# }}}

# vim: foldmethod=marker
