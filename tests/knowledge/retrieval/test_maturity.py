"""Tests for backend.knowledge.retrieval.maturity — note maturity lifecycle."""

from unittest.mock import AsyncMock

from backend.knowledge.retrieval.maturity import (
    MATURITY_ORDER,
    MaturityConfig,
    MaturityEvaluator,
    NoteMaturity,
    normalize_status,
)


class TestNoteMaturity:
    """Test NoteMaturity enum and ordering."""

    def test_maturity_order(self) -> None:
        assert MATURITY_ORDER == [
            NoteMaturity.SEED,
            NoteMaturity.SEEDLING,
            NoteMaturity.BUDDING,
            NoteMaturity.EVERGREEN,
        ]

    def test_maturity_values(self) -> None:
        assert NoteMaturity.SEED == "seed"
        assert NoteMaturity.SEEDLING == "seedling"
        assert NoteMaturity.BUDDING == "budding"
        assert NoteMaturity.EVERGREEN == "evergreen"


class TestNormalizeStatus:
    """Test normalize_status() mapping."""

    def test_growing_maps_to_seed(self) -> None:
        assert normalize_status("growing") == NoteMaturity.SEED

    def test_valid_values_pass_through(self) -> None:
        assert normalize_status("seed") == NoteMaturity.SEED
        assert normalize_status("seedling") == NoteMaturity.SEEDLING
        assert normalize_status("budding") == NoteMaturity.BUDDING
        assert normalize_status("evergreen") == NoteMaturity.EVERGREEN

    def test_unknown_defaults_to_seed(self) -> None:
        assert normalize_status("unknown") == NoteMaturity.SEED
        assert normalize_status("") == NoteMaturity.SEED
        assert normalize_status("draft") == NoteMaturity.SEED


class TestMaturityConfig:
    """Test MaturityConfig defaults."""

    def test_defaults(self) -> None:
        config = MaturityConfig()
        assert config.seedling_min_relationships == 2
        assert config.budding_min_sources == 3
        assert config.evergreen_min_days_stable == 14
        assert config.evergreen_min_relationships == 5

    def test_custom_values(self) -> None:
        config = MaturityConfig(seedling_min_relationships=5)
        assert config.seedling_min_relationships == 5


def _mock_graph(
    rel_count: int = 0,
    source_count: int = 0,
    updated_at: str | None = None,
) -> AsyncMock:
    graph = AsyncMock()
    graph.count_relationships_for_entity = AsyncMock(return_value=rel_count)
    graph.count_distinct_sources = AsyncMock(return_value=source_count)
    graph.get_entity_updated_at = AsyncMock(return_value=updated_at)
    return graph


class TestMaturityEvaluator:
    """Test MaturityEvaluator promotion logic."""

    async def test_seed_to_seedling_with_enough_relationships(self) -> None:
        graph = _mock_graph(rel_count=3)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "seed")
        assert result == NoteMaturity.SEEDLING

    async def test_seed_stays_seed_with_too_few_relationships(self) -> None:
        graph = _mock_graph(rel_count=1)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "seed")
        assert result is None

    async def test_seed_to_seedling_at_threshold(self) -> None:
        graph = _mock_graph(rel_count=2)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "seed")
        assert result == NoteMaturity.SEEDLING

    async def test_seedling_to_budding_with_enough_sources(self) -> None:
        # seedling has rel_count>=2 (how it became seedling) + enough sources
        graph = _mock_graph(rel_count=3, source_count=4)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "seedling")
        assert result == NoteMaturity.BUDDING

    async def test_seedling_stays_with_too_few_sources(self) -> None:
        graph = _mock_graph(rel_count=3, source_count=2)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "seedling")
        assert result is None

    async def test_budding_to_evergreen(self) -> None:
        graph = _mock_graph(rel_count=6, source_count=4, updated_at="2026-01-01T00:00:00")
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "budding")
        assert result == NoteMaturity.EVERGREEN

    async def test_budding_stays_when_not_stable_enough(self) -> None:
        from datetime import datetime, timedelta

        recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        graph = _mock_graph(rel_count=6, source_count=4, updated_at=recent)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "budding")
        assert result is None

    async def test_budding_stays_when_too_few_relationships(self) -> None:
        graph = _mock_graph(rel_count=3, source_count=4, updated_at="2026-01-01T00:00:00")
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "budding")
        assert result is None

    async def test_budding_stays_when_no_updated_at(self) -> None:
        graph = _mock_graph(rel_count=6, source_count=4, updated_at=None)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "budding")
        assert result is None

    async def test_evergreen_no_further_promotion(self) -> None:
        graph = _mock_graph(rel_count=10, source_count=5)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "evergreen")
        assert result is None

    async def test_growing_backward_compat(self) -> None:
        graph = _mock_graph(rel_count=3)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("garden/idea/test.md", "growing")
        assert result == NoteMaturity.SEEDLING

    async def test_custom_config_thresholds(self) -> None:
        config = MaturityConfig(seedling_min_relationships=5)
        graph = _mock_graph(rel_count=3)
        evaluator = MaturityEvaluator(graph, config)
        result = await evaluator.evaluate("garden/idea/test.md", "seed")
        assert result is None  # 3 < 5

        graph = _mock_graph(rel_count=5)
        evaluator = MaturityEvaluator(graph, config)
        result = await evaluator.evaluate("garden/idea/test.md", "seed")
        assert result == NoteMaturity.SEEDLING


class TestMaturityDemotion:
    """v2.2: Test demotion when graph support drops below thresholds."""

    async def test_evergreen_demotes_to_budding(self) -> None:
        # Relationships drop below evergreen threshold (5)
        graph = _mock_graph(rel_count=2)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("test.md", "evergreen")
        assert result == NoteMaturity.BUDDING

    async def test_budding_demotes_to_seedling(self) -> None:
        # Sources drop below budding threshold (3)
        graph = _mock_graph(rel_count=5, source_count=1)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("test.md", "budding")
        assert result == NoteMaturity.SEEDLING

    async def test_seedling_demotes_to_seed(self) -> None:
        # Relationships drop below seedling threshold (2)
        graph = _mock_graph(rel_count=0)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("test.md", "seedling")
        assert result == NoteMaturity.SEED

    async def test_seed_cannot_demote(self) -> None:
        graph = _mock_graph(rel_count=0)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("test.md", "seed")
        assert result is None  # no demotion from seed

    async def test_evergreen_stays_if_supported(self) -> None:
        graph = _mock_graph(rel_count=10, source_count=5)
        evaluator = MaturityEvaluator(graph, MaturityConfig())
        result = await evaluator.evaluate("test.md", "evergreen")
        assert result is None  # well-supported, no change
