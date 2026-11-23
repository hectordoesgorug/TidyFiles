from importlib.metadata import version
from pathlib import Path

import tidyfiles


def test_package_imports_from_source_layout() -> None:
    package_path = Path(tidyfiles.__file__).resolve()
    assert package_path.parent.name == "tidyfiles"
    assert package_path.parent.parent.name == "src"


def test_package_version_matches_distribution_metadata() -> None:
    assert tidyfiles.__version__ == version("tidyfiles")
