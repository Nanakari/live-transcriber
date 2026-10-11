"""Keep the default suite independent of machine-specific integrations."""
import pytest


def pytest_addoption(parser):
    parser.addoption("--run-local", action="store_true", help="Include installed local integrations")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-local"):
        return
    skip = pytest.mark.skip(reason="local integration: enable with --run-local")
    for item in items:
        if "local" in item.keywords:
            item.add_marker(skip)
