"""Runs the dashboard data-layer tests (tests/dashboard/data_layer.test.mjs) in Node.

The data layer lives in index.html; the Node tests extract it between the DATA LAYER
markers and run it against a fake fetch, so no browser (Playwright) is needed.
Skipped when Node is not installed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = ROOT / 'tests' / 'dashboard' / 'data_layer.test.mjs'
NODE = shutil.which('node')


@pytest.mark.skipif(NODE is None, reason='Node.js is not installed')
def test_dashboard_data_layer():
    result = subprocess.run([NODE, '--test', '--test-reporter=spec', str(TEST_FILE)], cwd=ROOT,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
