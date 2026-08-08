import json

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import ReadingSerializer


class ReadingIngestView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data if isinstance(request.data, list) else [request.data]

        # مؤقت: نطبع الدفعة كما وصلت من الراسبيري باي قبل التحقق منها.
        print(f'\n=== telemetry batch: {len(data)} reading(s) ===')
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print('=== end of batch ===\n', flush=True)

        serializer = ReadingSerializer(data=data, many=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {'received': len(serializer.validated_data)},
            status=status.HTTP_201_CREATED,
        )
