from django.urls import path

from . import views

urlpatterns = [
    path('', views.OrganizationListCreateView.as_view(), name='organization-list-create'),
    path('<int:pk>/', views.OrganizationDetailView.as_view(), name='organization-detail'),
    path('<int:pk>/approve/', views.OrganizationApproveView.as_view(), name='organization-approve'),
    path('<int:pk>/deny/', views.OrganizationDenyView.as_view(), name='organization-deny'),
]
