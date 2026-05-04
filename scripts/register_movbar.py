#!/usr/bin/env python3
"""Register/update movbar in the slopfactory9000 Stremio account.

Reads STREMIO_EMAIL, STREMIO_PASSWORD from ~/.nemoclaw_env.
Pass the manifest URL as argv[1] (e.g. https://<tunnel>.trycloudflare.com/manifest.json).

Idempotent: if a movbar addon is already in the collection (matched by manifest id),
it's replaced with the freshly-fetched manifest at the new URL.
"""
import json, sys, urllib.request
from pathlib import Path

API = "https://api.strem.io/api"

env = {}
for line in Path.home().joinpath(".nemoclaw_env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()


def post(method, body):
    req = urllib.request.Request(
        f"{API}/{method}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Stremio/1.9.12"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    if len(sys.argv) < 2:
        print("usage: register_movbar.py https://<host>/manifest.json", file=sys.stderr)
        sys.exit(2)
    manifest_url = sys.argv[1]

    manifest = fetch(manifest_url)
    print(f"FETCHED {manifest['id']} v{manifest['version']} — {manifest['name']}")

    auth = post("login", {
        "type": "Login",
        "email": env["STREMIO_EMAIL"],
        "password": env["STREMIO_PASSWORD"],
        "facebook": False,
    })
    if auth.get("error"):
        raise SystemExit(f"login failed: {auth['error']}")
    auth_key = auth["result"]["authKey"]
    print(f"LOGIN_OK")

    got = post("addonCollectionGet", {
        "type": "AddonCollectionGet",
        "authKey": auth_key,
        "update": True,
    })
    existing = got.get("result", {}).get("addons", [])
    print(f"COLLECTION {len(existing)} addons")

    # Replace any addon with the same id; insert movbar BEFORE Cinemeta so Stremio
    # picks up our enhanced meta (with barcode background) before falling back to
    # the plain Cinemeta response.
    new_id = manifest["id"]
    filtered = [a for a in existing if a.get("manifest", {}).get("id") != new_id]
    new_addon = {
        "transportUrl": manifest_url,
        "transportName": "http",
        "manifest": manifest,
        "flags": {"official": False, "protected": False},
    }
    cinemeta_idx = next(
        (i for i, a in enumerate(filtered) if a.get("manifest", {}).get("id") == "com.linvo.cinemeta"),
        0,
    )
    new_collection = filtered[:cinemeta_idx] + [new_addon] + filtered[cinemeta_idx:]

    set_resp = post("addonCollectionSet", {
        "type": "AddonCollectionSet",
        "authKey": auth_key,
        "addons": new_collection,
    })
    if set_resp.get("error"):
        raise SystemExit(f"set failed: {set_resp['error']}")
    print(f"SET_OK ({len(new_collection)} addons)")

    verify = post("addonCollectionGet", {
        "type": "AddonCollectionGet",
        "authKey": auth_key,
        "update": True,
    })
    final = verify.get("result", {}).get("addons", [])
    for a in final:
        m = a.get("manifest", {})
        if m.get("id") == new_id:
            print(f"VERIFIED {m['id']} v{m['version']} -> {a['transportUrl']}")
            return
    raise SystemExit("addon was not present after set; something's off")


if __name__ == "__main__":
    main()
