"""System-wide dynamic audit and autonomy verification probe runner.

Runs the complete empirical probe suite developed to test live actuation,
permission boundaries, approval lifecycle, and failure recovery.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"


def run_probe(name: str, script: str) -> tuple[int, str]:
    print(f"[*] Running {name} ({script})...", flush=True)
    res = subprocess.run(
        [str(PYTHON), str(Path(__file__).parent / script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if res.returncode != 0:
        print(f"[-] {name} FAILED with exit code {res.returncode}:\n{res.stderr}", flush=True)
    else:
        print(f"[+] {name} PASSED", flush=True)
    return res.returncode, res.stdout


def main() -> int:
    probes = [
        ("Logic & Perms Probe", "logic_probe.py"),
        ("Web SSRF & Tool Probe", "web_probe.py"),
        ("Audit Extra & Bounds Probe", "extra_probe.py"),
        ("CLI & Autonomy Approvals Probe", "cli_probe.py"),
        ("Driver IPC & Freeze Probe", "driver_probe.py"),
    ]

    total_failures = 0
    results: dict[str, list[str]] = {}

    for name, script in probes:
        code, output = run_probe(name, script)
        if code != 0:
            total_failures += 1
        lines = [line for line in output.strip().splitlines() if line.startswith("{")]
        results[name] = lines

    print("\n" + "=" * 60)
    print("AUDIT PROBE RESULTS SUMMARY")
    print("=" * 60)
    for name, lines in results.items():
        print(f"\n--- {name} ({len(lines)} probes) ---")
        for line in lines:
            try:
                data = json.loads(line)
                print(f"  • {data.get('probe')}: {json.dumps(data.get('result', data))[:100]}...")
            except (json.JSONDecodeError, TypeError):
                print(f"  • {line[:120]}")

    print("\n" + "=" * 60)
    if total_failures == 0:
        print("ALL PROBES COMPLETED SUCCESSFULLY (5/5 suites)")
        return 0
    else:
        print(f"FAILED: {total_failures} probe suite(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
