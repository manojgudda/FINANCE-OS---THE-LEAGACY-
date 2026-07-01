from django.urls import path
from . import views

urlpatterns = [
    # Core pages
    path('', views.index, name='index'), 
    path('dashboard/', views.dashboard, name='dashboard'),
    
    # Auth
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'), 
    
    # Premium Features & Tools
    path('tools/', views.tools_view, name='tools'),
    path('taxes/', views.taxes_view, name='taxes'),
    path('budgets/', views.budgets_view, name='budgets'),
    path('tracker/', views.tracker_view, name='tracker'),
    path('investments/', views.investments_view, name='investments'),
    path('splits/', views.splits_view, name='splits'),
    path('scan-receipt/', views.scan_receipt, name='scan_receipt'),
    
    # Ledger & Export
    path('transactions/', views.transactions_view, name='transactions'),
    path('export/', views.export_csv, name='export'),
    
    # Impulse Check
    path('impulse-check/', views.impulse_check, name='impulse_check'),
    
    # Nav Preview API
    path('api/nav-preview/', views.nav_preview_api, name='nav_preview_api'),
]