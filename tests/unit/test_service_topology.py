"""Cycle-safe graph queries and scalable dependency traversal."""

from uuid import UUID

import pytest
from opsmind_models.domain.topology import (
    MissingTopologyService,
    ServiceDescriptor,
    ServiceTopology,
    TopologyCycle,
    TopologyEdge,
)

TENANT = UUID(int=999)


def _service(number: int, name: str | None = None) -> ServiceDescriptor:
    return ServiceDescriptor(
        id=UUID(int=number),
        tenant_id=TENANT,
        name=name or f"service-{number}",
        environment="production",
    )


def _edge(source: int, target: int) -> TopologyEdge:
    return TopologyEdge(UUID(int=source), UUID(int=target))


def test_upstream_downstream_depth_critical_path_and_blast_radius() -> None:
    services = [_service(i) for i in range(1, 7)]
    graph = ServiceTopology(
        services,
        [_edge(1, 2), _edge(2, 3), _edge(2, 4), _edge(3, 5), _edge(4, 5), _edge(6, 1)],
    )

    result = graph.analyze(UUID(int=2))

    assert [item.id for item in result.direct_dependencies] == [UUID(int=3), UUID(int=4)]
    assert {item.id for item in result.upstream_services} == {UUID(int=1), UUID(int=6)}
    assert {item.id for item in result.downstream_services} == {
        UUID(int=3),
        UUID(int=4),
        UUID(int=5),
    }
    assert result.upstream_depth == 2
    assert result.downstream_depth == 2
    assert [item.id for item in result.critical_path] == [UUID(int=2), UUID(int=3), UUID(int=5)]
    assert {item.id for item in result.blast_radius} == {
        UUID(int=1),
        UUID(int=2),
        UUID(int=6),
    }


def test_edge_that_would_create_cycle_is_rejected() -> None:
    graph = ServiceTopology([_service(1), _service(2), _service(3)], [_edge(1, 2), _edge(2, 3)])

    with pytest.raises(TopologyCycle):
        graph.assert_edge_allowed(UUID(int=3), UUID(int=1))


def test_cycle_in_persisted_graph_is_rejected() -> None:
    with pytest.raises(TopologyCycle):
        ServiceTopology(
            [_service(1), _service(2)],
            [_edge(1, 2), _edge(2, 1)],
        )


def test_dependency_to_missing_service_is_rejected() -> None:
    with pytest.raises(MissingTopologyService):
        ServiceTopology([_service(1)], [_edge(1, 2)])


def test_large_graph_queries_complete_with_expected_depth() -> None:
    size = 4000
    services = [_service(number) for number in range(1, size + 1)]
    edges = [_edge(number, number + 1) for number in range(1, size)]
    graph = ServiceTopology(services, edges)

    result = graph.analyze(UUID(int=1))

    assert result.downstream_depth == size - 1
    assert len(result.downstream_services) == size - 1
    assert len(result.critical_path) == size
