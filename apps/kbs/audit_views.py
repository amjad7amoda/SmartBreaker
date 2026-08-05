import uuid

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.breakers.models import Breaker
from apps.organizations.models import Organization

from .authentication import DeviceAuthentication
from .models import (
    Alert, BreakerAction, EdgeDevice, KBSDecision, Tier1SafetyState,
)


ACTION_STATUSES = {choice[0] for choice in BreakerAction.STATUS_CHOICES}
EVENT_TYPES = {choice[0] for choice in KBSDecision.EVENT_TYPE_CHOICES}


class IsEdgeDevice(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, EdgeDevice) and request.auth.status == 'active'


def _iso_datetime(value, field):
    parsed = parse_datetime(str(value or ''))
    if parsed is None:
        raise ValueError(f'{field} must be an ISO-8601 datetime')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f'{field} must be a UUID')


def _resolved(status):
    return status in BreakerAction.RESOLVED_STATUSES


def _set_action_result(action, payload):
    status = payload.get('status')
    if status not in ACTION_STATUSES:
        raise ValueError(f'unknown action status: {status}')
    if action.status in BreakerAction.RESOLVED_STATUSES and status != action.status:
        raise ValueError(
            f'resolved action cannot change from {action.status} to {status}'
        )
    action.status = status
    if 'resulting_state' in payload:
        state = payload['resulting_state']
        if state is not None and type(state) is not bool:
            raise ValueError('resulting_state must be boolean or null')
        action.resulting_state = state
    action.failure_reason = str(payload.get('failure_reason') or '')[:500]
    if payload.get('executed_at'):
        action.executed_at = _iso_datetime(payload['executed_at'], 'executed_at')
    elif _resolved(status) and action.executed_at is None:
        action.executed_at = timezone.now()
    action.save()


