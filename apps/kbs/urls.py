from django.urls import path

from .audit_views import (
    DecisionLogDetailView, DecisionLogListView,
    EdgeActionResultsView, EdgeDecisionEventsView,
)
from .views import (
    AckActionsView, BreakerOverrideView, ClimateView, RunCycleView,
    SettingsView, SimResetView, SimStateView,
)

urlpatterns = [
    path('edge/decision-events/', EdgeDecisionEventsView.as_view(), name='kbs-edge-decision-events'),
    path('edge/action-results/', EdgeActionResultsView.as_view(), name='kbs-edge-action-results'),
    path('decision-logs/', DecisionLogListView.as_view(), name='kbs-decision-logs'),
    path('decision-logs/<uuid:event_id>/', DecisionLogDetailView.as_view(), name='kbs-decision-log-detail'),
    path('sim/climate/', ClimateView.as_view(), name='kbs-sim-climate'),
    path('sim/run-cycle/', RunCycleView.as_view(), name='kbs-run-cycle'),
    path('sim/state/', SimStateView.as_view(), name='kbs-sim-state'),
    path('sim/ack/', AckActionsView.as_view(), name='kbs-sim-ack'),
    path('sim/reset/', SimResetView.as_view(), name='kbs-sim-reset'),
    path('sim/breaker-override/', BreakerOverrideView.as_view(), name='kbs-sim-breaker-override'),
    path('settings/', SettingsView.as_view(), name='kbs-settings'),
]
