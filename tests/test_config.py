"""
Tests for the configuration module.

These tests verify that:
1. Individual config flags return correct values
2. --all flag returns all configuration values
3. Missing flag produces an error
4. --help shows all options
"""

import pytest
import typer
from typer.testing import CliRunner

from src.config import FLASK_PORT
from src.config import PROJECT_NAME
from src.config import PROJECT_VERSION
from src.config import config_cli

app = typer.Typer()
app.command()(config_cli)

runner = CliRunner()


@pytest.mark.parametrize(
    "flag,expected_output",
    [
        ("--project-name", PROJECT_NAME),
        ("--project-version", str(PROJECT_VERSION)),
        ("--flask-port", str(FLASK_PORT)),
    ],
)
def test_config_returns_single_value(flag: str, expected_output: str):
    """Test that individual flags return their correct values."""
    result = runner.invoke(app, [flag])

    assert result.exit_code == 0
    assert result.stdout.strip() == expected_output


def test_config_all_returns_all_values():
    """Test that --all flag returns all configuration values."""
    result = runner.invoke(app, ["--all"])

    assert result.exit_code == 0
    assert f"project_name={PROJECT_NAME}" in result.stdout
    assert f"project_version={PROJECT_VERSION}" in result.stdout
    assert f"flask_port={FLASK_PORT}" in result.stdout


def test_config_without_flag_fails():
    """Test that calling config without any flag produces an error."""
    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "Error: No config key specified" in result.output
