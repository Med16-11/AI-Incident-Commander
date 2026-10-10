"""Historical incident retrieval ports and PostgreSQL full-text implementation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol, cast
from uuid import UUID

from opsmind_storage.models import HistoricalIncidentRow
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsmind_incident_engine.investigation import (
    HistoricalEvidenceReference,
    HistoricalIncidentContext,
)

_ENGLISH_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "with",
    ]
)


class HistoricalIncidentRetriever(Protocol):
    """Retrieval boundary used by investigation tools; implementations are replaceable."""

    def search(
        self, session: Session, tenant_id: UUID, query: str, *, limit: int
    ) -> list[HistoricalIncidentContext]: ...


class VectorSearchHit(BaseModel):
    """Backend-neutral vector result identity and similarity score."""

    model_config = ConfigDict(extra="forbid", strict=True)

    incident_id: UUID
    similarity: float = Field(ge=0, le=1)


class HistoricalVectorSearchBackend(Protocol):
    """Adapter contract for pgvector or an external vector database."""

    def search(self, tenant_id: UUID, query: str, *, limit: int) -> Sequence[VectorSearchHit]: ...


class PostgreSQLFullTextHistoricalRetriever:
    """Use indexed PostgreSQL tsvector search; provide lexical fallback for SQLite tests."""

    def search(
        self, session: Session, tenant_id: UUID, query: str, *, limit: int
    ) -> list[HistoricalIncidentContext]:
        normalized_query = query.strip()
        if not normalized_query:
            return []
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            query_terms = _terms(normalized_query)
            if not query_terms:
                return []
            vector = func.to_tsvector("english", HistoricalIncidentRow.search_text)
            safe_tsquery = " | ".join(f"{term}:*" for term in sorted(query_terms))
            terms = func.to_tsquery("english", safe_tsquery)
            rank = func.ts_rank_cd(vector, terms)
            rows = session.execute(
                select(HistoricalIncidentRow, rank.label("similarity"))
                .where(
                    HistoricalIncidentRow.tenant_id == tenant_id,
                    vector.op("@@")(terms),
                )
                .order_by(rank.desc(), HistoricalIncidentRow.resolved_at.desc())
                .limit(limit)
            ).all()
            return [
                _to_context(
                    cast(HistoricalIncidentRow, row),
                    min(1.0, max(0.0, float(cast(float, similarity)))),
                )
                for row, similarity in rows
            ]
        return self._lexical_fallback(session, tenant_id, normalized_query, limit)

    @staticmethod
    def _lexical_fallback(
        session: Session, tenant_id: UUID, query: str, limit: int
    ) -> list[HistoricalIncidentContext]:
        tokens = _terms(query)
        if not tokens:
            return []
        rows = session.scalars(
            select(HistoricalIncidentRow)
            .where(HistoricalIncidentRow.tenant_id == tenant_id)
            .order_by(HistoricalIncidentRow.resolved_at.desc())
        ).all()
        ranked = [(row, len(tokens & _terms(row.search_text)) / len(tokens)) for row in rows]
        return [
            _to_context(row, similarity)
            for row, similarity in sorted(
                (item for item in ranked if item[1] > 0),
                key=lambda item: (item[1], item[0].resolved_at),
                reverse=True,
            )[:limit]
        ]


class VectorHistoricalIncidentRetriever:
    """Hydrate vector hits through tenant-scoped PostgreSQL snapshot rows."""

    def __init__(self, backend: HistoricalVectorSearchBackend) -> None:
        self._backend = backend

    def search(
        self, session: Session, tenant_id: UUID, query: str, *, limit: int
    ) -> list[HistoricalIncidentContext]:
        hits = list(self._backend.search(tenant_id, query, limit=limit))[:limit]
        if not hits:
            return []
        score_by_id = {hit.incident_id: hit.similarity for hit in hits}
        rows = session.scalars(
            select(HistoricalIncidentRow).where(
                HistoricalIncidentRow.tenant_id == tenant_id,
                HistoricalIncidentRow.incident_id.in_(score_by_id),
            )
        ).all()
        by_id = {row.incident_id: row for row in rows}
        return [
            _to_context(by_id[hit.incident_id], hit.similarity)
            for hit in hits
            if hit.incident_id in by_id
        ]


def _terms(value: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]{3,}", value.casefold())
        if term not in _ENGLISH_STOP_WORDS
    }


def _to_context(row: HistoricalIncidentRow, similarity: float) -> HistoricalIncidentContext:
    evidence: list[HistoricalEvidenceReference] = []
    for item in row.evidence:
        try:
            evidence.append(HistoricalEvidenceReference.model_validate(item))
        except ValueError:
            continue
    return HistoricalIncidentContext(
        incident_id=row.incident_id,
        similarity=similarity,
        summary=row.summary,
        symptoms=row.symptoms,
        root_cause=row.root_cause,
        affected_services=row.affected_services,
        evidence=evidence,
        remediation=[
            str(item.get("summary", item.get("action_type", "historical remediation")))
            for item in row.remediation
        ],
        resolved_at=row.resolved_at.isoformat(),
        resolution_seconds=row.resolution_seconds,
        tags=row.tags,
    )
