"""Neo4j-testfixturen: tom databas krävs, annars vägrar den."""

from __future__ import annotations

import pytest
from conftest import _guard_empty


class _FakeResult:
    def __init__(self, count: int) -> None:
        self._count = count

    def single(self) -> dict[str, int]:
        return {"c": self._count}


class _FakeSession:
    def __init__(self, count: int) -> None:
        self.count = count
        self.queries: list[str] = []

    def run(self, query: str, **_kw: object) -> _FakeResult:
        self.queries.append(query)
        return _FakeResult(self.count)


@pytest.mark.neo4j
def test_fixture_gives_empty_database(neo4j_session) -> None:
    assert neo4j_session.run("MATCH (n) RETURN count(n) AS c").single()["c"] == 0


def test_fixture_refuses_non_empty_database() -> None:
    session = _FakeSession(count=3)
    with pytest.raises(pytest.fail.Exception, match="Testdatabasen är inte tom"):
        _guard_empty(session)
    assert all("DELETE" not in q for q in session.queries)