class EdgeDecisionEventsView(APIView):
    authentication_classes = [DeviceAuthentication]
    permission_classes = [IsEdgeDevice]

    def post(self, request):
        events = request.data.get('events') if isinstance(request.data, dict) else None
        if not isinstance(events, list):
            return Response({'detail': 'events must be a JSON array'}, status=400)
        results = []
        for payload in events[:100]:
            try:
                with transaction.atomic():
                    results.append(self._ingest(request.auth, payload))
            except (ValueError, TypeError) as exc:
                results.append({
                    'event_id': payload.get('event_id') if isinstance(payload, dict) else None,
                    'status': 'rejected', 'detail': str(exc),
                })
        rejected = sum(item['status'] == 'rejected' for item in results)
        return Response({'results': results, 'accepted': len(results) - rejected, 'rejected': rejected})

    def _ingest(self, device, payload):
        if not isinstance(payload, dict):
            raise ValueError('each event must be a JSON object')
        organization = Organization.objects.select_for_update().get(
            pk=device.organization_id,
        )
        event_id = _uuid(payload.get('event_id'), 'event_id')
        event_type = payload.get('event_type', 'decision')
        if event_type not in EVENT_TYPES:
            raise ValueError(f'unknown event_type: {event_type}')
        occurred_at = _iso_datetime(payload.get('occurred_at'), 'occurred_at')
        trace_version = int(payload.get('trace_version', 1))
        trace = payload.get('trace', [])
        facts = payload.get('facts', {})
        actions = payload.get('actions', [])
        if not isinstance(trace, list) or not isinstance(facts, dict) or not isinstance(actions, list):
            raise ValueError('trace/actions must be arrays and facts must be an object')
        identity = {
            'organization': organization,
            'edge_device': device,
            'tier': 'tier1',
            'event_type': event_type,
            'engine': str(payload.get('engine') or 'edge.tier1_kbs.evaluate')[:150],
            'branch': str(payload.get('branch') or payload.get('situation') or '')[:100],
            'facts': facts,
            'trace_version': trace_version,
            'trace': trace,
            'occurred_at': occurred_at,
        }
        existing = KBSDecision.objects.filter(event_id=event_id).first()
        if existing is not None:
            immutable_match = (
                existing.organization_id == device.organization_id
                and existing.edge_device_id == device.device_id
                and existing.tier == 'tier1'
                and existing.event_type == identity['event_type']
                and existing.engine == identity['engine']
                and existing.branch == identity['branch']
                and existing.facts == facts
                and existing.trace_version == trace_version
                and existing.trace == trace
                and existing.occurred_at == occurred_at
            )
            if not immutable_match:
                return {'event_id': str(event_id), 'status': 'rejected',
                        'detail': 'immutable event conflicts with the stored record'}
            decision = existing
            outcome = 'duplicate'
        else:
            decision = KBSDecision.objects.create(event_id=event_id, **identity)
            outcome = 'created'
        for action_payload in actions:
            self._upsert_action(decision, device, action_payload)
        self._reconcile_confirmed_tier1_actions(decision)
        self._update_safety_state(decision, device, outcome)
        EdgeDevice.objects.filter(pk=device.pk).update(last_seen_at=timezone.now())
        return {'event_id': str(event_id), 'status': outcome, 'decision_id': decision.pk}

    @staticmethod
    def _upsert_action(decision, device, payload):
        if not isinstance(payload, dict):
            raise ValueError('each action must be a JSON object')
        action_id = _uuid(payload.get('action_id'), 'action_id')
        device_id = str(payload.get('device_id') or '')
        target = payload.get('action')
        if not device_id or target not in ('on', 'off'):
            raise ValueError('actions require device_id and action on/off')
        breaker = Breaker.objects.filter(
            organization=device.organization, device_id=device_id,
        ).first()
        existing = BreakerAction.objects.filter(action_id=action_id).first()
        if existing:
            if (existing.decision_id != decision.id or existing.device_id != device_id
                    or existing.action != target):
                raise ValueError('immutable action conflicts with the stored record')
            # A retried event must not downgrade a later execution result.
            # Rich status changes belong to the action-results endpoint.
            return existing
        action = BreakerAction.objects.create(
            action_id=action_id, decision=decision, breaker=breaker,
            device_id=device_id, action=target,
            countdown_s=max(int(payload.get('countdown_s') or 0), 0),
            reason=str(payload.get('reason') or '')[:255],
            status=payload.get('status', 'pending'),
        )
        if payload.get('status') and payload.get('status') != 'pending':
            _set_action_result(action, payload)
        return action

    @staticmethod
    def _reconcile_confirmed_tier1_actions(decision):
        """Resolve Tier-1 actions when a later edge snapshot confirms the target."""
        raw_breakers = decision.facts.get('breakers', [])
        if not isinstance(raw_breakers, list):
            return
        reported = {}
        for raw in raw_breakers:
            if not isinstance(raw, dict) or type(raw.get('switch')) is not bool:
                continue
            device_id = str(raw.get('device_id') or '')
            if device_id:
                reported[device_id] = raw['switch']
        if not reported:
            return
        unresolved = BreakerAction.objects.filter(
            decision__organization=decision.organization,
            decision__tier='tier1',
            status__in=('pending', 'scheduled'),
            device_id__in=reported,
        )
        for action in unresolved:
            desired_state = action.action == 'on'
            if reported[action.device_id] != desired_state:
                continue
            BreakerAction.objects.filter(pk=action.pk).update(
                status='applied',
                resulting_state=desired_state,
                executed=True,
                executed_at=decision.occurred_at,
                failure_reason='',
            )

    @staticmethod
    def _update_safety_state(decision, device, outcome):
        """Advance the organization safety hold from ordered Tier-1 events."""
        if decision.event_type == 'error':
            # Evaluator errors neither invent a danger nor clear an existing
            # hold. The last confirmed Tier-1 state remains authoritative.
            return
        state, _ = Tier1SafetyState.objects.select_for_update().get_or_create(
            organization=decision.organization,
        )
        if state.source_decision_id and state.source_decision_id != decision.id:
            current_source = state.source_decision
            current_order = (
                state.source_occurred_at,
                current_source.received_at,
                current_source.pk,
            )
            incoming_order = (
                decision.occurred_at,
                decision.received_at,
                decision.pk,
            )
            if incoming_order <= current_order:
                return

        state.edge_device = device
        state.source_decision = decision
        state.source_occurred_at = decision.occurred_at
        if decision.event_type == 'clear':
            state.active = False
            state.situation = ''
            state.commands = []
            state.cleared_at = decision.occurred_at
            state.save()
            return
        if decision.event_type != 'decision' or not decision.branch:
            return

        starting_episode = not state.active or state.situation != decision.branch
        if starting_episode:
            state.episode_id = uuid.uuid4()
            state.activated_at = decision.occurred_at
            state.cleared_at = None
        state.active = True
        state.situation = decision.branch
        incoming_commands = [
            {
                'device_id': action.device_id,
                'action': action.action,
                'countdown_s': action.countdown_s,
                'reason': action.reason,
            }
            for action in decision.actions.order_by('created_at', 'id')
        ]
        if starting_episode:
            state.commands = incoming_commands
        else:
            # A Tier-1 event with no commands means the current physical state
            # needs no additional command; it must not forget the episode's
            # desired safety targets. If a breaker is later turned back on,
            # Tier-2 can therefore reinforce the retained OFF target.
            merged = {
                command.get('device_id'): command
                for command in state.commands
                if isinstance(command, dict) and command.get('device_id')
            }
            for command in incoming_commands:
                merged[command['device_id']] = command
            state.commands = list(merged.values())
        state.save()

        if outcome != 'created':
            return
        reason = (
            f'Superseded by Tier-1 safety episode {state.episode_id}: '
            f'{state.situation}'
        )
        BreakerAction.objects.filter(
            decision__organization=decision.organization,
            decision__tier='tier2',
            status__in=('pending', 'scheduled'),
        ).update(
            status='superseded',
            executed=True,
            executed_at=timezone.now(),
            failure_reason=reason[:500],
        )


