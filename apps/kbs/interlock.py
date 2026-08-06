"""Pure coordination contract that mirrors an authoritative Tier-1 decision.

This module does not inspect sensor thresholds and is deliberately not a third
KBS. Tier-1 has already selected the danger situation and commands before a
Tier1SafetySnapshot reaches this boundary.
"""

from dataclasses import dataclass

from .engine.rules import ActionIntent, RuleResult, TRACE_VERSION


INTERLOCK_BRANCH_PREFIX = 'tier1_interlock.'


@dataclass(frozen=True)
class Tier1SafetyCommand:
    device_id: str
    action: str
    countdown_s: int = 0
    reason: str = ''


@dataclass(frozen=True)
class Tier1SafetySnapshot:
    active: bool
    situation: str = ''
    episode_id: str = ''
    source_event_id: str = ''
    commands: tuple = ()


def is_interlock_result(result):
    return result.branch.startswith(INTERLOCK_BRANCH_PREFIX)


def mirror_tier1_decision(facts, safety):
    """Return Tier-2's agreement with Tier-1's desired safety state."""
    trace = [{
        'code': 'tier2.interlock.tier1_active',
        'kind': 'interlock',
        'outcome': 'selected',
        'summary': 'Tier-1 safety is active; normal Tier-2 rules were bypassed.',
        'evidence': {
            'situation': safety.situation,
            'episode_id': safety.episode_id,
            'source_event_id': safety.source_event_id,
        },
    }]
    result = RuleResult(branch=f'{INTERLOCK_BRANCH_PREFIX}{safety.situation}')
    breakers = {breaker.device_id: breaker for breaker in facts.breakers}

    for command in safety.commands:
        breaker = breakers.get(command.device_id)
        if breaker is None:
            trace.append({
                'code': 'tier2.interlock.breaker_missing',
                'kind': 'interlock',
                'outcome': 'ignored',
                'summary': f'{command.device_id} is absent from current Tier-2 facts.',
                'evidence': {
                    'device_id': command.device_id,
                    'requested_action': command.action,
                },
            })
            continue
        desired_state = command.action == 'on'
        if breaker.switch == desired_state:
            trace.append({
                'code': 'tier2.interlock.already_satisfied',
                'kind': 'execution',
                'outcome': 'noop',
                'summary': f'{command.device_id} already matches Tier-1 safety.',
                'evidence': {
                    'device_id': command.device_id,
                    'requested_action': command.action,
                    'actual_state': breaker.switch,
                },
            })
            continue
        result.actions.append(ActionIntent(
            breaker_id=breaker.id,
            device_id=breaker.device_id,
            action=command.action,
            countdown_s=command.countdown_s,
            reason=command.reason,
        ))
        trace.append({
            'code': 'tier2.interlock.mirror_action',
            'kind': 'output',
            'outcome': 'emitted',
            'summary': f'{command.device_id} -> {command.action}, mirroring Tier-1.',
            'evidence': {
                'device_id': command.device_id,
                'action': command.action,
                'countdown_s': command.countdown_s,
                'reason': command.reason,
            },
        })

    trace.append({
        'code': 'tier2.interlock.complete',
        'kind': 'branch',
        'outcome': 'selected',
        'summary': 'Tier-2 completed the cycle without running its normal decision tree.',
        'evidence': {
            'branch': result.branch,
            'mirrored_action_count': len(result.actions),
        },
    })
    result.trace_version = TRACE_VERSION
    result.trace = trace
    return result
