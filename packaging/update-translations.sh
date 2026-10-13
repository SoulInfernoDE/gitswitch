#!/bin/sh
# Extract the program texts, merge them into every catalog and compile them.
set -eu
cd "$(dirname "$0")/.."

pot=gitswitch/locale/gitswitch.pot
xgettext --language=Python --keyword=_ --from-code=UTF-8 --sort-by-file \
    --package-name=gitswitch --msgid-bugs-address=https://github.com/SoulInfernoDE/gitswitch/issues \
    --output="$pot" gitswitch/*.py
# Keep the template stable between runs.
sed -i '/^"POT-Creation-Date:/d' "$pot"

for po in gitswitch/locale/*/LC_MESSAGES/gitswitch.po; do
    msgmerge --quiet --update --backup=none --no-fuzzy-matching "$po" "$pot"
    msgfmt --check --statistics --output-file="${po%.po}.mo" "$po"
done