class EdgeActionResultsView(APIView):
    authentication_classes = [DeviceAuthentication]
    permission_classes = [IsEdgeDevice]

    def post(self, request):
        results = request.data.get('results') if isinstance(request.data, dict) else None
        if not isinstance(results, list):
            return Response({'detail': 'results must be a JSON array'}, status=400)
        outcomes = []
        for payload in results[:200]:
            try:
                action_id = _uuid(payload.get('action_id'), 'action_id')
                action = BreakerAction.objects.filter(
                    action_id=action_id,
                    decision__organization=request.auth.organization,
                    decision__edge_device=request.auth,
                ).first()
                if action is None:
                    raise ValueError('unknown action for this device')
                _set_action_result(action, payload)
                outcomes.append({'action_id': str(action_id), 'status': 'updated'})
            except (ValueError, TypeError) as exc:
                outcomes.append({
                    'action_id': payload.get('action_id') if isinstance(payload, dict) else None,
                    'status': 'rejected', 'detail': str(exc),
                })
        EdgeDevice.objects.filter(pk=request.auth.pk).update(last_seen_at=timezone.now())
        return Response({'results': outcomes})


def _action_data(action):
    return {
        'id': action.id, 'action_id': str(action.action_id),
        'device_id': action.device_id, 'action': action.action,
        'countdown_s': action.countdown_s, 'reason': action.reason,
        'status': action.status, 'executed': action.executed,
        'resulting_state': action.resulting_state,
        'executed_at': action.executed_at,
        'failure_reason': action.failure_reason,
    }


def _summary(decision):
    actions = list(decision.actions.all())
    return {
        'event_id': str(decision.event_id),
        'organization': {'id': decision.organization_id, 'name': decision.organization.name},
        'tier': decision.tier, 'event_type': decision.event_type,
        'engine': decision.engine, 'branch': decision.branch,
        'trace_version': decision.trace_version,
        'legacy': decision.is_legacy,
        'occurred_at': decision.occurred_at, 'received_at': decision.received_at,
        'action_count': len(actions),
        'action_statuses': [action.status for action in actions],
        'edge_device': str(decision.edge_device_id) if decision.edge_device_id else None,
    }


def _visible_decisions(user):
    queryset = KBSDecision.objects.select_related('organization', 'edge_device')
    if user.role in ('admin', 'technician'):
        return queryset
    return queryset.filter(organization__owner=user)


class DecisionLogPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


class DecisionLogListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = _visible_decisions(request.user).prefetch_related('actions')
        for field in ('tier', 'event_type', 'branch'):
            value = request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        organization = request.query_params.get('organization')
        if organization:
            queryset = queryset.filter(organization_id=organization)
        after = parse_datetime(request.query_params.get('after', ''))
        before = parse_datetime(request.query_params.get('before', ''))
        if after:
            queryset = queryset.filter(occurred_at__gte=after)
        if before:
            queryset = queryset.filter(occurred_at__lte=before)
        has_actions = request.query_params.get('has_actions')
        if has_actions in ('true', '1'):
            queryset = queryset.filter(actions__isnull=False).distinct()
        elif has_actions in ('false', '0'):
            queryset = queryset.filter(actions__isnull=True)
        paginator = DecisionLogPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response([_summary(item) for item in page])


class DecisionLogDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, event_id):
        decision = _visible_decisions(request.user).prefetch_related(
            'actions', 'alerts'
        ).filter(event_id=event_id).first()
        if decision is None:
            return Response({'detail': 'decision log not found'}, status=404)
        data = _summary(decision)
        data.update({
            'facts': decision.facts,
            'trace': decision.trace,
            'actions': [_action_data(action) for action in decision.actions.all()],
            'alerts': [{
                'kind': alert.kind, 'severity': alert.severity,
                'message': alert.message, 'suppressed': alert.suppressed,
                'suppression_reason': alert.suppression_reason,
                'created_at': alert.created_at,
            } for alert in decision.alerts.all()],
        })
        return Response(data)
