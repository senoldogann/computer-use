from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from computeruse.agent import guarded
from computeruse.orchestrator.evidence import Evidence, combine
from computeruse.orchestrator.loop import AxProbeResult, OodaRunner, WorkingState, resolve_mark
from computeruse.orchestrator.schemas import Action, AgentTurn, ClickMark, Finish, MouseClick, PressHotkey, TypeText
from computeruse.security.autonomy import AutonomyLevel, classify_risk, decide_permission
from computeruse.security.grants import authorize, new_grant
from computeruse.security.killswitch import CursorSample, KillSwitch, MouseShakeMonitor
from computeruse.skills.distiller import Trajectory, distill, signature_of
from computeruse.vision.ax import AXElement, element_summary, summaries_to_image_space
from computeruse.vision.coordinates import Point, Rect, ScreenMap, Size
from computeruse.vision.diff import Verification, verdict
from computeruse.vision.som import MarkElement, parse_ax_elements_to_marks


def emit(probe: str, result: object) -> None:
    print(json.dumps({'probe': probe, 'result': result}, ensure_ascii=False))


def policy_probe() -> None:
    cases: tuple[tuple[str, Action, str, str | None], ...] = (
        ('short_rm_control', TypeText(type='type_text', text='rm -f /tmp/audit-placeholder'), 'continue', None),
        ('long_shell', TypeText(type='type_text', text="printf '%s' '" + 'x' * 220 + "'; rm -f /tmp/audit-placeholder"), 'continue', None),
        ('finder_trash_hotkey', PressHotkey(type='press_hotkey', modifiers=['command'], key='backspace'), 'continue', None),
        ('clear_label', MouseClick(type='mouse_click', x=20, y=20), 'delete every note', 'Tümünü Sil'),
    )
    for name, action, sub_goal, label in cases:
        turn: AgentTurn = AgentTurn(thought='audit', sub_goal=sub_goal, action=action)
        risk = classify_risk(turn, target_label=label)
        emit(name, {'risk': risk.value, 'permission': decide_permission(AutonomyLevel.FULL, risk).value})
    now: datetime = datetime.now(UTC)
    grant = new_grant(verb='delete', app='Mail', target_pattern='*', max_invocations=1,
                      expires_at=now + timedelta(hours=1), note='delete only', now=now)
    send_turn: AgentTurn = AgentTurn(thought='audit', sub_goal='remove the old draft',
                                    action=MouseClick(type='mouse_click', x=20, y=20))
    grant_result = authorize(send_turn.action, sub_goal=send_turn.sub_goal, target_label='Send',
                             app='Mail', grants=(grant,), now=now)
    emit('delete_grant_on_send_button', grant_result.model_dump(mode='json'))


def coordinate_probe() -> None:
    element: AXElement = AXElement(role='Button', title='Save', value='', focused=False,
                                   x=-1200, y=100, width=80, height=24, children=())
    summary: str = element_summary(element)
    screen_map: ScreenMap = ScreenMap(logical=Size(1440, 900), image=Size(512, 320), origin=Point(-1440, 0))
    marks: tuple[MarkElement, ...] = parse_ax_elements_to_marks((summary,))
    emit('negative_ax_marks', {'summary': summary, 'parsed_marks': len(marks),
                               'image_summary': summaries_to_image_space((summary,), screen_map)})
    mark: MarkElement = MarkElement(index=1, label=summary, role='Button',
                                    rect=Rect(Point(-1200, 100), Size(80, 24)))
    emit('negative_mark_resolution', resolve_mark(ClickMark(type='click_mark', mark=1), (mark,)).model_dump())


def memory_and_pixel_probe() -> None:
    save: Trajectory = Trajectory(app='Notes', description='save a draft',
        steps=(MouseClick(type='mouse_click', x=20, y=20), MouseClick(type='mouse_click', x=80, y=20)),
        step_descriptions=('open document', 'Save'))
    delete: Trajectory = Trajectory(app='Notes', description='delete a draft',
        steps=(MouseClick(type='mouse_click', x=20, y=20), MouseClick(type='mouse_click', x=180, y=20)),
        step_descriptions=('open document', 'Delete'))
    emit('save_delete_signature_collision', {'save': signature_of(save), 'delete': signature_of(delete),
                                           'second_distill': distill(delete, {signature_of(save)}).kind})
    before: tuple[tuple[float, ...], ...] = tuple((0.0,) * 48 for _ in range(48))
    after: tuple[tuple[float, ...], ...] = tuple((1.0,) * 48 for _ in range(48))
    verification: Verification = Verification(Rect(Point(0, 0), Size(48, 48)), verdict(before, after))
    pixel: Evidence = Evidence.CONFIRMED if verification.changed else Evidence.CONTRADICTED
    emit('noise_confirmation', {'kind': verification.verdict.kind.value, 'changed': verification.changed,
                               'combined_with_static_ax': combine(direct=(), circumstantial=(Evidence.CONTRADICTED, pixel)).value})


def kill_and_entry_probe() -> None:
    samples: list[CursorSample] = []

    def cursor() -> CursorSample:
        sample: CursorSample = CursorSample(x=100 if len(samples) % 2 else 0, y=0, time=float(len(samples)))
        samples.append(sample)
        return sample

    monitor: MouseShakeMonitor = MouseShakeMonitor(cursor, window_size=20, min_reversals=6)
    switch: KillSwitch = KillSwitch(monitor=monitor, signal_predicate=lambda: False)
    emit('kill_monitor_shadowed', {'trip_results': [switch.tripped() for _ in range(12)], 'samples_taken': len(samples)})
    physical: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        action: Action = (PressHotkey(type='press_hotkey', modifiers=[], key='a') if state.step_index == 0
                          else Finish(type='finish', status='failed', summary='audit complete'))
        return AgentTurn(thought='audit', sub_goal='continue', action=action)

    runner: OodaRunner = OodaRunner(provider=provider, execute_physical=lambda action: physical.append(action.type),
                                    guard=guarded(AutonomyLevel.FULL, authorize=None, auto_approve=False),
                                    ax_probe=lambda: AxProbeResult(summaries=(), asks_for_credential=True), max_steps=3)
    runner.run('audit credential guard')
    emit('secure_field_hotkey_dispatch', physical)
    focus_reads: list[str] = []
    runner.window_probe = lambda: focus_reads.append('read')  # type: ignore[assignment,func-returns-value]
    runner._guard_positional(TypeText(type='type_text', text='audit'))
    emit('type_text_focus_reads', focus_reads)
    runner._observation = replace(runner._observation, asks_for_credential=False)


def main() -> None:
    policy_probe()
    coordinate_probe()
    memory_and_pixel_probe()
    kill_and_entry_probe()


if __name__ == '__main__':
    main()
