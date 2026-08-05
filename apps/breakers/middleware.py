import logging

from . import scheduling

logger = logging.getLogger(__name__)


class OrganizationPollingMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return response

        try:
            for organization_id in scheduling.organization_ids_for(user):
                scheduling.touch_organization(organization_id)
        except Exception:
            logger.exception('Could not refresh polling schedule for user %s', user.pk)

        return response
