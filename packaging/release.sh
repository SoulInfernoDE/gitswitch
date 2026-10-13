#!/bin/sh
# Publish the current version: build the packages, create the GitHub release
# and copy it to the Forgejo mirror.
#
# Usage: release.sh <notes file> [<forgejo host> <forgejo owner/repo>]
# Expects the version commit to be pushed already.
set -eu
cd "$(dirname "$0")/.."

notes=$1
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' gitswitch/__init__.py)
source=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

python3 -m unittest discover -s tests
rm -f dist/*.deb dist/*.whl
packaging/build-deb.sh >/dev/null
python3 -m pip wheel . --no-deps --no-build-isolation -w dist -q
rm -rf build ./*.egg-info

gh release create "v$version" "dist/gitswitch_${version}_all.deb" "dist/gitswitch-${version}-py3-none-any.whl" \
    --repo "$source" --title "Git-Switch $version" --notes-file "$notes"

if [ $# -ge 3 ]; then
    packaging/mirror-releases.py "$source" "$2" "$3"
fi
