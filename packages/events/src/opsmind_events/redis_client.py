"""Synchronous Redis Streams command port, convenient to fake in tests."""

from typing import Protocol

StreamEntry = tuple[str, dict[str, str]]


class RedisStreamsClient(Protocol):
    """Subset of redis-py's synchronous client used by OpsMind transport."""

    def xadd(self, name: str, fields: dict[str, str], **kwargs: object) -> str: ...

    def xinfo_groups(self, name: str) -> list[dict[str, object]]: ...

    def xgroup_create(self, name: str, groupname: str, **kwargs: object) -> object: ...

    def xautoclaim(
        self, name: str, groupname: str, consumername: str, **kwargs: object
    ) -> tuple[str, list[StreamEntry], list[str]]: ...

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        **kwargs: object,
    ) -> list[tuple[str, list[StreamEntry]]]: ...

    def xack(self, name: str, groupname: str, *ids: str) -> int: ...
