from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError


def parse_moment(raw, param):
    moment = parse_datetime(raw)
    if moment is None:
        raise ValidationError({
            param: f'Expected an ISO-8601 datetime, got "{raw}".',
        })
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment


def filter_by_time_window(queryset, params, field='timestamp'):
    for param, lookup in (('since', 'gte'), ('until', 'lte')):
        raw = params.get(param)
        if raw:
            queryset = queryset.filter(
                **{f'{field}__{lookup}': parse_moment(raw, param)},
            )
    return queryset
