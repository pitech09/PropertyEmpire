from django.urls import path
from tennants.views.tenants import initiate_payment, report_issue, tenant_dashboard,  health
from tennants.views.web import (dashboard, BuildingListViewWeb, BuildingDetailViewWeb,
                    landlord_financial_dashboard, owner_dashboard,
                    reports, export_payments_csv, export_tenants_csv,
                    BuildingCreateViewWeb, BuildingUpdateViewWeb, BuildingDeleteViewWeb,
                    HouseListViewWeb, HouseDetailViewWeb,
                    HouseCreateViewWeb, HouseUpdateViewWeb, HouseDeleteViewWeb,
                    house_images, house_image_delete,
                    TenantListViewWeb, TenantDetailViewWeb,
                    TenantCreateViewWeb, TenantUpdateViewWeb, TenantDeleteViewWeb,
                    PaymentListViewWeb, PaymentDetailViewWeb,
                    PaymentCreateViewWeb, PaymentUpdateViewWeb,
                    RentChargeListViewWeb, RentChargeDetailViewWeb,
                    RentChargeCreateViewWeb, RentChargeUpdateViewWeb,
                    bulk_create_rent_charges, send_rent_reminders,
                    PaymentRequestListViewWeb, PaymentRequestDetailViewWeb,
                    PaymentRequestCreateViewWeb, PaymentRequestUpdateViewWeb,
                    approve_payment_request, reject_payment_request,
                    IssueListViewWeb, IssueDetailViewWeb, IssueUpdateViewWeb,
                    expense_list, expense_create, expense_edit, expense_delete,
                    issue_bids, accept_bid, reject_bid)

from tennants.views.api import (TenantListView, TenantDetailView,
                    HouseListView, HouseDetailView,
                    FlatBuildingListView, FlatBuildingDetailView,PaymentListView)
from tennants.views.auth import AdminLogoutView, login_view, RegisterUserView, AdminLogoutView
from tennants.views.api import user_login

# urlpatterns = [
#     path('tennants/', TenantListView.as_view(), name = 'tennant-list'),
#     path('tennants/<int:pk>/', TenantDetailView.as_view(),name ='tennants-list'),

#     path("houses/", HouseListView.as_view(), name="house-list"),
#     path('houses/<int:pk>/', HouseDetailView.as_view(), name = 'house-detail'),
#     path('admin/logout/', AdminLogoutView.as_view(), name='admin_logout'),
#     path('user/login/', user_login, name='admin_login'),
    
