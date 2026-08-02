"""The Tier-2 KBS: an Experta production system over the site's telemetry.

    gathering.py   database + clock + units  ->  the cycle's facts
    facts.py       the fact templates the rules match on
    rules.py       the knowledge base: the flowchart as @Rule declarations
    strategies.py  what a rule may do when it fires
    grouping.py    plug-in: staggered turn-on groups / best-subset selection
    weather.py     plug-in: weather API (season works already)
    derived.py     pure math shared by the above
    actions.py     run_cycle(): gather -> run the rules -> persist
"""

from .actions import run_cycle
from .facts import (
    AlertFact,
    BreakerFact,
    CommandFact,
    DecisionFact,
    SystemFact,
    breaker_facts,
    system_fact,
)
from .gathering import facts_to_json, gather_facts
from .results import ActionIntent, AlertIntent, RuleResult
from .rules import SmartBreakerKBS, decide

__all__ = [
    'run_cycle', 'gather_facts', 'facts_to_json', 'decide', 'SmartBreakerKBS',
    'SystemFact', 'BreakerFact', 'DecisionFact', 'CommandFact', 'AlertFact',
    'system_fact', 'breaker_facts',
    'RuleResult', 'ActionIntent', 'AlertIntent',
]
