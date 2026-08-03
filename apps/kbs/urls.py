from django.urls import path

from .views import (
    AckActionsView, BreakerOverrideView, ClimateView, RunCycleView,
    SettingsView, SimResetView, SimStateView,
)

urlpatterns = [
    path('sim/climate/', ClimateView.as_view(), name='kbs-sim-climate'),
    path('sim/run-cycle/', RunCycleView.as_view(), name='kbs-run-cycle'),
    path('sim/state/', SimStateView.as_view(), name='kbs-sim-state'),
    path('sim/ack/', AckActionsView.as_view(), name='kbs-sim-ack'),
    path('sim/reset/', SimResetView.as_view(), name='kbs-sim-reset'),
    path('sim/breaker-override/', BreakerOverrideView.as_view(), name='kbs-sim-breaker-override'),
    path('settings/', SettingsView.as_view(), name='kbs-settings'),
]
