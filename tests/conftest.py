import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))  # generate_sample_data, rfq_to_excel

from generate_sample_data import generate  # noqa: E402


@pytest.fixture(scope="session")
def sample_files(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("sample"))
