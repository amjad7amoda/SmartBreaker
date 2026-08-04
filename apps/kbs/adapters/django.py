"""Django adapter for the dependency-free Tier-2 KBS."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.breakers.models import Breaker, BreakerReading
from apps.telemetry.models import Reading

from ..contracts import TIER2_ENGINE
from ..engine.derived import (
    hours_until, is_daytime, is_sudden_draw, is_sudden_drop,
    joule_deficit_J, mean, ramped_threshold,
)
from ..engine.facts import BreakerFacts, SystemFacts, facts_to_dict
from ..models import Alert, BreakerAction, KBSDecision, KBSSettings
from ..weather import get_weather_context


TRIP_MEMORY_HOURS = 12
ALERT_COOLDOWN_MINUTES = 5
ACTION_DEDUPE_MINUTES = 10


class DjangoKBSAdapter:
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
        )

    @transaction.atomic
    def persist_result(self, organization, facts, result):
        trace = list(result.trace)
        decision = KBSDecision.objects.create(
            organization=organization,
            tier='tier2', event_type='decision',
            engine=TIER2_ENGINE,
            branch=result.branch,
            facts=facts_to_dict(facts),
            trace_version=result.trace_version,
            trace=trace,
            occurred_at=facts.now,
        )
        for intent in result.actions:
            duplicate = BreakerAction.objects.filter(
                breaker_id=intent.breaker_id,
                action=intent.action,
                status__in=('pending', 'scheduled'),
                created_at__gte=timezone.now() - timedelta(minutes=ACTION_DEDUPE_MINUTES),
            ).exists()
            status = 'suppressed_duplicate' if duplicate else 'pending'
            BreakerAction.objects.create(
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
        return decision

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
