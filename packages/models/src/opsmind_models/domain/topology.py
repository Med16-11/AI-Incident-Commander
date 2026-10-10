"""In-memory directed service topology with cycle-safe graph queries.

Edges point from a caller to the service it depends on. Therefore downstream
services are reachable by following edges, while upstream services are found
by walking the reverse graph.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from uuid import UUID


class TopologyError(ValueError):
    """Base exception for an invalid service topology operation."""


class MissingTopologyService(TopologyError):
    """A node referenced by an edge does not exist in this graph."""


class TopologyCycle(TopologyError):
    """A dependency edge would make the directed graph cyclic."""


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    """Stable service identity and scope used by topology queries."""

    id: UUID
    tenant_id: UUID
    name: str
    environment: str


@dataclass(frozen=True, slots=True)
class TopologyEdge:
    """Directed dependency edge: source depends on target."""

    source_service_id: UUID
    target_service_id: UUID


@dataclass(frozen=True, slots=True)
class TopologyAnalysis:
    """Calculated neighborhood and impact information for one service."""

    service: ServiceDescriptor
    direct_dependencies: tuple[ServiceDescriptor, ...]
    upstream_services: tuple[ServiceDescriptor, ...]
    downstream_services: tuple[ServiceDescriptor, ...]
    upstream_depth: int
    downstream_depth: int
    critical_path: tuple[ServiceDescriptor, ...]
    blast_radius: tuple[ServiceDescriptor, ...]


class ServiceTopology:
    """An indexed DAG supporting O(V + E) validation and traversal."""

    def __init__(
        self,
        services: tuple[ServiceDescriptor, ...] | list[ServiceDescriptor],
        edges: tuple[TopologyEdge, ...] | list[TopologyEdge],
    ) -> None:
        self.services = {service.id: service for service in services}
        if len(self.services) != len(services):
            raise TopologyError("Service identifiers must be unique")
        self.downstream: dict[UUID, set[UUID]] = {service_id: set() for service_id in self.services}
        self.upstream: dict[UUID, set[UUID]] = {service_id: set() for service_id in self.services}
        for edge in edges:
            if edge.source_service_id not in self.services:
                raise MissingTopologyService(str(edge.source_service_id))
            if edge.target_service_id not in self.services:
                raise MissingTopologyService(str(edge.target_service_id))
            if edge.source_service_id == edge.target_service_id:
                raise TopologyCycle("A service cannot depend on itself")
            self.downstream[edge.source_service_id].add(edge.target_service_id)
            self.upstream[edge.target_service_id].add(edge.source_service_id)
        self._topological_order = self._topological_sort()

    def assert_edge_allowed(self, source_id: UUID, target_id: UUID) -> None:
        """Validate node existence and reject an edge that closes a cycle."""
        self._require_service(source_id)
        self._require_service(target_id)
        if source_id == target_id or self._has_path(target_id, source_id, self.downstream):
            raise TopologyCycle("Dependency would create a cycle")

    def analyze(self, service_id: UUID) -> TopologyAnalysis:
        """Return direct dependencies, transitive impact, depth, and critical path."""
        service = self._require_service(service_id)
        downstream_depths = self._shortest_depths(service_id, self.downstream)
        upstream_depths = self._shortest_depths(service_id, self.upstream)
        downstream = self._descriptors(downstream_depths, excluding=service_id)
        upstream = self._descriptors(upstream_depths, excluding=service_id)
        direct_ids = self.downstream[service_id]
        critical_path_ids = self._longest_path(service_id, self.downstream)
        return TopologyAnalysis(
            service=service,
            direct_dependencies=tuple(
                self.services[node_id] for node_id in sorted(direct_ids, key=self._service_sort_key)
            ),
            upstream_services=upstream,
            downstream_services=downstream,
            upstream_depth=self._max_path_depth(service_id, self.upstream),
            downstream_depth=self._max_path_depth(service_id, self.downstream),
            critical_path=tuple(self.services[node_id] for node_id in critical_path_ids),
            # A failed dependency affects this service and callers that depend on it.
            blast_radius=tuple(
                [service]
                + [
                    self.services[node_id]
                    for node_id in sorted(
                        upstream_depths,
                        key=lambda key: (upstream_depths[key], self._service_sort_key(key)),
                    )
                    if node_id != service_id
                ]
            ),
        )

    def _require_service(self, service_id: UUID) -> ServiceDescriptor:
        service = self.services.get(service_id)
        if service is None:
            raise MissingTopologyService(str(service_id))
        return service

    def _topological_sort(self) -> tuple[UUID, ...]:
        indegree = {service_id: len(parents) for service_id, parents in self.upstream.items()}
        ready = deque(
            sorted(
                (key for key, count in indegree.items() if count == 0), key=self._service_sort_key
            )
        )
        ordered: list[UUID] = []
        while ready:
            current = ready.popleft()
            ordered.append(current)
            for child in sorted(self.downstream[current], key=self._service_sort_key):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(ordered) != len(self.services):
            raise TopologyCycle("Service dependencies contain a cycle")
        return tuple(ordered)

    @staticmethod
    def _has_path(start: UUID, end: UUID, adjacency: dict[UUID, set[UUID]]) -> bool:
        stack = [start]
        seen: set[UUID] = set()
        while stack:
            current = stack.pop()
            if current == end:
                return True
            if current not in seen:
                seen.add(current)
                stack.extend(adjacency[current] - seen)
        return False

    @staticmethod
    def _shortest_depths(start: UUID, adjacency: dict[UUID, set[UUID]]) -> dict[UUID, int]:
        depths = {start: 0}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if neighbor not in depths:
                    depths[neighbor] = depths[current] + 1
                    queue.append(neighbor)
        return depths

    def _max_path_depth(self, start: UUID, adjacency: dict[UUID, set[UUID]]) -> int:
        # The reverse topological order is also valid for the reversed DAG.
        order = (
            self._topological_order
            if adjacency is self.downstream
            else tuple(reversed(self._topological_order))
        )
        longest: dict[UUID, int] = {}
        for service_id in reversed(order):
            children = adjacency[service_id]
            longest[service_id] = max((1 + longest[child] for child in children), default=0)
        return longest[start]

    def _longest_path(self, start: UUID, adjacency: dict[UUID, set[UUID]]) -> tuple[UUID, ...]:
        order = (
            self._topological_order
            if adjacency is self.downstream
            else tuple(reversed(self._topological_order))
        )
        lengths: dict[UUID, int] = {}
        next_node: dict[UUID, UUID] = {}
        for service_id in reversed(order):
            children = adjacency[service_id]
            if not children:
                lengths[service_id] = 0
                continue
            best = min(children, key=lambda child: (-lengths[child], self._service_sort_key(child)))
            lengths[service_id] = 1 + lengths[best]
            next_node[service_id] = best
        path = [start]
        while path[-1] in next_node:
            path.append(next_node[path[-1]])
        return tuple(path)

    def _descriptors(
        self, depths: dict[UUID, int], *, excluding: UUID
    ) -> tuple[ServiceDescriptor, ...]:
        node_ids = (node_id for node_id in depths if node_id != excluding)
        return tuple(
            self.services[node_id]
            for node_id in sorted(
                node_ids, key=lambda key: (depths[key], self._service_sort_key(key))
            )
        )

    def _service_sort_key(self, service_id: UUID) -> tuple[str, str, str]:
        service = self.services[service_id]
        return (service.name.casefold(), service.environment.casefold(), str(service.id))
