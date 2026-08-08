"""Application services that orchestrate the pure KBS through an adapter."""

import logging

from .engine import decide


logger = logging.getLogger(__name__)


def run_cycle(organization, now=None, adapter=None):
    if adapter is None:
        # Lazy import keeps the application service testable with a fake
        # adapter in environments that do not even have Django installed.
        from .adapters import DjangoKBSAdapter
        adapter = DjangoKBSAdapter()
    settings = adapter.get_settings(organization)
    if settings.mode != 'active':
        logger.info('KBS observing, no actions: org=%s', organization.id)
        return None

    cycle_time = adapter.resolve_cycle_time(
        organization,
        settings,
        requested_now=now,
    )
    if cycle_time is None:
        logger.warning(
            'KBS skipped, no cycle time/telemetry: org=%s',
            organization.id,
        )
        return None

    facts = adapter.build_facts(organization, settings, cycle_time)
    if facts is None:
        logger.warning('KBS skipped, no facts: org=%s', organization.id)
        return None

    make_decision = getattr(adapter, 'make_decision', None)
    result = (
        make_decision(organization, facts, decide)
        if make_decision is not None
        else decide(facts)
    )
    decision = adapter.persist_result(organization, facts, result)
    logger.info(
        'KBS decision persisted: org=%s branch=%s',
        organization.id,
        decision.branch,
    )
    return decision
