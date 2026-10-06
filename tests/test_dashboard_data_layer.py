"""Runs the dashboard's Node tests (tests/dashboard/*.test.mjs).

The data layer and the panel models live in index.html; the tests extract them between
their BEGIN/END markers and run them (the data layer against a fake fetch), and check the
page markup and script for hard-coded analytics, unsafe sinks and broken element ids.
No browser (Playwright) is needed. Skipped when Node is not installed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / 'tests' / 'dashboard'
NODE = shutil.which('node')


@pytest.mark.skipif(NODE is None, reason='Node.js is not installed')
def test_dashboard_node_tests():
    files = sorted(str(p) for p in TEST_DIR.glob('*.test.mjs'))
    assert len(files) >= 2
    result = subprocess.run([NODE, '--test', '--test-timeout=60000', '--test-reporter=spec', *files], cwd=ROOT,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
