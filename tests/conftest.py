import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def pytest_addoption(parser):
    parser.addoption("--target", default="duckdb", help="duckdb (default) or databricks")


@pytest.fixture(scope="session")
def target(request):
    return request.config.getoption("--target")
