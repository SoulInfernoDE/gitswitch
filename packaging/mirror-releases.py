#!/usr/bin/env python3
"""Copy the GitHub releases, with notes and assets, to a Forgejo mirror.

A Forgejo pull mirror follows branches and tags but not releases. This script
creates every release that is missing on the mirror. The Forgejo token is read
from the system keyring through Git-Switch, so the account must have been
added there.

Usage: mirror-releases.py <github owner/repo> <forgejo host> <forgejo owner/repo>
"""

import json
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gitswitch import core, forge  # noqa: E402


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def post(url: str, token: str, data: bytes, content_type: str) -> dict:
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Authorization": f"token {token}", "Content-Type": content_type, "Accept": "application/json"},
    )
    with core._api_opener.open(request, timeout=120) as response:
        return json.load(response)


def upload(url: str, token: str, path: Path) -> None:
    boundary = uuid.uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="{path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    post(url, token, head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}")


def main() -> int:
    source, host, target = sys.argv[1:4]
    account = next(a for a in forge.list_accounts() if a.host == host and a.active)
    token = forge.lookup_token(host, account.user)
    api = f"https://{host}/api/v1/repos/{target}/releases"
    present = {release["tag_name"]: release for release in core.api_get(f"{api}?limit=100", f"token {token}")[0]}

    tags = [entry["tagName"] for entry in json.loads(gh("release", "list", "--repo", source, "--limit", "100", "--json", "tagName"))]
    for tag in reversed(tags):
        release = json.loads(gh("release", "view", tag, "--repo", source, "--json", "name,body,isPrerelease,assets"))
        mirrored = present.get(tag)
        if mirrored is None:
            body = {"tag_name": tag, "name": release["name"], "body": release["body"], "prerelease": release["isPrerelease"]}
            mirrored = post(api, token, json.dumps(body).encode(), "application/json")
        have = {asset["name"] for asset in mirrored.get("assets") or []}
        missing = [asset["name"] for asset in release["assets"] if asset["name"] not in have]
        with tempfile.TemporaryDirectory() as directory:
            for name in missing:
                gh("release", "download", tag, "--repo", source, "--dir", directory, "--pattern", name)
                upload(f"{api}/{mirrored['id']}/assets?name={name}", token, Path(directory, name))
        print(f"{tag}: {len(missing)} asset(s) copied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
