from __future__ import annotations

from pathlib import Path


def replace_between(path: str, start: str, end: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    start_count = text.count(start)
    end_count = text.count(end)
    if start_count != 1 or end_count != 1:
        raise SystemExit(
            f"{path}: expected one boundary each; start={start_count} end={end_count}"
        )
    i = text.index(start)
    j = text.index(end, i)
    p.write_text(text[:i] + replacement.rstrip() + "\n\n" + text[j:], encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, got {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/computeruse/orchestrator/loop.py",
    'STALEMATE_RETROSPECTIVE: Final[str] = (\n    "Görev tamamlanamadı: ekran kanıtı doğrulanamadı ve auditor reddetti."\n)',
    'STALEMATE_RETROSPECTIVE: Final[str] = (\n    "Görev tamamlanamadı: tamamlanma kanıtı bağımsız olarak doğrulanamadı."\n)',
)

replace_between(
    "src/computeruse/orchestrator/loop.py",
    "    def _audit_completion(self, state: WorkingState, finish: Finish) -> str | None:\n",
    "    def _finalize(\n",
    '''    def _audit_completion(self, state: WorkingState, finish: Finish) -> str | None:
        """Challenge a success claim; return the rejection reason, or None.

        A configured completion checker is an independent verification boundary.
        If it cannot answer, the actor's own success claim cannot substitute for
        evidence. Verification outages and empty evidence are therefore bounded
        recoverable rejections; after the existing rejection budget is exhausted,
        the run closes as an honest unverified failure.
        """
        if self.sensor is None and self.window_probe is None and self.ax_probe is None:
            # Headless/scripted callers intentionally have no observable surface.
            # Preserve that legacy shape: when there is nothing to verify and no
            # production perception is wired, the model claim remains accepted.
            return None
        if self.vision_enabled and not state.screenshot_b64:
            raise RuntimeError(
                "finish verification unavailable: the latest screenshot is missing"
            )
        if self.completion_check is None:
            return None

        if self._rejected_finishes >= MAX_FINISH_REJECTIONS:
            # One final verification attempt lets an actor that repaired the state
            # after earlier rejections still finish normally.
            try:
                verdict = self.completion_check(state, finish.summary)
            except Exception as exc:  # noqa: BLE001
                self._forced_finish = True
                self._stalemate_rejected = True
                LOGGER.warning(
                    "completion audit unavailable after bounded retries: %s", exc
                )
                return None
            evidence = verdict.evidence.strip()
            if verdict.satisfied and evidence:
                LOGGER.info("ooda completion audited (after recovery): %s", evidence)
                return None
            self._forced_finish = True
            self._stalemate_rejected = True
            LOGGER.warning(
                "completion claims remained unverifiable after %d rejections",
                self._rejected_finishes,
            )
            return None

        try:
            verdict = self.completion_check(state, finish.summary)
        except Exception as exc:  # noqa: BLE001 - verification failure is recoverable
            self._rejected_finishes += 1
            evidence = f"completion audit unavailable: {type(exc).__name__}: {exc}"
            LOGGER.warning("completion audit unavailable: %s", exc)
            return (
                "completion check could not verify this finish: "
                f"{evidence} "
                "The success claim is unverified. Continue only if another action can "
                "produce observable evidence; otherwise emit finish with status "
                "\"failed\" and explain the verification outage."
            )

        evidence = verdict.evidence.strip()
        if verdict.satisfied and evidence:
            LOGGER.info("ooda completion audited: %s", evidence)
            return None

        self._rejected_finishes += 1
        reason = evidence or "completion auditor returned no evidence"
        return (
            "completion check rejected this finish: "
            f"{reason} "
            "The goal is not yet independently verified. Either continue working "
            "toward observable evidence, or emit finish with status \"failed\" and "
            "explain what blocked verification."
        )''',
)

replace_between(
    "src/computeruse/orchestrator/prompts.py",
    "def parse_completion(raw: str) -> CompletionVerdict:\n",
    "def completion_auditor(\n",
    '''def parse_completion(raw: str) -> CompletionVerdict:
    """Parse an auditor reply into a fully evidenced verdict.

    A completion verdict is a verification result, not a model opinion. Both
    the boolean and a non-empty evidence string are therefore mandatory. A
    malformed or unevidenced reply raises :class:`InvalidDecisionError`; the
    loop treats that as a bounded verification failure and fails closed if the
    independent checker never becomes available.
    """
    candidate = _first_json_object(raw.strip())
    if candidate is None:
        raise InvalidDecisionError(
            cause="no JSON object found in the completion reply",
            hint='reply with {"satisfied": true|false, "evidence": "..."}',
        )
    try:
        payload: object = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise InvalidDecisionError(
            cause=f"invalid JSON: {exc}",
            hint='reply with {"satisfied": true|false, "evidence": "..."}',
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidDecisionError(
            cause="root JSON element is not an object",
            hint='reply with {"satisfied": true|false, "evidence": "..."}',
        )
    typed = cast(dict[str, object], payload)
    satisfied = typed.get("satisfied")
    if not isinstance(satisfied, bool):
        raise InvalidDecisionError(
            cause=f"'satisfied' must be a boolean, got {type(satisfied).__name__}",
            hint='"satisfied" must be exactly true or false',
        )
    evidence = typed.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise InvalidDecisionError(
            cause="'evidence' must be a non-empty string",
            hint='"evidence" must state the independent observation supporting the verdict',
        )
    return CompletionVerdict(satisfied=satisfied, evidence=evidence.strip())''',
)

replace_between(
    "tests/smoke/test_prompts.py",
    "def test_completion_parser_rejects_a_missing_verdict() -> None:\n",
    "def test_completion_auditor_reads_the_attached_screenshot() -> None:\n",
    '''def test_completion_parser_rejects_a_missing_verdict() -> None:
    """A completion reply needs both a boolean verdict and real evidence."""
    from computeruse.orchestrator.prompts import parse_completion

    verdict = parse_completion(
        '{"satisfied": true, "evidence": "the page shows Signed out"}'
    )
    assert verdict.satisfied is True
    assert "Signed out" in verdict.evidence
    for bad in (
        '{"satisfied": false}',
        '{"satisfied": "yes"}',
        "{}",
        "not json at all",
    ):
        with pytest.raises(InvalidDecisionError):
            parse_completion(bad)''',
)
