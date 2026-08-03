"""KBS control API.

These endpoints close the control loop with whatever executes the switches —
today the browser simulator, later the Raspberry Pi:

  POST /api/kbs/sim/run-cycle/   trigger one decision cycle now
  GET  /api/kbs/sim/state/       settings + latest decision + pending actions + recent alerts
  POST /api/kbs/sim/ack/         confirm actions were applied (executed=True)
  PATCH /api/kbs/settings/       change K, mode, power saving, data source
"""

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import Organization

from .models import Alert, BreakerAction, KBSDecision, KBSSettings
from .services import run_cycle

SETTINGS_EDITABLE_FIELDS = (
    'cycle_seconds', 'power_saving', 'mode', 'data_source',
    'battery_low_voltage_V', 'battery_low_margin_V', 'battery_shutdown_buffer_percent',
    'joule_deficit_limit_J', 'grid_present_min_V',
)  # fields PATCH may change


def _org_or_none(request, from_query=True):
    """Resolve the organization from ?organization= (GET/PATCH) or the body (POST)."""
    org_id = (request.query_params if from_query else request.data).get('organization')
    return Organization.objects.filter(id=org_id).first()


def _action_dict(action):
    """One BreakerAction as the executor (simulator/Pi) needs it."""
    return {
        'id': action.id,
        'device_id': action.breaker.device_id,
        'action': action.action,                 # target relay state: 'on' | 'off'
        'countdown_s': action.countdown_s,       # 0 = switch now; >0 = arm the device countdown (s)
        'reason': action.reason,
        'branch': action.decision.branch,        # decision-tree path that produced it
        'created_at': action.created_at,
    }


class RunCycleView(APIView):
    """POST {organization}: run one KBS decision cycle immediately.

    Lets the simulator drive the K-cadence itself without Celery running.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        org = _org_or_none(request, from_query=False)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        decision = run_cycle(org)
        if decision is None:
            return Response({
                'engine': 'apps.kbs.services.run_cycle',
                'branch': None,
                'facts': None,
                'actions': [],
                'detail': 'skipped (observing mode or no readings)',
            })
        return Response({
            'engine': 'apps.kbs.services.run_cycle',
            'branch': decision.branch,
            'created_at': decision.created_at,
            'facts': decision.facts,
            'actions': [_action_dict(a) for a in decision.actions.select_related('breaker', 'decision')],
        })


class SimStateView(APIView):
    """GET ?organization=: everything the executor UI needs in one call."""

    permission_classes = [AllowAny]

    def get(self, request):
        org = _org_or_none(request)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        kbs, _ = KBSSettings.objects.get_or_create(organization=org)
        latest = KBSDecision.objects.filter(organization=org).first()  # newest (Meta orders by -created_at)
        # Switch commands not yet applied. Only the newest per breaker is sent:
        # an older unexecuted command for the same breaker is obsolete — the
        # later decision supersedes it — and replaying it would fight the engine.
        newest_per_breaker = {}  # breaker_id -> newest pending BreakerAction
        for action in (
            BreakerAction.objects
            .filter(breaker__organization=org, executed=False)
            .select_related('breaker', 'decision')
            .order_by('created_at')
        ):
            newest_per_breaker[action.breaker_id] = action
        pending = sorted(newest_per_breaker.values(), key=lambda a: a.created_at)
        alerts = Alert.objects.filter(organization=org)[:10]  # newest first (Meta ordering)
        return Response({
            'settings': {f: getattr(kbs, f) for f in SETTINGS_EDITABLE_FIELDS},
            'latest_decision': (
                {
                    'engine': 'apps.kbs.services.run_cycle',
                    'branch': latest.branch,
                    'created_at': latest.created_at,
                    'facts': latest.facts,
                } if latest else None
            ),
            'pending_actions': [_action_dict(a) for a in pending],
            'recent_alerts': [
                {'kind': a.kind, 'severity': a.severity, 'message': a.message, 'created_at': a.created_at}
                for a in alerts
            ],
        })


class AckActionsView(APIView):
    """POST {action_ids: [...]}: mark actions as applied by the executor."""

    permission_classes = [AllowAny]

    def post(self, request):
        ids = request.data.get('action_ids', [])
        updated = BreakerAction.objects.filter(id__in=ids, executed=False).update(executed=True)
        return Response({'acknowledged': updated})


class SettingsView(APIView):
    """PATCH ?organization=: update the editable engine settings (K, mode, ...)."""

    permission_classes = [AllowAny]

    def patch(self, request):
        org = _org_or_none(request)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        kbs, _ = KBSSettings.objects.get_or_create(organization=org)
        changed = {}  # field -> new value, echoed back
        for field in SETTINGS_EDITABLE_FIELDS:
            if field in request.data:
                setattr(kbs, field, request.data[field])
                changed[field] = request.data[field]
        try:
            kbs.full_clean(exclude=['organization'])
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        kbs.save()
        return Response({'updated': changed})
