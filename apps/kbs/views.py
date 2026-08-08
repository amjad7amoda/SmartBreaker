"""KBS control and closed-loop simulator API."""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.breakers.models import Breaker, BreakerReading, BreakerStatus
from apps.organizations.models import Organization
from apps.telemetry.models import Reading

from .climate import CLIMATE_CSV_PATH, ClimateDataError, load_climate_rows
from .contracts import TIER2_ENGINE
from .engine.fuzzy import PROFILE_VERSION
from .models import (
    Alert, BreakerAction, KBSControllerState, KBSDecision, KBSSettings,
    Tier1SafetyState,
)
from .services import run_cycle

SETTINGS_EDITABLE_FIELDS = (
    'cycle_seconds', 'power_saving', 'mode', 'data_source', 'tier2_policy',
    'battery_capacity_Wh', 'night_reserve_percent', 'max_inverter_power_W',
    'battery_low_voltage_V', 'battery_low_margin_V', 'battery_shutdown_buffer_percent',
    'joule_deficit_limit_J', 'grid_present_min_V',
)
SETTINGS_SHARED_FIELDS = SETTINGS_EDITABLE_FIELDS + (
    'stability_threshold_percent',
    'event_stability_threshold_percent', 'heatsink_temp_limit_C',
    'deficit_window_minutes', 'sudden_drop_fraction',
    'sudden_draw_W', 'baseline_minutes', 'motor_peak_minutes', 'event_prep_hours',
    'day_start', 'day_end', 'pv_day_min_W',
)


def _org_or_none(request, from_query=True):
    org_id = (request.query_params if from_query else request.data).get('organization')
    return Organization.objects.filter(id=org_id).first()


def _action_dict(action):
    return {
        'id': action.id,
        'action_id': str(action.action_id),
        'device_id': action.device_id,
        'action': action.action,
        'countdown_s': action.countdown_s,
        'reason': action.reason,
        'status': action.status,
        'resulting_state': action.resulting_state,
        'executed_at': action.executed_at,
        'failure_reason': action.failure_reason,
        'branch': action.decision.branch,
        'created_at': action.created_at,
    }


def _reading_dict(reading):
    if reading is None:
        return None
    result = {'organization': reading.organization_id}
    for field in Reading._meta.concrete_fields:
        if field.name in ('pk', 'organization'):
            continue
        result[field.name] = getattr(reading, field.name)
    return result


def _breaker_dict(breaker):
    try:
        current = breaker.status
    except BreakerStatus.DoesNotExist:
        current = None
    return {
        'device_id': breaker.device_id,
        'priority_type': breaker.priority_type,
        'priority_degree': breaker.priority_degree,
        'load_type': breaker.load_type,
        'peak_load_W': breaker.peak_load_W,
        'mean_load_W': breaker.mean_load_W,
        'cycle_start': breaker.cycle_start,
        'cycle_end': breaker.cycle_end,
        'switch': current.switch if current else None,
        'countdown_1_s': current.countdown_1_s if current else 0,
        'online': current.online if current else None,
        'fault': current.fault if current else '',
        'child_lock': breaker.child_lock,
        'locked_out': breaker.locked_out,
        'lockout_reason': breaker.lockout_reason,
        'locked_at': breaker.locked_at,
        'reported_at': current.reported_at if current else None,
    }


def _tier1_safety_dict(safety):
    if safety is None:
        return {
            'active': False,
            'situation': '',
            'episode_id': None,
            'source_event_id': None,
            'commands': [],
            'source_occurred_at': None,
            'activated_at': None,
            'cleared_at': None,
            'updated_at': None,
        }
    return {
        'active': safety.active,
        'situation': safety.situation,
        'episode_id': str(safety.episode_id) if safety.episode_id else None,
        'source_event_id': (
            str(safety.source_decision.event_id)
            if safety.source_decision_id else None
        ),
        'commands': safety.commands,
        'source_occurred_at': safety.source_occurred_at,
        'activated_at': safety.activated_at,
        'cleared_at': safety.cleared_at,
        'updated_at': safety.updated_at,
    }


def _fuzzy_evaluation(decision):
    if decision is None or not isinstance(decision.facts, dict):
        return {}
    value = decision.facts.get('fuzzy_evaluation', {})
    return value if isinstance(value, dict) else {}


def _controller_state_dict(state):
    if state is None:
        return {
            'current_band': 'watch',
            'candidate_band': None,
            'consecutive_cycles': 0,
            'last_risk_score': None,
            'last_evaluated_at': None,
            'profile_version': PROFILE_VERSION,
        }
    return {
        'current_band': state.current_band,
        'candidate_band': state.candidate_band or None,
        'consecutive_cycles': state.consecutive_cycles,
        'last_risk_score': state.last_risk_score,
        'last_evaluated_at': state.last_evaluated_at,
        'profile_version': state.profile_version,
    }


