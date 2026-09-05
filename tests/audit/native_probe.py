from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from computeruse.orchestrator.client import ActuationClient
from computeruse.orchestrator.schemas import MouseClick, PressHotkey, TypeText
from computeruse.vision.ax import AXElement, asks_for_a_credential

from driver_probe import BIN, OUT, await_socket


def matching_fields(root: AXElement) -> tuple[AXElement, ...]:
    own: tuple[AXElement, ...] = (root,) if root.title.startswith('Audit ') else ()
    return own + tuple(field for child in root.children for field in matching_fields(child))


def read_state(path: Path) -> object:
    return json.loads(path.read_text())


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='cu-native-') as tmp:
        directory: Path = Path(tmp)
        socket_path: Path = directory / 'driver.sock'
        state_path: Path = directory / 'state.json'
        with (OUT / 'native-driver-stderr.log').open('wb') as stderr:
            driver: subprocess.Popen[bytes] = subprocess.Popen(
                [str(BIN), str(socket_path), '--real'], stderr=stderr, stdout=subprocess.DEVNULL,
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
                            raise TimeoutError('Disposable AppKit probe did not become ready')
                        tree: AXElement = client.ax_snapshot(pid=app.pid, max_depth=12, max_nodes=250)
                        fields: tuple[AXElement, ...] = matching_fields(tree)
                        print(json.dumps({'probe': 'native_secure_detection',
                                          'native': read_state(state_path),
                                          'driver_fields': [{'title': field.title, 'role': field.role}
                                                            for field in fields],
                                          'asks_for_credential': asks_for_a_credential(tree)}))
                        normal: AXElement = next(field for field in fields if field.title == 'Audit normal field')
                        if client.focused_window().pid != app.pid:
                            raise RuntimeError('Foreground changed; refusing physical test input')
                        client.send(MouseClick(type='mouse_click',
                                               x=round(normal.x + normal.width / 2),
                                               y=round(normal.y + normal.height / 2)))
                        payload: str = 'Türkçe İı şğ 😀'
                        client.send(TypeText(type='type_text', text=payload, wpm=600))
                        time.sleep(0.2)
                        print(json.dumps({'probe': 'physical_unicode', 'expected': payload,
                                          'native': read_state(state_path)}, ensure_ascii=False))
                        if client.focused_window().pid != app.pid:
                            raise RuntimeError('Foreground changed; refusing hotkey test input')
                        client.send(PressHotkey(type='press_hotkey', modifiers=['command'], key='a'))
                        client.send(TypeText(type='type_text', text='replacement', wpm=600))
                        time.sleep(0.2)
                        print(json.dumps({'probe': 'physical_cmd_a_replace', 'native': read_state(state_path)}))
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
