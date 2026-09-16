"""Environment-based configuration for the Navigator microservice."""
import os


class NavigatorConfig:
    """Configuration loaded from environment variables with sensible defaults."""

    NEO4J_URI: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "sentinel_graph")
    NEO4J_DATABASE: str = os.getenv("NEO4J_DATABASE", "neo4j")

    # API server
    HOST: str = os.getenv("NAVIGATOR_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("NAVIGATOR_PORT", "8100"))

    # Limits
    MAX_VALUES_PER_EDGE: int = int(os.getenv("MAX_VALUES_PER_EDGE", "50"))
    DEFAULT_NEIGHBOR_DEPTH: int = int(os.getenv("DEFAULT_NEIGHBOR_DEPTH", "3"))


config = NavigatorConfig()
