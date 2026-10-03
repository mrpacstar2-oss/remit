#!/usr/bin/env python3
"""Create an API key for a paying customer and store only its hash.

Usage on the VPS:  docker compose exec mcp python /app/deploy/new_key.py acme 50000
(or locally: python new_key.py <customer-id> <monthly-request-limit> --keys path/to/keys.json)
The key is printed once; give it to the customer. Restart the mcp service to load new keys.
"""
import argparse
import hashlib
import json
import os
import secrets

ap = argparse.ArgumentParser()
ap.add_argument("customer")
ap.add_argument("monthly_limit", type=int)
ap.add_argument("--keys", default="/data/keys.json")
a = ap.parse_args()
data = {"keys": []}
if os.path.exists(a.keys):
    data = json.load(open(a.keys))
key = "ax_" + secrets.token_urlsafe(24)
data["keys"].append({"id": a.customer, "sha256": hashlib.sha256(key.encode()).hexdigest(), "monthly_limit": a.monthly_limit})
json.dump(data, open(a.keys, "w"), indent=2)
print(f"API key for {a.customer} (shown once): {key}")
