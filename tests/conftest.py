import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The ingestion function reads its configuration at import time.
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("AGENT_SEARCH_DATA_STORE_ID", "test-store")
os.environ.setdefault("GCS_BUCKET", "test-bucket")


@pytest.fixture(scope="session")
def ingestion():
    """Import ingestion/main.py as a module (the folder is not a package)."""
    spec = importlib.util.spec_from_file_location("ingestion_main", ROOT / "ingestion" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
