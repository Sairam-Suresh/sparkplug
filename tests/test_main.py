"""Tests for CLI entrypoint."""

import sys
from pathlib import Path
import pytest
from pytest_mock import MockerFixture

from src.main import main, setup_logging


def test_setup_logging():
    setup_logging("DEBUG")
    setup_logging("info")
    setup_logging("WARNING")


def test_main_with_valid_config(sample_yaml_file: Path, mocker: MockerFixture):
    mocker.patch.object(sys, "argv", ["sparkplug", "-c", str(sample_yaml_file)])
    mock_run = mocker.patch("uvicorn.run")
    main()
    mock_run.assert_called_once()


def test_main_with_invalid_config(mocker: MockerFixture):
    mocker.patch.object(sys, "argv", ["sparkplug", "-c", "/invalid/nonexistent.yaml"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1

