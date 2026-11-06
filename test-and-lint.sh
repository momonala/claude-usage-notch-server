#!/bin/bash
# Pre-commit hook: tests, ruff format, ruff check

set -e

echo "🧪 Running tests..."
uv run pytest

echo "🖤 Running ruff format..."
if ! uvx ruff format --check .; then
    echo "❌ Ruff found formatting issues. To auto fix, run:"
    echo -e "\033[32muvx ruff format .\033[0m"
    exit 1
fi

echo "🧼 Running ruff check..."
if ! uvx ruff check .; then
    echo "❌ Ruff found linting issues. To auto fix, run:"
    echo -e "\033[32muvx ruff check . --fix\033[0m"
    exit 1
fi


echo "🔍 Checking if template files that block installation have been updated or deleted."
if [ -f install/projects_.service ] || [ -f install/projects_data-backup-scheduler.service ]; then
    echo "❌ install/projects_.service or install/projects_data-backup-scheduler.service needs to be renamed or deleted"
    exit 1
fi


echo "✅ Pre-commit checks passed!"
