"""Django adapter for the dependency-free Tier-2 KBS."""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.breakers.models import Breaker, BreakerReading
from apps.organizations.models import Organization
from apps.telemetry.models import Reading

from ..contracts import TIER2_ENGINE
from ..engine.derived import (
    hours_until, is_daytime, is_sudden_draw, is_sudden_drop,
    joule_deficit_J, mean, ramped_threshold,
)
from ..engine.facts import BreakerFacts, SystemFacts, facts_to_dict
from ..engine.fuzzy import (
    PROFILE_VERSION, ControllerSnapshot, advance_controller, evaluate_fuzzy,
)
from ..engine.rules import decide as decide_tier2, decide_fuzzy
from ..interlock import (
    Tier1SafetyCommand, Tier1SafetySnapshot, is_interlock_result,
    mirror_tier1_decision,
)
from ..models import (
    Alert, BreakerAction, KBSControllerState, KBSDecision, KBSSettings,
    Tier1SafetyState,
)
from ..weather import get_weather_context


TRIP_MEMORY_HOURS = 12
ALERT_COOLDOWN_MINUTES = 5
ACTION_DEDUPE_MINUTES = 10

logger = logging.getLogger(__name__)


def _result_payload(result, policy):
    if result is None:
        return {'policy': policy, 'branch': None, 'actions': [], 'alerts': []}
    return {
        'policy': policy,
        'branch': result.branch,
        'actions': [{
            'device_id': action.device_id,
            'action': action.action,
            'countdown_s': action.countdown_s,
            'reason': action.reason,
            'lockout': action.lockout,
        } for action in result.actions],
        'alerts': [{
            'kind': alert.kind,
            'severity': alert.severity,
            'message': alert.message,
        } for alert in result.alerts],
        'trace_version': result.trace_version,
        'trace': result.trace,
    }


def _authoritative_evaluation(reason):
    return {
        'profile_version': PROFILE_VERSION,
        'valid': False,
        'fallback_reason': reason,
        'inputs': {},
        'memberships': {},
        'fired_rules': [],
        'aggregated_strengths': {},
        'risk_score': None,
        'inferred_band': None,
        'risk_band': None,
        'controller': {
            'transition': 'not_evaluated',
            'advanced': False,
        },
    }


def _dispatch_real_actions(action_ids):
    """Queue committed real-site intents without coupling the pure engine to Celery."""
    from ..tasks import execute_kbs_action

    for action_id in action_ids:
        try:
            execute_kbs_action.delay(action_id)
        except Exception:
            logger.exception('Unable to queue KBS action %s', action_id)


