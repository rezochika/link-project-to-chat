#!/bin/bash
set -e

VERSION=$(python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['version'])")
echo "Releasing v$VERSION..."

hatch build
hatch publish

git tag "v$VERSION"
git push origin main --tags

echo "Done. v$VERSION published to PyPI and tagged."
