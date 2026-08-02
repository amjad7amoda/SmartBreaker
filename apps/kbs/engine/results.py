from dataclasses import dataclass, field
from .facts import AlertFact, CommandFact, DecisionFact


@dataclass
class ActionIntent:
    breaker_id: int        
    device_id: str         
    action: str            
    reason: str            
    lockout: bool = False  
    countdown_s: int = 0   


@dataclass
class AlertIntent:
    kind: str      # Alert.KIND_CHOICES code (text)
    severity: str  # 'info' | 'warning' | 'critical'
    message: str   # human-readable description (text)


@dataclass
class RuleResult:
    branch: str = ''                             # decision-tree path code (text)
    actions: list = field(default_factory=list)  # switch commands to execute (list[ActionIntent])
    alerts: list = field(default_factory=list)   # notifications to raise (list[AlertIntent])

    @classmethod
    def from_working_memory(cls, facts):
        result = cls()
        for fact in facts:
            if isinstance(fact, DecisionFact):
                result.branch = fact['branch']
            elif isinstance(fact, CommandFact):
                result.actions.append(ActionIntent(**fact.as_dict()))
            elif isinstance(fact, AlertFact):
                result.alerts.append(AlertIntent(**fact.as_dict()))
        return result
