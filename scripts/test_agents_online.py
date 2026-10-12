import json
import os
from pathlib import Path
import urllib.request

token = (Path.home() / ".aihub_token").read_text(encoding="utf-8").strip()
req = urllib.request.Request("http://127.0.0.1:8765/agents", headers={"Authorization": f"Bearer {token}"})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))
    for a in data:
        print(f"{a['id']:15} | {a['state']:15} | {a['name']}")