#     path("flats/", FlatBuildingListView.as_view(), name="flat-list"),
#     path('flats/new/api', FlatBuildingDetailView.as_view(), name='flat-update'),
#     path('flats/api/<int:pk>/', FlatBuildingDetailView.as_view(), name='flat-detail'),
#     path('payments/api', PaymentListView.as_view(), name='payment-list'),
#     path('register/user/api', RegisterUserView.as_view(), name='register-user'),
# ]
urlpatterns = [
    # ========================================
    # WEB INTERFACE (Template-based)
    # ========================================
    path('', dashboard, name='dashboard'),
    path('health/', health, name='health_check'),
    path('financials/', landlord_financial_dashboard, name='landlord_financial_dashboard'),
    path('owner/dashboard/', owner_dashboard, name='owner_dashboard'),

    # Reports
    path('reports/', reports, name='reports'),
    path('reports/export/payments.csv', export_payments_csv, name='export_payments_csv'),
    path('reports/export/tenants.csv', export_tenants_csv, name='export_tenants_csv'),

    # Buildings

    path('buildings/', BuildingListViewWeb.as_view(), name='building_list'),
    path('buildings/add/', BuildingCreateViewWeb.as_view(), name='building_add'),
    path('buildings/<int:pk>/', BuildingDetailViewWeb.as_view(), name='building_detail'),
    path('buildings/<int:pk>/edit/', BuildingUpdateViewWeb.as_view(), name='building_edit'),
    path('buildings/<int:pk>/delete/', BuildingDeleteViewWeb.as_view(), name='building_delete'),
    
    # Houses
    path('houses/', HouseListViewWeb.as_view(), name='house_list'),
    path('houses/add/', HouseCreateViewWeb.as_view(), name='house_add'),
    path('houses/<int:pk>/', HouseDetailViewWeb.as_view(), name='house_detail'),
    path('houses/<int:pk>/edit/', HouseUpdateViewWeb.as_view(), name='house_edit'),
    path('houses/<int:pk>/delete/', HouseDeleteViewWeb.as_view(), name='house_delete'),
    path('houses/<int:house_pk>/images/', house_images, name='house_images'),
    path('houses/<int:house_pk>/images/<int:image_pk>/delete/', house_image_delete, name='house_image_delete'),

    # Tenants
    path('tenants/', TenantListViewWeb.as_view(), name='tenant_list'),
    path('tenants/add/', TenantCreateViewWeb.as_view(), name='tenant_add'),
    path('tenants/<int:pk>/', TenantDetailViewWeb.as_view(), name='tenant_detail'),
    path('tenants/<int:pk>/edit/', TenantUpdateViewWeb.as_view(), name='tenant_edit'),
    path('tenants/<int:pk>/delete/', TenantDeleteViewWeb.as_view(), name='tenant_delete'), 

  

    # Payments
    path('payments/', PaymentListViewWeb.as_view(), name='payment_list'),
    path('payments/add/', PaymentCreateViewWeb.as_view(), name='payment_add'),
    path('payments/<int:pk>/', PaymentDetailViewWeb.as_view(), name='payment_detail'),
    path('payments/<int:pk>/edit/', PaymentUpdateViewWeb.as_view(), name='payment_edit'),

    #paymentrequest
    path('payment-requests/', PaymentRequestListViewWeb.as_view(), name='payment_request_list'),
    path('payment-requests/add/', PaymentRequestCreateViewWeb.as_view(), name='payment_request_add'),
    path('payment-requests/<int:pk>/', PaymentRequestDetailViewWeb.as_view(), name='payment_request_detail'),
    path('payment-requests/<int:pk>/edit/', PaymentRequestUpdateViewWeb.as_view(), name='payment_request_edit'),
    path('payment-requests/<int:pk>/approve/', approve_payment_request, name='payment_request_approve'),
    path('payment-requests/<int:pk>/reject/', reject_payment_request, name='payment_request_reject'),

    # Rent Charges
    path('rent-charges/', RentChargeListViewWeb.as_view(), name='rent_charge_list'),
    path('rent-charges/add/', RentChargeCreateViewWeb.as_view(), name='rent_charge_add'),
    path('rent-charges/<int:pk>/', RentChargeDetailViewWeb.as_view(), name='rent_charge_detail'),
    path('rent-charges/<int:pk>/edit/', RentChargeUpdateViewWeb.as_view(), name='rent_charge_edit'),
    path('rent-charges/bulk-create/', bulk_create_rent_charges, name='rent_charge_bulk_create'),
    

    # notifications
    path('send-rent-reminders/', send_rent_reminders, name='send_rent_reminders'),

    # Issues
    path('issues/', IssueListViewWeb.as_view(), name='issue_list'),
    path('issues/<int:pk>/', IssueDetailViewWeb.as_view(), name='issue_detail'),
    path('issues/<int:pk>/edit/', IssueUpdateViewWeb.as_view(), name='issue_edit'),

    # Expenses
    path('expenses/', expense_list, name='expense_list'),
    path('expenses/add/', expense_create, name='expense_add'),
    path('expenses/<int:pk>/edit/', expense_edit, name='expense_edit'),
    path('expenses/<int:pk>/delete/', expense_delete, name='expense_delete'),

    # Bid reviews (landlord reviews worker bids on an issue)
    path('issues/<int:issue_id>/bids/', issue_bids, name='issue_bids'),
    path('bids/<int:bid_id>/accept/', accept_bid, name='accept_bid'),
    path('bids/<int:bid_id>/reject/', reject_bid, name='reject_bid'),
]


urlpatterns += [        
    path("tenant/dashboard/", tenant_dashboard, name="tenant_dashboard"),
    path("tenant/report/", report_issue, name="report_issue"),
    path("tenant/payment/initiate/<int:charge_id>/", initiate_payment, name="initiate_payment"),
]

# Worker portal URLs
from tennants.views.workers import (
    worker_register, worker_dashboard, worker_place_bid,
    worker_my_bids, worker_withdraw_bid, worker_profile
)

urlpatterns += [
    path("workers/register/", worker_register, name="worker_register"),
    path("workers/dashboard/", worker_dashboard, name="worker_dashboard"),
    path("workers/issues/<int:issue_id>/bid/", worker_place_bid, name="worker_place_bid"),
    path("workers/bids/", worker_my_bids, name="worker_my_bids"),
    path("workers/bids/<int:bid_id>/withdraw/", worker_withdraw_bid, name="worker_withdraw_bid"),
    path("workers/profile/", worker_profile, name="worker_profile"),
]
