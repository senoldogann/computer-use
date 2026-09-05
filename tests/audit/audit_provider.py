from __future__ import annotations

from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.schemas import AgentTurn, Finish, TypeText


def propose_delete(state: WorkingState) -> AgentTurn:
    return AgentTurn(thought='audit', sub_goal='delete the disposable placeholder',
                     action=TypeText(type='type_text', text='rm -f /tmp/audit-placeholder'))


def finish_failure(state: WorkingState) -> AgentTurn:
    if state.step_index == 0:
        return AgentTurn(thought='audit', sub_goal='enter the disposable fixture',
                         action=TypeText(type='type_text', text='audit fixture', wpm=600))
    return AgentTurn(thought='audit', sub_goal='report that the work failed',
                     action=Finish(type='finish', status='failed', summary='intentional audit failure'))