class DjangoKBSAdapter:
    @staticmethod
    def decision_transaction():
        """Keep safety selection, fuzzy state, and persistence on one lock."""
        return transaction.atomic()

    def get_settings(self, organization):
        settings, _ = KBSSettings.objects.get_or_create(organization=organization)
        return settings

    def resolve_cycle_time(self, organization, settings, requested_now=None):
        if requested_now is not None:
            return requested_now
        if settings.data_source == 'simulator':
            return organization.readings.order_by('-timestamp').values_list(
                'timestamp', flat=True
            ).first()
        return timezone.now()

    def build_facts(self, organization, settings, cycle_time):
        if cycle_time is None:
            return None
        local_now = timezone.localtime(cycle_time)
        window_minutes = max(settings.deficit_window_minutes, settings.baseline_minutes)
        readings = list(Reading.objects.filter(
            organization=organization,
            timestamp__gte=cycle_time - timedelta(minutes=window_minutes),
            timestamp__lte=cycle_time,
        ).order_by('timestamp'))
        if not readings:
            return None
        latest = readings[-1]
        weather = get_weather_context(
            float(organization.latitude), float(organization.longitude), local_now,
        )
        day_start = weather.sunrise or settings.day_start
        day_end = weather.sunset or settings.day_end
        pv_now_W = self._pv_power_W(latest)
        pv_power_valid = (
            latest.pv_charging_power_W is not None
            or (
                latest.pv_input_voltage_V is not None
                and latest.pv_input_current_A is not None
            )
        )
        load_power_valid = latest.ac_output_active_power_W is not None
        load_now_W = latest.ac_output_active_power_W or 0.0
        baseline_cutoff = cycle_time - timedelta(minutes=settings.baseline_minutes)
        baseline_rows = [
            row for row in readings if row.timestamp >= baseline_cutoff and row is not latest
        ]
        pv_baseline_W = mean([self._pv_power_W(row) for row in baseline_rows])
        load_baseline_W = mean([row.ac_output_active_power_W for row in baseline_rows])
        deficit_cutoff = cycle_time - timedelta(minutes=settings.deficit_window_minutes)
        deficit_J = joule_deficit_J([
            (row.timestamp, row.ac_output_active_power_W, self._pv_power_W(row))
            for row in readings if row.timestamp >= deficit_cutoff
        ])

        active_event = organization.scheduled_events.filter(
            start_at__lte=cycle_time, end_at__gte=cycle_time,
        ).first()
        next_event = organization.scheduled_events.filter(
            start_at__gt=cycle_time,
            start_at__lte=cycle_time + timedelta(hours=settings.event_prep_hours),
        ).order_by('start_at').first()
        event_upcoming = active_event is not None or next_event is not None
        if active_event is not None:
            hours_until_event = 0.0
        elif next_event is not None:
            hours_until_event = (next_event.start_at - cycle_time).total_seconds() / 3600.0
        else:
            hours_until_event = None
        threshold_percent = ramped_threshold(
            settings.stability_threshold_percent,
            settings.event_stability_threshold_percent,
            hours_until_event, settings.event_prep_hours,
        )
        required_ids = (
            set(active_event.required_breakers.values_list('id', flat=True))
            if active_event is not None else set()
        )
        breaker_facts = tuple(
            self._breaker_facts(breaker, cycle_time, required_ids)
            for breaker in organization.breakers.select_related('status').all()
        )
        battery_percent = latest.battery_capacity_percent
        hours_to_morning = hours_until(local_now, day_start)
        mandatory_need_Wh = sum(
            breaker.expected_draw_W(settings.motor_peak_minutes) * hours_to_morning
            for breaker in breaker_facts
            if breaker.priority_type == 'mandatory' or breaker.event_required
        )
        if latest.battery_voltage_V is not None and latest.battery_discharge_current_A is not None:
            battery_draw_W = latest.battery_voltage_V * latest.battery_discharge_current_A
        else:
            battery_draw_W = max(load_now_W - pv_now_W, 0.0)
        battery_low_threshold_V = settings.battery_low_voltage_V + settings.battery_low_margin_V
        battery_low = (
            latest.battery_voltage_V is not None
            and latest.battery_voltage_V <= battery_low_threshold_V
            and (latest.battery_charge_current_A or 0.0) <= 0.5
        )
        grid = next((b for b in breaker_facts if b.priority_type == 'ac_grid'), None)
        grid_breaker_on = bool(grid and grid.switch)
        grid_energized = (latest.grid_voltage_V or 0.0) >= settings.grid_present_min_V
        sudden_draw = is_sudden_draw(load_now_W, load_baseline_W, settings.sudden_draw_W)

        return SystemFacts(
            organization_id=organization.id,
            now=cycle_time,
            local_time=local_now.time(),
            is_daytime=is_daytime(
                local_now.time(), day_start, day_end,
                pv_power_W=pv_now_W, pv_day_min_W=settings.pv_day_min_W,
            ),
            season=weather.season,
            weather_condition=weather.condition,
            power_saving=settings.power_saving,
            event_upcoming=event_upcoming,
            stability_threshold_percent=threshold_percent,
            battery_capacity_percent=battery_percent,
            battery_remaining_Wh=(battery_percent or 0.0) / 100.0 * settings.battery_capacity_Wh,
            battery_stable=battery_percent is not None and battery_percent >= threshold_percent,
            battery_voltage_V=latest.battery_voltage_V,
            battery_low=battery_low,
            battery_draw_W=battery_draw_W,
            battery_buffer_Wh=(
                settings.battery_shutdown_buffer_percent / 100.0 * settings.battery_capacity_Wh
            ),
            grid_breaker_on=grid_breaker_on,
            grid_energized=grid_energized,
            grid_failed=grid_breaker_on and not grid_energized,
            heatsink_temp_C=latest.heatsink_temp_C,
            heat_high=(
                latest.heatsink_temp_C is not None
                and latest.heatsink_temp_C >= settings.heatsink_temp_limit_C
            ),
            joule_deficit_J=deficit_J,
            deficit_high=deficit_J >= settings.joule_deficit_limit_J,
            overload=load_now_W >= settings.max_inverter_power_W,
            pv_power_W=pv_now_W,
            pv_baseline_W=pv_baseline_W,
            sudden_pv_drop=is_sudden_drop(
                pv_now_W, pv_baseline_W, settings.sudden_drop_fraction,
            ),
            load_power_W=load_now_W,
            load_baseline_W=load_baseline_W,
            sudden_draw=sudden_draw,
            sudden_draw_culprit_id=(
                self._sudden_draw_culprit_id(
                    organization, cycle_time, settings.baseline_minutes,
                ) if sudden_draw else None
            ),
            mean_load_on_W=sum(
                breaker.expected_draw_W(settings.motor_peak_minutes)
                for breaker in breaker_facts
                if breaker.switch and breaker.priority_type != 'ac_grid'
            ),
            headroom_W=max(settings.max_inverter_power_W - load_now_W, 0.0),
            max_inverter_power_W=settings.max_inverter_power_W,
            hours_to_morning=hours_to_morning,
            mandatory_need_Wh=mandatory_need_Wh,
            motor_peak_minutes=settings.motor_peak_minutes,
            breakers=breaker_facts,
            battery_low_threshold_V=battery_low_threshold_V,
            heatsink_temp_limit_C=settings.heatsink_temp_limit_C,
            joule_deficit_limit_J=settings.joule_deficit_limit_J,
            grid_present_min_V=settings.grid_present_min_V,
            sudden_drop_fraction=settings.sudden_drop_fraction,
            sudden_draw_W=settings.sudden_draw_W,
            pv_day_min_W=settings.pv_day_min_W,
            battery_capacity_Wh=settings.battery_capacity_Wh,
            night_reserve_percent=settings.night_reserve_percent,
            pv_power_valid=pv_power_valid,
            load_power_valid=load_power_valid,
        )

    @transaction.atomic
    def make_decision(self, organization, facts, default_decider):
        """Bypass normal Tier-2 rules while Tier-1 owns an active danger."""
        # When run_cycle supplies its outer transaction this lock remains held
        # through persist_result, so Tier-1 cannot activate between fuzzy-state
        # advancement and the authoritative decision row.
        Organization.objects.select_for_update().only('id').get(
            pk=organization.pk,
        )
        settings = self.get_settings(organization)
        safety = self._safety_snapshot(organization)
        if safety.active:
            result = mirror_tier1_decision(facts, safety)
            result.policy = settings.tier2_policy
            if settings.tier2_policy != 'crisp':
                result.fuzzy_evaluation = _authoritative_evaluation(
                    'tier1_interlock_authoritative',
                )
                result.counterfactual = _result_payload(result, 'crisp')
            return result
        return self._normal_policy_decision(
            organization, settings, facts, default_decider,
        )

    @transaction.atomic
    def _normal_policy_decision(
        self, organization, settings, facts, default_decider,
    ):
        crisp_result = default_decider(facts)
        crisp_result.policy = settings.tier2_policy
        if settings.tier2_policy == 'crisp':
            return crisp_result

        if crisp_result.branch in ('protect_inverter.overload', 'protect_battery'):
            crisp_result.fuzzy_evaluation = _authoritative_evaluation(
                'hard_protection_authoritative',
            )
            crisp_result.counterfactual = _result_payload(
                crisp_result,
                'fuzzy_active' if settings.tier2_policy == 'fuzzy_shadow' else 'crisp',
            )
            return crisp_result

        # Serializing on the organization also protects creation of its
        # one-to-one state during the first fuzzy cycle.
        Organization.objects.select_for_update().only('id').get(pk=organization.pk)
        state, _ = KBSControllerState.objects.select_for_update().get_or_create(
            organization=organization,
        )
        snapshot = ControllerSnapshot(
            current_band=state.current_band,
            candidate_band=state.candidate_band,
            consecutive_cycles=state.consecutive_cycles,
            last_risk_score=state.last_risk_score,
            last_evaluated_at=state.last_evaluated_at,
            profile_version=state.profile_version,
        )
        evaluation = evaluate_fuzzy(facts)
        # Hysteresis freshness is about controller executions. Simulator fact
        # timestamps advance with the accelerated physical clock and would
        # otherwise make every five-real-second cycle look stale.
        evaluated_at = timezone.now()
        next_snapshot, transition = advance_controller(
            snapshot, evaluation, evaluated_at, settings.cycle_seconds,
        )
        evaluation['controller'] = transition
        evaluation['risk_band'] = next_snapshot.current_band
        if next_snapshot != snapshot:
            state.current_band = next_snapshot.current_band
            state.candidate_band = next_snapshot.candidate_band
            state.consecutive_cycles = next_snapshot.consecutive_cycles
            state.last_risk_score = next_snapshot.last_risk_score
            state.last_evaluated_at = next_snapshot.last_evaluated_at
            state.profile_version = next_snapshot.profile_version
            state.save()

        fuzzy_result = (
            decide_fuzzy(
                facts, next_snapshot.current_band,
                evaluation=evaluation, crisp_result=crisp_result,
            )
            if evaluation['valid'] else None
        )
        if settings.tier2_policy == 'fuzzy_shadow':
            crisp_result.fuzzy_evaluation = evaluation
            crisp_result.counterfactual = _result_payload(
                fuzzy_result, 'fuzzy_active',
            )
            if fuzzy_result is None:
                crisp_result.counterfactual['fallback_reason'] = evaluation[
                    'fallback_reason'
                ]
            return crisp_result

        if fuzzy_result is None:
            # Active mode retains the configured policy in the audit row while
            # executing the complete crisp result as its safe fallback.
            crisp_result.fuzzy_evaluation = evaluation
            crisp_result.counterfactual = _result_payload(crisp_result, 'crisp')
            return crisp_result
        fuzzy_result.policy = settings.tier2_policy
        fuzzy_result.fuzzy_evaluation = evaluation
        fuzzy_result.counterfactual = _result_payload(crisp_result, 'crisp')
        return fuzzy_result

    @transaction.atomic
    def persist_result(self, organization, facts, result):
        # The organization row serializes Tier-1 ingestion with Tier-2
        # persistence. Re-reading the safety state here closes the race where a
        # danger starts or clears while Tier-2 is building facts.
        Organization.objects.select_for_update().only('id').get(pk=organization.pk)
        safety = self._safety_snapshot(organization, for_update=True)
        settings = self.get_settings(organization)
        if safety.active:
            result = mirror_tier1_decision(facts, safety)
            result.policy = settings.tier2_policy
            if settings.tier2_policy != 'crisp':
                result.fuzzy_evaluation = _authoritative_evaluation(
                    'tier1_interlock_authoritative',
                )
                result.counterfactual = _result_payload(result, 'crisp')
        elif is_interlock_result(result):
            result = self._normal_policy_decision(
                organization, settings, facts, decide_tier2,
            )

        trace = list(result.trace)
        stored_facts = facts_to_dict(facts)
        if result.fuzzy_evaluation:
            stored_facts['fuzzy_evaluation'] = result.fuzzy_evaluation
        decision = KBSDecision.objects.create(
            organization=organization,
            tier='tier2', event_type='decision',
            engine=TIER2_ENGINE,
            branch=result.branch,
            facts=stored_facts,
            trace_version=result.trace_version,
            trace=trace,
            policy=result.policy,
            counterfactual=result.counterfactual,
            occurred_at=facts.now,
        )
        dispatch_ids = []
        for intent in result.actions:
            duplicate = BreakerAction.objects.filter(
                breaker_id=intent.breaker_id,
                action=intent.action,
                status__in=('pending', 'scheduled'),
                created_at__gte=timezone.now() - timedelta(minutes=ACTION_DEDUPE_MINUTES),
            ).exists()
            status = 'suppressed_duplicate' if duplicate else 'pending'
            action = BreakerAction.objects.create(
                decision=decision,
                breaker_id=intent.breaker_id,
                device_id=intent.device_id,
                action=intent.action,
                countdown_s=intent.countdown_s,
                reason=intent.reason,
                status=status,
            )
            if duplicate:
                trace.append({
                    'code': 'tier2.persistence.action_duplicate',
                    'kind': 'execution', 'outcome': 'suppressed_duplicate',
                    'summary': f'Duplicate command for {intent.device_id} was retained as an audit outcome.',
                    'evidence': {'device_id': intent.device_id, 'action': intent.action},
                })
                continue
            dispatch_ids.append(action.id)
            if intent.lockout:
                Breaker.objects.filter(id=intent.breaker_id).update(
                    locked_out=True, lockout_reason=intent.reason, locked_at=facts.now,
                )

        for alert in result.alerts:
            duplicate = Alert.objects.filter(
                organization=organization, kind=alert.kind, suppressed=False,
                created_at__gte=timezone.now() - timedelta(minutes=ALERT_COOLDOWN_MINUTES),
            ).exists()
            Alert.objects.create(
                organization=organization, decision=decision,
                kind=alert.kind, severity=alert.severity, message=alert.message,
                suppressed=duplicate,
                suppression_reason='cooldown' if duplicate else '',
            )
            if duplicate:
                trace.append({
                    'code': 'tier2.persistence.alert_cooldown',
                    'kind': 'alert', 'outcome': 'suppressed_duplicate',
                    'summary': f'{alert.kind} alert retained but suppressed by cooldown.',
                    'evidence': {'kind': alert.kind, 'cooldown_minutes': ALERT_COOLDOWN_MINUTES},
                })
        if trace != decision.trace:
            decision.trace = trace
            decision.save(update_fields=['trace'])
        if settings.data_source == 'real' and dispatch_ids:
            transaction.on_commit(
                lambda ids=tuple(dispatch_ids): _dispatch_real_actions(ids),
            )
        return decision

    @staticmethod
    def _safety_snapshot(organization, for_update=False):
        queryset = Tier1SafetyState.objects
        if for_update:
            queryset = queryset.select_for_update()
            state = queryset.filter(organization=organization).first()
        else:
            state = queryset.filter(organization=organization).select_related(
                'source_decision',
            ).first()
        if state is None or not state.active:
            return Tier1SafetySnapshot(active=False)
        commands = []
        for raw in state.commands:
            if not isinstance(raw, dict):
                continue
            device_id = str(raw.get('device_id') or '')
            action = raw.get('action')
            if not device_id or action not in ('on', 'off'):
                continue
            commands.append(Tier1SafetyCommand(
                device_id=device_id,
                action=action,
                countdown_s=max(int(raw.get('countdown_s') or 0), 0),
                reason=str(raw.get('reason') or ''),
            ))
        return Tier1SafetySnapshot(
            active=True,
            situation=state.situation,
            episode_id=str(state.episode_id or ''),
            source_event_id=(
                str(state.source_decision.event_id)
                if state.source_decision_id else ''
            ),
            commands=tuple(commands),
        )

    @staticmethod
    def _pv_power_W(reading):
        if reading.pv_charging_power_W is not None:
            return reading.pv_charging_power_W
        if reading.pv_input_voltage_V is not None and reading.pv_input_current_A is not None:
            return reading.pv_input_voltage_V * reading.pv_input_current_A
        return 0.0

    @staticmethod
    def _breaker_facts(breaker, cycle_time, event_required_ids):
        status = getattr(breaker, 'status', None)
        minutes_since_on = None
        if status is not None and status.switch and status.last_switched_on_at is not None:
            minutes_since_on = (cycle_time - status.last_switched_on_at).total_seconds() / 60.0
        recently_tripped = (
            not breaker.locked_out and breaker.locked_at is not None
            and cycle_time - breaker.locked_at < timedelta(hours=TRIP_MEMORY_HOURS)
        )
        return BreakerFacts(
            id=breaker.id, device_id=breaker.device_id,
            priority_type=breaker.priority_type, priority_degree=breaker.priority_degree,
            load_type=breaker.load_type, peak_load_W=breaker.peak_load_W,
            mean_load_W=breaker.mean_load_W, cycle_start=breaker.cycle_start,
            cycle_end=breaker.cycle_end, switch=status.switch if status else False,
            online=status.online if status else False, fault=status.fault if status else '',
            locked_out=breaker.locked_out, recently_tripped=recently_tripped,
            event_required=breaker.id in event_required_ids,
            cur_power_W=(
                status.cur_power_mW / 1000.0
                if status and status.cur_power_mW is not None else None
            ),
            minutes_since_on=minutes_since_on,
        )

    @staticmethod
    def _sudden_draw_culprit_id(organization, cycle_time, baseline_minutes):
        rows = BreakerReading.objects.filter(
            breaker__organization=organization,
            timestamp__gte=cycle_time - timedelta(minutes=baseline_minutes),
            timestamp__lte=cycle_time,
        ).order_by('timestamp').values_list('breaker_id', 'cur_power_mW')
        by_breaker = {}
        for breaker_id, power_mW in rows:
            by_breaker.setdefault(breaker_id, []).append(power_mW)
        culprit_id = None
        biggest_jump_W = 0.0
        for breaker_id, powers in by_breaker.items():
            if len(powers) < 2:
                continue
            earlier_mW = mean(powers[:-1])
            latest_mW = powers[-1]
            if earlier_mW is None or latest_mW is None:
                continue
            jump_W = (latest_mW - earlier_mW) / 1000.0
            if jump_W > biggest_jump_W:
                biggest_jump_W = jump_W
                culprit_id = breaker_id
        return culprit_id
