import json
import os
from pathlib import Path
import shutil
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/agent/.agents/agent/utility/peer/scripts'
FIXTURE = Path(__file__).parent / 'fixtures/hidden_observer.py'


@pytest.mark.skipif(shutil.which('tj_hook') is None, reason='installed native tj_hook is required')
def test_detached_observer_and_descendant_remain_hidden_after_frontend_exit(tmp_path):
    from tj_cli.process import run

    state = tmp_path / 'observer state é'
    state.mkdir()
    frontend = run([sys.executable, '-X', 'utf8', str(FIXTURE), 'frontend', str(SCRIPTS), str(state)],
                   capture_output=True, timeout=45)
    assert frontend.returncode == 0, frontend.stderr.decode('utf-8', errors='replace')
    deadline = time.monotonic() + 20
    while not (state / 'finished.json').exists():
        assert time.monotonic() < deadline, 'detached observer did not survive the dummy frontend'
        time.sleep(.05)
    result = json.loads((state / 'finished.json').read_text(encoding='utf-8'))
    child = json.loads((state / 'child.json').read_text(encoding='utf-8'))
    assert result['survived_frontend'] is True
    assert result['child_returncode'] == 0
    observations = result['observations'] + child['observations']
    assert observations
    assert all(not item['visible'] and not item['windows'] for item in observations)
    if os.name == 'nt':
        assert all(item['console'] for item in observations)
