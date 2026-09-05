from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from computeruse.orchestrator.client import ActuationClient, DriverRpcError
from computeruse.orchestrator.schemas import PressHotkey, TypeText

from driver_probe import OUT, ROOT, await_socket
from native_probe import read_state


def main() -> None:
    release: Path = ROOT / 'driver/target/release/actuation-driver'
    with tempfile.TemporaryDirectory(prefix='cu-release-') as tmp:
        directory: Path = Path(tmp)
        socket_path: Path = directory / 'driver.sock'
        state_path: Path = directory / 'state.json'
        with (OUT / 'release-driver-stderr.log').open('wb') as stderr:
            driver: subprocess.Popen[bytes] = subprocess.Popen(
                [str(release), str(socket_path), '--real'], stderr=stderr, stdout=subprocess.DEVNULL,
            )
            app: subprocess.Popen[bytes] | None = None
            try:
                await_socket(socket_path, driver)
                with ActuationClient(str(socket_path)) as client:
                    original_app: str = client.focused_window().app_name
                    try:
                        app = subprocess.Popen([str(OUT / 'AuditProbe'), str(state_path)])
                        for attempt in range(50):
                            if state_path.exists():
                                break
                            time.sleep(0.1)
                        else:
                            raise TimeoutError('AppKit probe did not become ready')
                        if client.focused_window().pid != app.pid:
                            raise RuntimeError('Foreground changed; refusing hotkey test input')
                        client.send(PressHotkey(type='press_hotkey', modifiers=['command', 'shift'], key='escape'))
                        time.sleep(0.15)
                        tripped: bool = client.hotkey_state()
                        print(json.dumps({'probe': 'release_kill_combo', 'tripped': tripped}))
                        if not tripped:
                            raise RuntimeError('Release listener did not report the kill combo')
                        if client.focused_window().pid != app.pid:
                            raise RuntimeError('Foreground changed; refusing latch test input')
                        client.send(PressHotkey(type='press_hotkey', modifiers=[], key='x'))
                        time.sleep(0.15)
                        print(json.dumps({'probe': 'hotkey_after_latched_kill',
                                          'tripped': client.hotkey_state(), 'native': read_state(state_path)}))
                        try:
                            client.send(TypeText(type='type_text', text='SHOULD_NOT_TYPE', wpm=600))
                        except DriverRpcError as exc:
                            print(json.dumps({'probe': 'type_after_latched_kill', 'error': str(exc)}))
                    finally:
                        if app is not None:
                            app.terminate()
                            app.wait(timeout=5)
                        if original_app:
                            client.activate_app(original_app)
            finally:
                driver.terminate()
                driver.wait(timeout=5)


if __name__ == '__main__':
    main()
