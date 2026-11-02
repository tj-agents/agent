import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)


def console_state(title: str) -> dict:
    if os.name != 'nt':
        return {'console': None, 'visible': False, 'windows': []}
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    kernel.SetConsoleTitleW(title)
    window = kernel.GetConsoleWindow()
    visible = []

    @callback_type
    def collect(handle, _parameter):
        text = ctypes.create_unicode_buffer(1024)
        user.GetWindowTextW(handle, text, len(text))
        if user.IsWindowVisible(handle) and title in text.value:
            visible.append(text.value)
        return True

    user.EnumWindows(collect, 0)
    return {'console': int(window or 0), 'visible': bool(user.IsWindowVisible(window)), 'windows': visible}


def main() -> None:
    mode, scripts, directory = sys.argv[1:4]
    state = Path(directory)
    title = 'tj-hidden-observer-' + hashlib.sha256(str(state).encode()).hexdigest()[:16]
    if mode == 'frontend':
        sys.path.insert(0, scripts)
        import session_close
        from tj_cli import processes

        os.environ['AGENT_STATE_DIRECTORY'] = str(state)
        info = processes.process_info(os.getpid())
        write(state / 'frontend.json', {'pid': info.pid, 'started_at': info.started_at})
        session_close.start_detached(Path(__file__), ['observer', scripts, directory], stage=False)
        deadline = time.monotonic() + 20
        while not (state / 'started.json').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('observer did not acknowledge startup')
            time.sleep(.05)
        return
    if mode == 'child':
        observations = []
        until = time.monotonic() + 2
        while time.monotonic() < until:
            observations.append(console_state(title))
            time.sleep(.05)
        write(state / 'child.json', {'observations': observations})
        return
    from tj_cli import processes

    frontend = json.loads((state / 'frontend.json').read_text(encoding='utf-8'))
    observations = [console_state(title)]
    write(state / 'started.json', {'pid': os.getpid()})
    deadline = time.monotonic() + 30
    while processes.matches(frontend):
        if time.monotonic() >= deadline:
            raise TimeoutError('dummy frontend did not exit')
        observations.append(console_state(title))
        time.sleep(.05)
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'child', scripts, directory])
    while child.poll() is None:
        if time.monotonic() >= deadline:
            child.kill()
            child.wait(timeout=5)
            raise TimeoutError('dummy descendant did not exit')
        observations.append(console_state(title))
        time.sleep(.05)
    write(state / 'finished.json', {'survived_frontend': True, 'child_returncode': child.returncode,
                                   'observations': observations})


if __name__ == '__main__':
    main()
