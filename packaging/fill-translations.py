#!/usr/bin/python3
"""Fill untranslated entries of a .po file from a JSON object {msgid: msgstr}.

For plural entries the key is the singular msgid and the value a list with one
text per plural form.

Usage: fill-translations.py <catalog.po> <translations.json>
"""

import json
import re
import sys
from pathlib import Path


def unquote(block: str) -> str:
    return "".join(json.loads(line) for line in block.splitlines() if line.startswith('"'))


def main() -> int:
    catalog, translations = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    entry = re.compile(
        r'msgid ((?:"[^\n]*"\n)+)(?:msgid_plural ((?:"[^\n]*"\n)+))?((?:msgstr(?:\[\d+\])? ""\n)+)(?=\n|\Z)'
    )

    def fill(match: re.Match) -> str:
        msgid = unquote(match.group(1))
        if msgid not in translations:
            return match.group(0)
        text = translations.pop(msgid)
        head = f"msgid {json.dumps(msgid, ensure_ascii=False)}\n"
        if match.group(2) is None:
            return head + f"msgstr {json.dumps(text, ensure_ascii=False)}\n"
        head += f"msgid_plural {json.dumps(unquote(match.group(2)), ensure_ascii=False)}\n"
        return head + "".join(f"msgstr[{n}] {json.dumps(form, ensure_ascii=False)}\n" for n, form in enumerate(text))

    catalog.write_text(entry.sub(fill, catalog.read_text(encoding="utf-8")), encoding="utf-8")
    for unused in translations:
        print(f"not found or already translated: {unused}", file=sys.stderr)
    return 1 if translations else 0


if __name__ == "__main__":
    sys.exit(main())
