from __future__ import annotations

import json
from typing import Never

from computeruse.orchestrator.loop import AxProbeResult, OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.providers.openai import OpenAIError
from computeruse.vision.capture import ScreenCapture
from computeruse.vision.coordinates import CoordinateOutOfBoundsError, Point


def main() -> None:
    effects: list[tuple[float, float]] = []
    frame: ScreenCapture = ScreenCapture(display_id=1, width=100, height=100, scale=1.0,
                                         data=bytes((0, 0, 0, 255)) * 10000)

    def quiet_press(point: Point) -> bool:
        effects.append((point.x, point.y))
        return True

    def provider(state: WorkingState) -> AgentTurn:
        return AgentTurn(thought='audit', sub_goal='audit',
                         action=Finish(type='finish', status='success', summary='audit'))

    runner: OodaRunner = OodaRunner(provider=provider, execute_physical=lambda action: None,
                                    sensor=lambda: frame, quiet_press=quiet_press)
    try:
        runner._act_and_verify(MouseClick(type='mouse_click', x=200, y=20))
    except CoordinateOutOfBoundsError as exc:
        print(json.dumps({'probe': 'quiet_effect_before_bounds_check',
                          'effects': effects, 'subsequent_error': type(exc).__name__}))

    def unavailable_auditor(state: WorkingState, summary: str) -> Never:
        raise OpenAIError('audit transport unavailable')

    checker: OodaRunner = OodaRunner(provider=provider, execute_physical=lambda action: None,
                                     ax_probe=lambda: AxProbeResult(summaries=()),
                                     completion_check=unavailable_auditor)
    result: WorkingState = checker.run('audit')
    print(json.dumps({'probe': 'audit_transport_failure_accepts_success',
                      'completed_steps': result.completed_steps, 'forced_finish': checker._forced_finish,
                      'last_error': result.last_error}))


if __name__ == '__main__':
    main()
