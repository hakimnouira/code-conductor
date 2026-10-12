import os
import shutil
import subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = [pytest.mark.windows, pytest.mark.unit]

def test_typescript_compilation():
    npm_bin = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm_bin:
        pytest.skip("npm executable not found in PATH")

    ext_dir = REPO_ROOT / "extension"
    assert ext_dir.exists(), f"Extension directory missing at {ext_dir}"

    proc = subprocess.run(
        [npm_bin, "run", "compile"],
        cwd=str(ext_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"TypeScript compilation failed:\n{proc.stdout}\n{proc.stderr}"

    # Verify compiled outputs exist
    out_dir = ext_dir / "out"
    assert (out_dir / "extension.js").exists(), "out/extension.js not found after compilation"
    assert (out_dir / "backendManager.js").exists(), "out/backendManager.js not found after compilation"
    assert (out_dir / "chatViewProvider.js").exists(), "out/chatViewProvider.js not found after compilation"

def test_clean_windows_setup_script():
    setup_script = REPO_ROOT / "scripts" / "setup.ps1"
    assert setup_script.exists(), "scripts/setup.ps1 does not exist"

    content = setup_script.read_text(encoding="utf-8")

    # 1. Must NOT contain hardcoded specific usernames
    assert "C:\\Users\\hakim" not in content, "setup.ps1 still contains hardcoded username 'hakim'"

    # 2. Must discover Python dynamically through PATH or uv
    assert "uv python find" in content or "Get-Command python" in content, (
        "setup.ps1 does not dynamically discover Python"
    )

    # 3. Must validate Python >= 3.11
    assert "3.11" in content, "setup.ps1 does not validate Python 3.11+"

    # 4. Must install backend and extension dependencies
    assert "requirements.txt" in content, "setup.ps1 does not install requirements.txt"
    assert "run compile" in content, "setup.ps1 does not compile TypeScript extension"
