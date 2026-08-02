from django.urls import path

from .views import AckActionsView, RunCycleView, SettingsView, SimStateView

urlpatterns = [
    path('sim/run-cycle/', RunCycleView.as_view(), name='kbs-run-cycle'),
    path('sim/state/', SimStateView.as_view(), name='kbs-sim-state'),
    path('sim/ack/', AckActionsView.as_view(), name='kbs-sim-ack'),
    path('settings/', SettingsView.as_view(), name='kbs-settings'),
]