class ClimateView(APIView):
    """Return validated source climatology; never substitute invented values."""

    permission_classes = [AllowAny]

    def get(self, request):
        try:
            rows = list(load_climate_rows(str(CLIMATE_CSV_PATH)))
        except ClimateDataError as exc:
            return Response(
                {'detail': str(exc), 'source': str(CLIMATE_CSV_PATH)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        cities = sorted({row['city'] for row in rows})
        city = request.query_params.get('city')
        month = request.query_params.get('month')
        if city:
            rows = [row for row in rows if row['city'] == city]
        if month is not None:
            try:
                month_number = int(month)
            except (TypeError, ValueError):
                return Response({'detail': 'month must be an integer from 1 to 12'}, status=400)
            if month_number not in range(1, 13):
                return Response({'detail': 'month must be an integer from 1 to 12'}, status=400)
            rows = [row for row in rows if row['month'] == month_number]
        return Response({'cities': cities, 'count': len(rows), 'rows': rows})


class RunCycleView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        org = _org_or_none(request, from_query=False)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        decision = run_cycle(org)
        if decision is None:
            kbs, _ = KBSSettings.objects.get_or_create(organization=org)
            return Response({
                'engine': TIER2_ENGINE,
                'policy': kbs.tier2_policy,
                'branch': None,
                'facts': None,
                'fuzzy_evaluation': {},
                'counterfactual': {},
                'actions': [],
                'detail': 'skipped (observing mode or no readings)',
            })
        return Response({
            'engine': TIER2_ENGINE,
            'event_id': str(decision.event_id),
            'policy': decision.policy,
            'branch': decision.branch,
            'trace_version': decision.trace_version,
            'trace': decision.trace,
            'created_at': decision.created_at,
            'facts': decision.facts,
            'fuzzy_evaluation': _fuzzy_evaluation(decision),
            'counterfactual': decision.counterfactual,
            'actions': [_action_dict(a) for a in decision.actions.select_related('breaker', 'decision')],
        })


class SimStateView(APIView):
    """Return engine settings, source state, pending work, metadata and lockouts."""

    permission_classes = [AllowAny]

    def get(self, request):
        org = _org_or_none(request)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        kbs, _ = KBSSettings.objects.get_or_create(organization=org)
        # Live state follows server receipt order. Historical/simulated event
        # time can move backwards and remains available as occurred_at.
        latest = KBSDecision.objects.filter(
            organization=org, tier='tier2',
        ).order_by('-received_at', '-id').first()
        newest_per_breaker = {}
        for action in (
            BreakerAction.objects
            .filter(decision__organization=org, decision__tier='tier2', status__in=('pending', 'scheduled'))
            .select_related('breaker', 'decision')
            .order_by('created_at')
        ):
            newest_per_breaker[action.device_id] = action
        pending = sorted(newest_per_breaker.values(), key=lambda action: action.created_at)
        alerts = Alert.objects.filter(organization=org, suppressed=False)[:10]
        safety = Tier1SafetyState.objects.filter(
            organization=org,
        ).select_related('source_decision').first()
        controller_state = KBSControllerState.objects.filter(
            organization=org,
        ).first()
        latest_reading = Reading.objects.filter(organization=org).first()
        breakers = Breaker.objects.filter(organization=org).select_related('status')
        return Response({
            'organization': {
                'id': org.id,
                'name': org.name,
                'latitude': float(org.latitude),
                'longitude': float(org.longitude),
                'status': org.status,
            },
            'settings': {field: getattr(kbs, field) for field in SETTINGS_SHARED_FIELDS},
            'latest_telemetry': _reading_dict(latest_reading),
            'tier1_safety': _tier1_safety_dict(safety),
            'breakers': [_breaker_dict(breaker) for breaker in breakers],
            'metadata': {
                'engine': TIER2_ENGINE,
                'data_source': kbs.data_source,
                'policy': kbs.tier2_policy,
                'fuzzy_profile': PROFILE_VERSION,
                'generated_at': timezone.now(),
            },
            'policy': kbs.tier2_policy,
            'fuzzy_evaluation': _fuzzy_evaluation(latest),
            'counterfactual': latest.counterfactual if latest else {},
            'controller_state': _controller_state_dict(controller_state),
            'latest_decision': (
                {
                    'event_id': str(latest.event_id),
                    'engine': latest.engine,
                    'tier': latest.tier,
                    'branch': latest.branch,
                    'policy': latest.policy,
                    'fuzzy_evaluation': _fuzzy_evaluation(latest),
                    'counterfactual': latest.counterfactual,
                    'trace_version': latest.trace_version,
                    'legacy': latest.is_legacy,
                    'trace': latest.trace,
                    'occurred_at': latest.occurred_at,
                    'received_at': latest.received_at,
                    'created_at': latest.created_at,
                    'facts': latest.facts,
                } if latest else None
            ),
            'pending_actions': [_action_dict(action) for action in pending],
            'recent_alerts': [
                {'kind': alert.kind, 'severity': alert.severity, 'message': alert.message, 'created_at': alert.created_at}
                for alert in alerts
            ],
        })


class AckActionsView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ids = request.data.get('action_ids', [])
        updated = 0
        ignored = 0
        simulator_actions = BreakerAction.objects.filter(
            decision__organization__kbs_settings__data_source='simulator',
        )
        for action in simulator_actions.filter(id__in=ids):
            if action.status in BreakerAction.RESOLVED_STATUSES:
                ignored += 1
                continue
            action.status = 'applied'
            action.resulting_state = action.action == 'on'
            action.executed_at = timezone.now()
            action.save()
            updated += 1
        for payload in request.data.get('results', []):
            if payload.get('id'):
                action = simulator_actions.filter(id=payload['id']).first()
            else:
                action = simulator_actions.filter(
                    action_id=payload.get('action_id'),
                ).first()
            if action is None:
                continue
            if action.status in BreakerAction.RESOLVED_STATUSES:
                ignored += 1
                continue
            action.status = payload.get('status', action.status)
            action.resulting_state = payload.get('resulting_state', action.resulting_state)
            action.failure_reason = str(payload.get('failure_reason') or '')[:500]
            action.executed_at = timezone.now()
            action.save()
            updated += 1
        return Response({'acknowledged': updated, 'ignored_resolved': ignored})


class SettingsView(APIView):
    permission_classes = [AllowAny]

    def patch(self, request):
        org = _org_or_none(request)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        kbs, _ = KBSSettings.objects.get_or_create(organization=org)
        changed = {}
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


class SimulatorOnlyMixin:
    def simulator_org(self, request):
        org = _org_or_none(request, from_query=False)
        if org is None:
            return None, Response({'detail': 'unknown organization'}, status=404)
        settings_row = KBSSettings.objects.filter(organization=org).first()
        if settings_row is None or settings_row.data_source != 'simulator':
            return None, Response({'detail': 'operation is restricted to simulator organizations'}, status=403)
        return org, None


class SimResetView(SimulatorOnlyMixin, APIView):
    """Clear scoped run history while preserving configuration and definitions."""

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        org, error = self.simulator_org(request)
        if error:
            return error
        if request.data.get('confirm') is not True:
            return Response({'detail': 'confirm must be true'}, status=400)
        deleted = {
            'telemetry': Reading.objects.filter(organization=org).count(),
            'breaker_readings': BreakerReading.objects.filter(breaker__organization=org).count(),
            'decisions': KBSDecision.objects.filter(organization=org).count(),
            'alerts': Alert.objects.filter(organization=org).count(),
            'controller_states': KBSControllerState.objects.filter(
                organization=org,
            ).count(),
        }
        Reading.objects.filter(organization=org).delete()
        BreakerReading.objects.filter(breaker__organization=org).delete()
        KBSDecision.objects.filter(organization=org).delete()
        Alert.objects.filter(organization=org).delete()
        Tier1SafetyState.objects.filter(organization=org).delete()
        KBSControllerState.objects.filter(organization=org).delete()
        Breaker.objects.filter(organization=org).update(
            child_lock=False, locked_out=False, lockout_reason='', locked_at=None,
        )
        BreakerStatus.objects.filter(breaker__organization=org).update(
            countdown_1_s=0, child_lock=False,
        )
        return Response({'reset': True, 'organization': org.id, 'deleted': deleted})


class BreakerOverrideView(SimulatorOnlyMixin, APIView):
    """Apply an explicit physical simulator switch and record its reading."""

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        org, error = self.simulator_org(request)
        if error:
            return error
        device_id = request.data.get('device_id')
        switch = request.data.get('switch')
        if not device_id or type(switch) is not bool:
            return Response({'detail': 'device_id and boolean switch are required'}, status=400)
        timestamp = parse_datetime(str(request.data.get('timestamp', '')))
        if timestamp is None:
            return Response({'detail': 'timestamp must be an ISO-8601 datetime'}, status=400)
        if timezone.is_naive(timestamp):
            timestamp = timezone.make_aware(timestamp)
        breaker = (
            Breaker.objects.select_for_update()
            .filter(organization=org, device_id=device_id)
            .first()
        )
        if breaker is None:
            return Response({'detail': 'breaker does not belong to the selected simulator organization'}, status=404)
        current, _ = BreakerStatus.objects.select_for_update().get_or_create(breaker=breaker)
        was_on = current.switch
        current.switch = switch
        current.countdown_1_s = 0
        current.online = True
        if switch and not was_on:
            current.last_switched_on_at = timestamp
        if switch:
            current.child_lock = False
        current.save()
        if switch:
            breaker.child_lock = False
            breaker.locked_out = False
            breaker.lockout_reason = ''
            breaker.locked_at = None
            breaker.save(update_fields=['child_lock', 'locked_out', 'lockout_reason', 'locked_at'])
        BreakerReading.objects.update_or_create(
            breaker=breaker,
            timestamp=timestamp,
            defaults={'switch': switch, 'cur_power_mW': current.cur_power_mW},
        )
        return Response({
            'organization': org.id,
            'device_id': breaker.device_id,
            'switch': current.switch,
            'countdown_1_s': current.countdown_1_s,
            'child_lock': breaker.child_lock,
            'locked_out': breaker.locked_out,
            'timestamp': timestamp,
        })
