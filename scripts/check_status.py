import json
import pathlib
import time
import urllib.request

token = pathlib.Path.home().joinpath(".aihub_token").read_text().strip()
req = urllib.request.Request(
    "http://127.0.0.1:8765/agents",
    headers={"Authorization": f"Bearer {token}"}
)

t0 = time.time()
with urllib.request.urlopen(req, timeout=20) as resp:
    data = json.loads(resp.read().decode())
    elapsed = time.time() - t0
    print(f"Discovered {len(data)} agents in {elapsed:.2f}s:")
    for a in data:
        print(f"  [{a['state']}] {a['id']:<18} : {a['name']}")
