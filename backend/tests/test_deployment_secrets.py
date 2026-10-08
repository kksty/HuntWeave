import importlib.util
import os
from pathlib import Path

import pytest

filename = Path(__file__).resolve().parents[2] / "deploy" / "initialize.py"
spec = importlib.util.spec_from_file_location("deployment_initialize", filename)
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


def test_deployment_initialization_preserves_existing_secrets(tmp_path: Path) -> None:
    initializer.initialize(tmp_path)
    directory = tmp_path / "runtime" / "secrets"
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert len(before) == 6
    assert all(len(bytes.fromhex(value.decode())) == 32 for value in before.values())
    assert len(set(before.values())) == 6
    initializer.initialize(tmp_path)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    if os.name == "posix":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o444 for path in directory.iterdir())


def test_invalid_existing_secret_is_not_replaced(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "secrets"
    directory.mkdir(parents=True)
    filename = directory / "access_key"
    filename.write_text("invalid")
    with pytest.raises(ValueError, match="Existing secret"):
        initializer.initialize(tmp_path)
    assert filename.read_text() == "invalid"
