import json

from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.query_params import filter_by_time_window

from .models import Reading
from .serializers import ReadingOutputSerializer, ReadingSerializer
from .services import dispatch_kbs_cycles


class ReadingPagination(PageNumberPagination):
    """A site pushes a sample every few seconds, so listing is always paged."""

    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 1000


def scoped_readings(user):
    queryset = Reading.objects.select_related('organization')
    if user.role in ('technician', 'admin'):
        return queryset
    return queryset.filter(organization__owner=user)


def filter_readings(queryset, params):
    organization = params.get('organization')
    if organization:
        queryset = queryset.filter(organization_id=organization)
    return filter_by_time_window(queryset, params)


class ReadingIngestView(generics.ListAPIView):
    """POST is the edge agent's unauthenticated ingest; GET is the read side.

    The Pi has no credentials to present, so ingest stays open while listing
    requires a login and is scoped to the caller's organizations.
    """

    serializer_class = ReadingOutputSerializer
    pagination_class = ReadingPagination

    def get_permissions(self):
        if self.request.method == 'POST':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        return filter_readings(
            scoped_readings(self.request.user), self.request.query_params,
        )

    def post(self, request):
        data = request.data if isinstance(request.data, list) else [request.data]

        # مؤقت: نطبع الدفعة كما وصلت من الراسبيري باي قبل التحقق منها.
        print(f'\n=== telemetry batch: {len(data)} reading(s) ===')
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print('=== end of batch ===\n', flush=True)

        serializer = ReadingSerializer(data=data, many=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        dispatch_kbs_cycles(
            item['organization'].id for item in serializer.validated_data
        )
        return Response(
            {'received': len(serializer.validated_data)},
            status=status.HTTP_201_CREATED,
        )


class ReadingLatestView(APIView):
    """The newest reading for each site in scope — one row per organization."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = filter_readings(
            scoped_readings(request.user), request.query_params,
        )
        latest = queryset.order_by('organization_id', '-timestamp').distinct(
            'organization_id',
        )
        return Response(ReadingOutputSerializer(latest, many=True).data)
