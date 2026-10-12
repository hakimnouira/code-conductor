import json
import os
import sys
import urllib.request
from pathlib import Path


def main():
    token_file = Path.home() / ".aihub_token"
    if not token_file.exists():
        print(f"Error: Token file not found at {token_file}. Launch the backend server first.")
        sys.exit(1)

    token = token_file.read_text(encoding="utf-8").strip()
    url = "http://127.0.0.1:8765/agents"

    print(f"Querying live agents at {url}...")
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print("-" * 65)
            print(f"{'ID':15} | {'STATE':15} | {'NAME'}")
            print("-" * 65)
            for a in data:
                print(f"{a['id']:15} | {a['state']:15} | {a['name']}")
            print("-" * 65)
    except urllib.error.URLError as e:
        print(f"Backend is not currently reachable at {url}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error checking agents: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
