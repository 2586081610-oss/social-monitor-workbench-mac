"""Run real shell entrypoints in disposable roots; never touch real ports/services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='Mac POSIX entrypoints')


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / 'project with spaces'
    root.mkdir()
    for name in ('start_all.sh', 'start_monitor.sh', 'stop_all.sh'):
        shutil.copy(ROOT / name, root / name)
    (root / 'tools').mkdir()
    helper = ROOT / 'tools' / 'mac_runtime.sh'
    if helper.exists():
        shutil.copy(helper, root / 'tools' / helper.name)
    for directory in ('.venv/bin', 'node/bin', 'frontend/dist', 'fake-bin'):
        (root / directory).mkdir(parents=True)
    (root / 'frontend/dist/index.html').write_text('fixture')
    stub = f'#!{Path(sys.executable).resolve()}\nimport os,time,json\nfrom pathlib import Path\nPath(os.environ["CAPTURE_DIR"], str(os.getpid())+".json").write_text(json.dumps(dict(os.environ)))\nif os.environ.get("EXIT_STUB") == "1": raise SystemExit(9)\ntime.sleep(120)\n'
    for path in (root / '.venv/bin/python', root / 'node/bin/node'):
        path.write_text(stub)
        path.chmod(0o755)
    for name, script in {'lsof': '#!/bin/sh\nexit 1\n',
                         'curl': '#!/bin/sh\n[ "${HEALTH_FAIL:-0}" != 1 ]\n'}.items():
        p = root / 'fake-bin' / name
        p.write_text(script)
        p.chmod(0o755)
    captures = tmp_path / 'captures'
    captures.mkdir()
    env = dict(os.environ, NODE_DIR=str(root / 'node'), CAPTURE_DIR=str(captures),
               PATH=str(root / 'fake-bin') + ':' + os.environ['PATH'], STARTUP_TIMEOUT='2')
    yield root, env, captures
    for p in captures.glob('*.json'):
        try:
            os.kill(int(p.stem), 15)
        except ProcessLookupError:
            pass


def run(root, env, script):
    return subprocess.run(['sh', str(root / script)], env=env, text=True, capture_output=True, timeout=12)


@pytest.mark.parametrize('script', ['start_all.sh', 'start_monitor.sh'])
def test_proxy_lists_preserved_and_all_loopbacks_added(rig, script):
    root, env, captures = rig
    env.update(NO_PROXY='internal.example,localhost', no_proxy='lower.example,::1')
    result = run(root, env, script)
    assert result.returncode == 0, result.stdout + result.stderr
    for record in captures.glob('*.json'):
        child = json.loads(record.read_text())
        assert set(child['NO_PROXY'].split(',')) == {'internal.example', 'localhost', '127.0.0.1', '::1'}
        assert set(child['no_proxy'].split(',')) == {'lower.example', 'localhost', '127.0.0.1', '::1'}
    assert list(captures.glob('*.json'))


def test_start_exited_children_is_failure(rig):
    root, env, _ = rig
    env['EXIT_STUB'] = '1'
    result = run(root, env, 'start_all.sh')
    assert result.returncode != 0
    assert '已启动（' not in result.stdout
    assert not list((root / '.run').glob('*.pid'))


def test_unhealthy_start_cleans_up_children(rig):
    root, env, captures = rig
    env['HEALTH_FAIL'] = '1'
    result = run(root, env, 'start_all.sh')
    assert result.returncode != 0
    for record in captures.glob('*.json'):
        with pytest.raises(ProcessLookupError):
            os.kill(int(record.stem), 0)
    assert not list((root / '.run').glob('*.pid'))


def test_unrecorded_port_owner_with_project_path_survives(rig):
    root, env, _ = rig
    foreign = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', str(root)])
    try:
        lsof = root / 'fake-bin/lsof'
        lsof.write_text(f'#!/bin/sh\necho {foreign.pid}\n')
        result = run(root, env, 'stop_all.sh')
        assert foreign.poll() is None, result.stdout
        assert '已停止端口' not in result.stdout
    finally:
        foreign.terminate()
        foreign.wait()


def test_recorded_pid_without_identity_is_not_killed(rig):
    root, env, _ = rig
    foreign = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', str(root)])
    try:
        (root / '.run').mkdir()
        (root / '.run/backend.pid').write_text(str(foreign.pid))
        result = run(root, env, 'stop_all.sh')
        assert foreign.poll() is None, result.stdout
    finally:
        foreign.terminate()
        foreign.wait()


def test_successful_start_duplicate_rejected_and_verified_stop(rig):
    root, env, captures = rig
    result = run(root, env, 'start_all.sh')
    assert result.returncode == 0, result.stdout + result.stderr
    second = run(root, env, 'start_all.sh')
    assert second.returncode != 0
    assert len(list(captures.glob('*.json'))) == 2
    stopped = run(root, env, 'stop_all.sh')
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    for record in captures.glob('*.json'):
        with pytest.raises(ProcessLookupError):
            os.kill(int(record.stem), 0)


def test_delayed_interpreter_exec_does_not_record_wrapper_identity(rig):
    root, env, captures = rig
    python = root / '.venv/bin/python'
    payload = python.with_name('service.py')
    payload.write_text(python.read_text())
    python.write_text('#!/bin/sh\nsleep 2\nexec /usr/bin/env python3 "$(dirname "$0")/service.py" "$@"\n')
    started = run(root, env, 'start_all.sh')
    assert started.returncode == 0, started.stdout + started.stderr
    assert len(list(captures.glob('*.json'))) == 2
    stopped = run(root, env, 'stop_all.sh')
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
