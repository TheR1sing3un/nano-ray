#!/bin/bash
VERSION=$1
sed -i "s/^version = .*/version = \"$VERSION\"/" pyproject.toml
sed -i "s/^version = .*/version = \"$VERSION\"/" rust/nano_ray_core/Cargo.toml
echo "Updated version to $VERSION"