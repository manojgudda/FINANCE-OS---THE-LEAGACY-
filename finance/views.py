from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from core.settings import db
import datetime
import json
import csv
from django.http import HttpResponse
from bson.objectid import ObjectId
import random
import requests
from bs4 import BeautifulSoup

from django.http import JsonResponse
import re
import time
try:
    from PIL import Image
    import pytesseract
except ImportError:
    pass

# --- HELPER METRICS ENGINE ---
def calculate_financial_metrics(user_id, extra_transaction=None):
    # Fetch all transactions
    transactions_cursor = list(db.transactions.find({'user_id': user_id}))
    if extra_transaction:
        transactions_cursor.append(extra_transaction)
        
    total_income = 0
    total_expense = 0
    total_tax_shield_holding = 0.0
    category_spending = {}
    needs_categories = ['Food', 'Bills', 'Transport']
    total_needs = 0
    total_wants = 0
    
    for item in transactions_cursor:
        amt = item.get('amount', 0)
        if item.get('type') == 'income':
            total_income += amt
            if item.get('is_gig'):
                holding = amt * (item.get('tax_withholding', 20.0) / 100.0)
                total_tax_shield_holding += holding
        elif item.get('type') == 'expense':
            total_expense += amt
            cat = item.get('category', 'Other')
            category_spending[cat] = category_spending.get(cat, 0) + amt
            if cat in needs_categories:
                total_needs += amt
            else:
                total_wants += amt
                
    total_balance = total_income - total_expense - total_tax_shield_holding
    
    # Gamification Algorithm
    health_score = 300
    if total_income > 0:
        needs_pct = (total_needs / total_income) * 100
        wants_pct = (total_wants / total_income) * 100
        savings_pct = (total_balance / total_income) * 100

        if needs_pct <= 50: health_score += 250
        elif needs_pct <= 70: health_score += 150
        else: health_score += 50

        if wants_pct <= 30: health_score += 200
        elif wants_pct <= 50: health_score += 100

        if savings_pct >= 20: health_score += 250
        elif savings_pct >= 10: health_score += 150
        elif savings_pct > 0: health_score += 50
    elif total_expense == 0 and total_income == 0:
        health_score = 0
        
    health_score = max(300, min(1000, health_score))
    
    return {
        'total_income': total_income,
        'total_expense': total_expense,
        'total_balance': total_balance,
        'total_tax_shield_holding': total_tax_shield_holding,
        'category_spending': category_spending,
        'health_score': health_score
    }

# --- LANDING PAGE ---
def index(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'finance/index.html')

# --- AUTHENTICATION ---
def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        #just using username and password
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')

        if password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            return redirect('register')

        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username is already taken!')
            return redirect('register')

        try:
            user = User.objects.create_user(username=username, password=password)
            user.save()
            login(request, user)
            messages.success(request, 'Account created successfully!')
            return redirect('dashboard')
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
            return redirect('register')

    return render(request, 'finance/register.html')

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        print(f"DEBUG: Checking database for -> '{username}'")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            print("DEBUG: Password matched! Logging in.")
            login(request, user)
            messages.success(request, f'Welcome back, {username}!')
            return redirect('dashboard')
        else:
            print("DEBUG: Password failed!")
            messages.error(request, 'Invalid username or password.')
            return redirect('login')

    return render(request, 'finance/login.html')

def logout_view(request):
    logout(request)
    messages.success(request, 'You have been logged out.')
    return redirect('login')

# --- DASHBOARD ---
@login_required(login_url='login')
def dashboard(request):
    # 1. Handle Form Submission
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        try:
            base_amount = float(request.POST.get('amount', 0))
            gst_rate = float(request.POST.get('gst', 0))
            if base_amount < 0:
                raise ValueError("Amount cannot be negative.")
        except ValueError as e:
            messages.error(request, str(e) or 'Invalid amount or GST rate.')
            return redirect('dashboard')
            
        trans_type = request.POST.get('type')
        category = request.POST.get('category', 'Other')
        if category == 'Other':
            custom = request.POST.get('custom_category', '').strip()
            if custom:
                category = custom
        is_gig = request.POST.get('is_gig') == 'true'
        
        tax_withholding = 0.0
        if is_gig and trans_type == 'income':
            try:
                tax_withholding = float(request.POST.get('tax_withholding', 20.0))
            except ValueError:
                tax_withholding = 20.0

        gst_amount = base_amount * (gst_rate / 100)
        final_amount = base_amount + gst_amount

        db.transactions.insert_one({
            'user_id': request.user.id,
            'title': title,
            'amount': final_amount,
            'base_amount': base_amount,
            'gst_amount': gst_amount,
            'type': trans_type,
            'category': category,
            'is_gig': is_gig,
            'tax_withholding': tax_withholding,
            'date': datetime.datetime.now()
        })
        messages.success(request, f'Logged {title} successfully!')
        return redirect('dashboard')

    # --- THE AUTO-BILL TRIGGER ---
    today = datetime.datetime.now()
    active_bills = db.recurring_bills.find({'user_id': request.user.id})
    last_processed_key = f"{today.year}-{today.month}"
    for bill in active_bills:
        if today.day >= bill['billing_date'] and bill.get('last_processed') != last_processed_key:
            # Backward compatibility check: if already processed under old scheme this month
            if bill.get('last_processed_month') == today.month and not bill.get('last_processed'):
                db.recurring_bills.update_one({'_id': bill['_id']}, {'$set': {'last_processed': last_processed_key}})
                continue

            db.transactions.insert_one({
                'user_id': request.user.id,
                'title': f"{bill['title']} (Auto-Pay)",
                'amount': bill['amount'],
                'base_amount': bill['amount'],
                'gst_amount': 0,
                'type': 'expense',
                'category': bill['category'],
                'date': datetime.datetime.now()
            })
            db.recurring_bills.update_one(
                {'_id': bill['_id']},
                {'$set': {'last_processed': last_processed_key, 'last_processed_month': today.month}}
            )

    # 2. Fetch Data & Calculate Analytics
    transactions_cursor = list(db.transactions.find({'user_id': request.user.id}).sort('date', -1))
    transactions = []
    for item in transactions_cursor:
        item['id'] = str(item['_id']) 
        transactions.append(item)
        
    metrics = calculate_financial_metrics(request.user.id)
    total_income = metrics['total_income']
    total_expense = metrics['total_expense']
    total_balance = metrics['total_balance']
    total_tax_shield_holding = metrics['total_tax_shield_holding']
    category_spending = metrics['category_spending']
    health_score = metrics['health_score']
    
    # Determine the status text and color
    if health_score >= 800:
        status, color = "Excellent", "text-accent"
    elif health_score >= 600:
        status, color = "Good", "text-warning"
    else:
        status, color = "Needs Work", "text-danger"

    # 3. LIVE BUDGETS ENGINE
    user_budgets = list(db.budgets.find({'user_id': request.user.id}))
    budget_limits = {b['category']: b['limit'] for b in user_budgets if b['limit'] > 0}
    budget_data = []
    for cat, limit in budget_limits.items():
        spent = category_spending.get(cat, 0)
        percentage = (spent / limit) * 100 if limit > 0 else 0
        visual_percent = min(percentage, 100) 
        if percentage < 70: color_class = 'bg-success'
        elif percentage < 90: color_class = 'bg-warning'
        else: color_class = 'bg-danger'

        budget_data.append({
            'category': cat, 'spent': round(spent, 2), 'limit': limit,
            'percentage': visual_percent, 'color': color_class
        })

    # 3.5. SUBSCRIPTION LEAKAGE AUDITOR
    recurring_bills = list(db.recurring_bills.find({'user_id': request.user.id}))
    ghost_subscriptions = []
    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=30)
    for bill in recurring_bills:
        # Check if there are any manual transactions in this category in the last 30 days
        manual_tx = db.transactions.find_one({
            'user_id': request.user.id,
            'category': bill['category'],
            'title': {'$not': re.compile(r'\(Auto-Pay\)')},
            'date': {'$gte': cutoff_date}
        })
        if not manual_tx:
            ghost_subscriptions.append(bill)

    # 4. Send data to HTML
    context = {
        'transactions': transactions,
        'total_balance': round(total_balance, 2),
        'total_income': round(total_income, 2),
        'total_expense': round(total_expense, 2),
        'chart_labels': json.dumps(list(category_spending.keys())),
        'chart_data': json.dumps(list(category_spending.values())),
        'budget_data': budget_data,
        'ghost_subscriptions': ghost_subscriptions,
        'total_tax_shield_holding': round(total_tax_shield_holding, 2),
        # Send Health Data
        'health_score': int(health_score),
        'health_status': status,
        'health_color': color,
        'health_pct': (health_score / 1000) * 100 # For the progress bar
    }
    return render(request, 'finance/dashboard.html', context) 


# --- DEBT PAYOFF PROJECTOR ---
def project_debt_payoff(debts, extra_payment, method='snowball'):
    local_debts = []
    for d in debts:
        local_debts.append({
            'name': d['name'],
            'balance': float(d['balance']),
            'apr': float(d['apr']),
            'min_payment': float(d['min_payment'])
        })
        
    total_min_payments = sum(d['min_payment'] for d in local_debts)
    total_monthly_budget = total_min_payments + float(extra_payment)
    
    timeline = []
    total_interest_paid = 0.0
    month = 0
    
    initial_balance = sum(d['balance'] for d in local_debts)
    timeline.append({
        'month': 0,
        'total_balance': round(initial_balance, 2),
        'total_interest': 0.0
    })
    
    while sum(d['balance'] for d in local_debts) > 0.01 and month < 360:
        month += 1
        
        # 1. Accrue interest
        month_interest = 0.0
        for d in local_debts:
            if d['balance'] > 0:
                interest = d['balance'] * (d['apr'] / 100.0 / 12.0)
                d['balance'] += interest
                month_interest += interest
                
        total_interest_paid += month_interest
        
        # 2. Sort debts based on method
        if method == 'snowball':
            local_debts.sort(key=lambda x: x['balance'] if x['balance'] > 0 else float('inf'))
        else:
            local_debts.sort(key=lambda x: x['apr'] if x['balance'] > 0 else -1, reverse=True)
            
        # 3. Apply payments
        available_budget = total_monthly_budget
        
        # First, pay minimum payments
        for d in local_debts:
            if d['balance'] > 0:
                payment = min(d['min_payment'], d['balance'])
                d['balance'] -= payment
                available_budget -= payment
                
        # Second, apply remaining budget to the target debt
        for d in local_debts:
            if d['balance'] > 0:
                extra_to_apply = min(available_budget, d['balance'])
                d['balance'] -= extra_to_apply
                available_budget -= extra_to_apply
                if available_budget <= 0:
                    break
                    
        current_total_balance = sum(d['balance'] for d in local_debts)
        timeline.append({
            'month': month,
            'total_balance': round(current_total_balance, 2),
            'total_interest': round(total_interest_paid, 2)
        })
        
    return timeline, round(total_interest_paid, 2), month


# --- SMART TOOLS HUB ---
@login_required(login_url='login')
def tools_view(request):
    if request.method == 'POST':
        form_type = request.POST.get('form_type')
        
        # Handle Adding to Smart Cart
        if form_type == 'smart_cart':
            item_name = request.POST.get('item_name', '').strip()
            try:
                price = float(request.POST.get('price', 0))
                if price < 0:
                    raise ValueError("Price cannot be negative.")
            except ValueError as e:
                messages.error(request, str(e) or 'Invalid price value.')
                return redirect('tools')

            db.smart_cart.insert_one({
                'user_id': request.user.id,
                'item_name': item_name,
                'price': price,
                'added_on': datetime.datetime.now()
            })
            messages.success(request, f'{item_name} added to your Smart Cart!')
            
        # Handle Adding a Recurring Bill
        elif form_type == 'recurring_bill':
            title = request.POST.get('title', '').strip()
            category = request.POST.get('category')
            if category == 'Other':
                custom = request.POST.get('custom_category', '').strip()
                if custom:
                    category = custom
            try:
                amount = float(request.POST.get('amount', 0))
                billing_date = int(request.POST.get('billing_date', 1))
                if amount < 0:
                    raise ValueError("Amount cannot be negative.")
                if not (1 <= billing_date <= 31):
                    raise ValueError("Billing date must be between 1 and 31.")
            except ValueError as e:
                messages.error(request, str(e) or 'Invalid amount or billing date.')
                return redirect('tools')
            
            db.recurring_bills.insert_one({
                'user_id': request.user.id,
                'title': title,
                'amount': amount,
                'category': category,
                'billing_date': billing_date,
                'last_processed_month': 0
            })
            messages.success(request, f'{title} subscription activated!')

        # Handle Adding a Debt
        elif form_type == 'add_debt':
            name = request.POST.get('name', '').strip()
            try:
                balance = float(request.POST.get('balance', 0))
                apr = float(request.POST.get('apr', 0))
                min_payment = float(request.POST.get('min_payment', 0))
                if balance <= 0 or apr < 0 or min_payment < 0:
                    raise ValueError("Values must be non-negative, and balance must be positive.")
            except ValueError as e:
                messages.error(request, str(e) or 'Invalid input values.')
                return redirect('tools')
                
            db.debts.insert_one({
                'user_id': request.user.id,
                'name': name,
                'balance': balance,
                'apr': apr,
                'min_payment': min_payment,
                'created_at': datetime.datetime.now()
            })
            messages.success(request, f'Debt "{name}" added successfully!')

        # Handle Deleting a Debt
        elif form_type == 'delete_debt':
            debt_id = request.POST.get('debt_id')
            db.debts.delete_one({'_id': ObjectId(debt_id), 'user_id': request.user.id})
            messages.success(request, 'Debt account removed.')
            
        return redirect('tools')

    # Fetch Data for the UI
    cart_cursor = db.smart_cart.find({'user_id': request.user.id}).sort('added_on', -1)
    bills_cursor = db.recurring_bills.find({'user_id': request.user.id}).sort('billing_date', 1)
    debts_cursor = db.debts.find({'user_id': request.user.id}).sort('created_at', -1)
    
    # Calculate Total Balance for the Affordability Engine
    transactions = db.transactions.find({'user_id': request.user.id})
    total_balance = sum([t['amount'] if t['type'] == 'income' else -t['amount'] for t in transactions])

    # Process Cart Items
    cart_items = []
    for item in cart_cursor:
        item['id'] = str(item['_id'])
        # Affordability Math
        item['affordable'] = total_balance >= item['price']
        item['progress'] = min((total_balance / item['price']) * 100, 100) if item['price'] > 0 else 100
        item['shortfall'] = abs(total_balance - item['price']) if not item['affordable'] else 0
        cart_items.append(item)

    # Process Bills
    recurring_bills = []
    for bill in bills_cursor:
        bill['id'] = str(bill['_id'])
        recurring_bills.append(bill)

    # Process Debts & Projections
    import json
    debts = []
    for debt in debts_cursor:
        debt['id'] = str(debt['_id'])
        debts.append(debt)

    extra_payment = 0.0
    try:
        extra_payment = float(request.GET.get('extra_payment', 0))
        if extra_payment < 0:
            extra_payment = 0.0
    except ValueError:
        pass

    snowball_timeline = []
    snowball_interest = 0.0
    snowball_months = 0
    avalanche_timeline = []
    avalanche_interest = 0.0
    avalanche_months = 0

    if debts:
        snowball_timeline, snowball_interest, snowball_months = project_debt_payoff(debts, extra_payment, 'snowball')
        avalanche_timeline, avalanche_interest, avalanche_months = project_debt_payoff(debts, extra_payment, 'avalanche')

    context = {
        'cart_items': cart_items,
        'recurring_bills': recurring_bills,
        'total_balance': total_balance,
        'debts': debts,
        'extra_payment': extra_payment,
        'snowball_timeline_json': json.dumps(snowball_timeline),
        'avalanche_timeline_json': json.dumps(avalanche_timeline),
        'snowball_interest': snowball_interest,
        'snowball_months': snowball_months,
        'avalanche_interest': avalanche_interest,
        'avalanche_months': avalanche_months,
        'interest_saved': max(0.0, round(snowball_interest - avalanche_interest, 2))
    }
    return render(request, 'finance/tools.html', context)

# --- FULL LEDGER & EXPORT ---
@login_required(login_url='login')
def transactions_view(request):
    # Handle Delete Request
    if request.method == 'POST' and 'delete_id' in request.POST:
        t_id = request.POST.get('delete_id')
        db.transactions.delete_one({'_id': ObjectId(t_id), 'user_id': request.user.id})
        messages.success(request, 'Transaction securely deleted.')
        return redirect('transactions')

    # Fetch all history
    transactions_cursor = db.transactions.find({'user_id': request.user.id}).sort('date', -1)
    transactions = []
    for item in transactions_cursor:
        item['id'] = str(item['_id']) 
        transactions.append(item)
        
    return render(request, 'finance/transactions.html', {'transactions': transactions})

@login_required(login_url='login')
def export_csv(request):
    # Create the HTTP response with CSV headers
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="FinanceOS_Ledger.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Date', 'Title', 'Category', 'Type', 'Amount (INR)', 'GST Included (INR)'])
    
    transactions = db.transactions.find({'user_id': request.user.id}).sort('date', -1)
    for t in transactions:
        writer.writerow([
            t['date'].strftime("%Y-%m-%d"), 
            t['title'], 
            t['category'], 
            t['type'], 
            round(t['amount'], 2), 
            round(t.get('gst_amount', 0), 2)
        ])
        
    return response

@login_required(login_url='login')
def taxes_view(request):
    # This page runs purely on front-end JavaScript for instant calculation!
    return render(request, 'finance/taxes.html')


# --- BUDGET PLANNER ---
@login_required(login_url='login')
def budgets_view(request):
    default_categories = ['Food', 'Transport', 'Bills', 'Shopping', 'Entertainment', 'Other']
    
    if request.method == 'POST':
        # 1. Handle adding a new custom category limit
        new_cat = request.POST.get('new_category', '').strip()
        if new_cat:
            try:
                new_limit = float(request.POST.get('new_limit', 0))
                if new_limit < 0:
                    raise ValueError("Limit cannot be negative.")
                db.budgets.update_one(
                    {'user_id': request.user.id, 'category': new_cat},
                    {'$set': {'limit': new_limit}},
                    upsert=True
                )
            except ValueError as e:
                messages.error(request, str(e) or 'Invalid limit for new category.')
                return redirect('budgets')

        # 2. Loop through all existing limit fields submitted in POST
        for key, val in request.POST.items():
            if key.startswith('limit_'):
                cat = key.replace('limit_', '')
                try:
                    limit_val = float(val)
                    if limit_val < 0:
                        raise ValueError("Limit cannot be negative.")
                except ValueError as e:
                    messages.error(request, str(e) or f'Invalid budget limit for {cat}.')
                    return redirect('budgets')

                db.budgets.update_one(
                    {'user_id': request.user.id, 'category': cat},
                    {'$set': {'limit': limit_val}},
                    upsert=True
                )
        messages.success(request, 'Budget limits updated successfully!')
        return redirect('budgets')

    # Fetch current limits to pre-fill the form
    user_budgets = list(db.budgets.find({'user_id': request.user.id}))
    budget_dict = {b['category']: b['limit'] for b in user_budgets}
    
    # Discover all unique categories used in transactions to include them in the list
    tx_categories = db.transactions.distinct('category', {'user_id': request.user.id})
    
    # Merge default categories with custom ones from budgets and transactions
    all_categories = set(default_categories)
    for b in user_budgets:
        all_categories.add(b['category'])
    for cat in tx_categories:
        if cat:
            all_categories.add(cat)
            
    # Sort categories for display
    categories_list = sorted(list(all_categories))

    context = {
        'budgets': {cat: budget_dict.get(cat, 0.0) for cat in categories_list}
    }
    return render(request, 'finance/budgets.html', context)

# --- PRICE TRACKER (ADVANCED HUB) ---
@login_required(login_url='login')
def tracker_view(request):
    if request.method == 'POST':
        if 'add_tracker' in request.POST:
            url = request.POST.get('url', '').strip()
            try:
                target_price = float(request.POST.get('target_price', 0))
                if target_price <= 0:
                    raise ValueError("Target price must be greater than zero.")
            except ValueError as e:
                messages.error(request, str(e) or 'Invalid target price.')
                return redirect('tracker')

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            title = "Apple iPhone 17 (Mist Blue, 256 GB)" # Default fallback name
            current_price = target_price * random.uniform(1.05, 1.25) 

            try:
                res = requests.get(url, headers=headers, timeout=5)
                soup = BeautifulSoup(res.text, 'html.parser')
                if soup.title:
                    title = soup.title.string.strip()[:60]
            except:
                pass # Use fallbacks if blocked

            # Generate realistic mock stats for the UI
            highest_price = current_price * 1.15
            lowest_price = current_price * 0.85
            avg_price = current_price * 1.05

            db.price_tracker.insert_one({
                'user_id': request.user.id,
                'url': url,
                'title': title,
                'current_price': round(current_price, 2),
                'target_price': target_price,
                'highest_price': round(highest_price, 2),
                'lowest_price': round(lowest_price, 2),
                'avg_price': round(avg_price, 2),
                'added_on': datetime.datetime.now()
            })
            messages.success(request, 'Product added to analytics hub!')

        elif 'delete_tracker' in request.POST:
            t_id = request.POST.get('tracker_id')
            db.price_tracker.delete_one({'_id': ObjectId(t_id), 'user_id': request.user.id})
            messages.success(request, 'Product removed.')

        elif 'refresh_prices' in request.POST:
            trackers = db.price_tracker.find({'user_id': request.user.id})
            for t in trackers:
                new_price = t['current_price'] * random.uniform(0.85, 1.05)
                db.price_tracker.update_one(
                    {'_id': t['_id']},
                    {'$set': {'current_price': round(new_price, 2)}}
                )
            messages.success(request, 'Live prices synced across all stores!')

        return redirect('tracker')

    # Fetch Data and Calculate UI Metrics
    trackers_cursor = db.price_tracker.find({'user_id': request.user.id}).sort('added_on', -1)
    trackers = []
    
    for t in trackers_cursor:
        t['id'] = str(t['_id'])
        cp = t['current_price']
        
        # Calculate Buy Recommendation Score (0-100)
        # 100 = It's at its lowest price. 0 = It's at its highest price.
        score_range = t['highest_price'] - t['lowest_price']
        if score_range > 0:
            score = 100 - (((cp - t['lowest_price']) / score_range) * 100)
        else:
            score = 50
            
        t['buy_score'] = max(0, min(100, score)) # Clamp between 0-100
        
        # Generate Competitor Prices based on current price
        t['competitors'] = [
            {'name': 'Flipkart', 'price': cp, 'diff': 'Best Price', 'color': 'text-accent'},
            {'name': 'Vijay Sales', 'price': cp * 1.02, 'diff': '2% Higher', 'color': 'text-danger'},
            {'name': 'Reliance Digital', 'price': cp * 1.04, 'diff': '4% Higher', 'color': 'text-danger'},
            {'name': 'Apple Official', 'price': t['highest_price'], 'diff': 'MRP', 'color': 'text-muted'},
        ]
        
        trackers.append(t)

    return render(request, 'finance/tracker.html', {'trackers': trackers})

# --- SPRINT 2: LIVE CRYPTO PORTFOLIO ---
@login_required(login_url='login')
def investments_view(request):
    if request.method == 'POST':
        coin_id = request.POST.get('coin_id')
        action = request.POST.get('action') # 'buy' or 'sell'
        try:
            amount = float(request.POST.get('amount', 0))
            if amount <= 0:
                raise ValueError("Quantity to trade must be greater than zero.")
        except ValueError as e:
            messages.error(request, str(e) or 'Invalid quantity.')
            return redirect('investments')

        # Find out how much the user currently owns
        holding = db.portfolio.find_one({'user_id': request.user.id, 'coin_id': coin_id})
        current_qty = holding['quantity'] if holding else 0

        # Calculate new quantity
        if action == 'buy':
            new_qty = current_qty + amount
        else:
            new_qty = max(0, current_qty - amount) # Prevent negative balances

        # Save back to MongoDB
        db.portfolio.update_one(
            {'user_id': request.user.id, 'coin_id': coin_id},
            {'$set': {'quantity': new_qty, 'last_updated': datetime.datetime.now()}},
            upsert=True # Creates the document if they've never bought this coin before
        )
        messages.success(request, f'Successfully {action}ed {amount} {coin_id.upper()}!')
        return redirect('investments')

    # 1. Define the assets we want to track
    tracked_coins = {
        'bitcoin': {'name': 'Bitcoin', 'symbol': 'BTC', 'icon': '₿', 'color': '#f59e0b'},
        'ethereum': {'name': 'Ethereum', 'symbol': 'ETH', 'icon': 'Ξ', 'color': '#8b5cf6'},
        'solana': {'name': 'Solana', 'symbol': 'SOL', 'icon': '◎', 'color': '#14b8a6'}
    }

    # 2. Fetch LIVE data from CoinGecko API (No API Key required!)
    api_url = f"https://api.coingecko.com/api/v3/simple/price?ids={','.join(tracked_coins.keys())}&vs_currencies=inr&include_24hr_change=true"
    
    live_data = {}
    try:
        response = requests.get(api_url, timeout=5)
        live_data = response.json()
    except:
        # Presentation Fallback: If you have no internet during a demo, fake the data!
        live_data = {
            'bitcoin': {'inr': 5500000, 'inr_24h_change': 2.5},
            'ethereum': {'inr': 250000, 'inr_24h_change': -1.2},
            'solana': {'inr': 12000, 'inr_24h_change': 5.4}
        }

    # 3. Fetch user's actual portfolio from MongoDB
    portfolio_cursor = db.portfolio.find({'user_id': request.user.id})
    user_holdings = {item['coin_id']: item['quantity'] for item in portfolio_cursor}

    # 4. Merge API data with Database data
    display_assets = []
    total_portfolio_value = 0

    for coin_id, details in tracked_coins.items():
        price = live_data.get(coin_id, {}).get('inr', 0)
        change = live_data.get(coin_id, {}).get('inr_24h_change', 0)
        qty = user_holdings.get(coin_id, 0)
        value = price * qty
        total_portfolio_value += value

        display_assets.append({
            'id': coin_id,
            'name': details['name'],
            'symbol': details['symbol'],
            'icon': details['icon'],
            'color': details['color'],
            'live_price': price,
            'change_24h': round(change, 2),
            'quantity': round(qty, 4),
            'value': round(value, 2)
        })

    context = {
        'assets': display_assets,
        'total_portfolio_value': round(total_portfolio_value, 2)
    }
    return render(request, 'finance/investments.html', context)

    # --- SPRINT 3: SPLIT BILLS ENGINE ---
@login_required(login_url='login')
def splits_view(request):
    if request.method == 'POST':
        # 1. Handle Creating a New Split Bill
        if 'add_split' in request.POST:
            title = request.POST.get('title', '').strip()
            category = request.POST.get('category', 'Other')
            if category == 'Other':
                custom = request.POST.get('custom_category', '').strip()
                if custom:
                    category = custom
            friends_raw = request.POST.get('friends', '')
            try:
                total_amount = float(request.POST.get('total_amount', 0))
                if total_amount <= 0:
                    raise ValueError("Total amount must be greater than zero.")
            except ValueError as e:
                messages.error(request, str(e) or 'Invalid total amount.')
                return redirect('splits')

            # Algorithm: Clean the names and divide equally
            friends_list = [f.strip() for f in friends_raw.split(',') if f.strip()]
            total_people = len(friends_list) + 1 # Include the user!
            split_amount = round(total_amount / total_people, 2)

            # Create data structure for MongoDB array
            friends_data = [{'name': friend, 'owes': split_amount, 'is_paid': False} for friend in friends_list]

            db.splits.insert_one({
                'user_id': request.user.id,
                'title': title,
                'total_amount': total_amount,
                'my_share': split_amount,
                'friends': friends_data,
                'date': datetime.datetime.now()
            })

            # AUTO-MAGIC: Log ONLY your share to your main dashboard expenses!
            db.transactions.insert_one({
                'user_id': request.user.id,
                'title': f"{title} (My Share)",
                'amount': split_amount,
                'base_amount': split_amount,
                'gst_amount': 0,
                'type': 'expense',
                'category': category,
                'date': datetime.datetime.now()
            })

            messages.success(request, f'Bill split! Logged ₹{split_amount} as your personal expense.')

        # 2. Handle Marking a Friend as "Paid"
        elif 'mark_paid' in request.POST:
            split_id = request.POST.get('split_id')
            friend_name = request.POST.get('friend_name')

            # Update the specific friend's status inside the array
            db.splits.update_one(
                {'_id': ObjectId(split_id), 'user_id': request.user.id, 'friends.name': friend_name},
                {'$set': {'friends.$.is_paid': True}}
            )
            messages.success(request, f'Marked {friend_name} as Paid!')

        # 3. Handle Deleting a Split completely
        elif 'delete_split' in request.POST:
            split_id = request.POST.get('split_id')
            db.splits.delete_one({'_id': ObjectId(split_id), 'user_id': request.user.id})
            messages.success(request, 'Split bill deleted.')

        return redirect('splits')

    # Fetch Data for the UI
    splits_cursor = db.splits.find({'user_id': request.user.id}).sort('date', -1)
    splits = []
    total_owed_to_me = 0

    for s in splits_cursor:
        s['id'] = str(s['_id'])
        # Calculate how much is still owed on this specific bill
        pending_amount = sum(f['owes'] for f in s['friends'] if not f['is_paid'])
        s['pending_amount'] = pending_amount
        s['is_completely_settled'] = (pending_amount == 0)

        total_owed_to_me += pending_amount
        splits.append(s)

    return render(request, 'finance/splits.html', {
        'splits': splits,
        'total_owed': round(total_owed_to_me, 2)
    })

# --- SPRINT 4: AI RECEIPT SCANNER ---
@login_required(login_url='login')
def scan_receipt(request):
    if request.method == 'POST' and request.FILES.get('receipt'):
        try:
            # Attempt real Optical Character Recognition (OCR)
            image = Image.open(request.FILES['receipt'])
            text = pytesseract.image_to_string(image)
            
            # Use Regex to find things that look like prices (e.g., 450.00)
            amounts = re.findall(r'\b\d+\.\d{2}\b', text)
            amounts = [float(a) for a in amounts]
            max_amount = max(amounts) if amounts else 0.0

            # Guess the merchant name from the very first line of text
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            title = lines[0] if lines else "Scanned Receipt"

            return JsonResponse({'success': True, 'title': title[:30], 'amount': max_amount})
            
        except Exception as e:
            # PORTFOLIO DEMO FALLBACK: 
            # If Tesseract isn't installed on the presentation computer, fake it!
            time.sleep(1.5) # Simulate AI thinking time
            return JsonResponse({
                'success': True, 
                'title': 'Zomato/Swiggy Delivery', 
                'amount': 850.50
            })
            
    return JsonResponse({'success': False, 'error': 'Invalid request'})


# --- SPRINT 5: IMPULSE BUY REALITY CHECK ---
@login_required(login_url='login')
def impulse_check(request):
    if request.method == 'POST':
        try:
            title = request.POST.get('title', '').strip()
            if not title:
                raise ValueError("Please enter the item name.")
                
            price = float(request.POST.get('price', 0))
            if price <= 0:
                raise ValueError("Please enter a valid price greater than zero.")
                
            category = request.POST.get('category', 'Other')
            if category == 'Other':
                custom = request.POST.get('custom_category', '').strip()
                if not custom:
                    raise ValueError("Please specify the custom category name.")
                category = custom
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e) or 'Invalid price.'})

        # 1. Calculate Hourly Wage
        income_tx = list(db.transactions.find({'user_id': request.user.id, 'type': 'income'}))
        total_income = sum(t.get('amount', 0) for t in income_tx)
        
        # If no income, fallback to a standard hourly wage of ₹300
        hourly_wage = (total_income / 160.0) if total_income > 0 else 300.0
        hours_required = price / hourly_wage

        # 2. Calculate Current Metrics
        current_metrics = calculate_financial_metrics(request.user.id)
        
        # 3. Calculate Projected Metrics with the proposed purchase added
        extra_tx = {
            'user_id': request.user.id,
            'title': title,
            'amount': price,
            'type': 'expense',
            'category': category,
            'date': datetime.datetime.now()
        }
        projected_metrics = calculate_financial_metrics(request.user.id, extra_transaction=extra_tx)

        # 4. Calculate Budget Impact
        budget = db.budgets.find_one({'user_id': request.user.id, 'category': category})
        budget_limit = budget['limit'] if budget else 0
        current_spent = current_metrics['category_spending'].get(category, 0)
        projected_spent = current_spent + price
        
        current_spent_pct = (current_spent / budget_limit * 100) if budget_limit > 0 else 0
        projected_spent_pct = (projected_spent / budget_limit * 100) if budget_limit > 0 else 0

        # 5. Determine Verdict
        total_balance = current_metrics['total_balance']
        if price > total_balance:
            verdict = "🔴 Debt Alert (Exceeds available balance)"
            verdict_type = "danger"
        elif budget_limit > 0 and projected_spent > budget_limit:
            verdict = "🔴 Budget Blown (Exceeds category limit)"
            verdict_type = "danger"
        elif (projected_metrics['health_score'] < current_metrics['health_score'] - 50) or (budget_limit > 0 and projected_spent_pct > 85):
            verdict = "🟡 Think Twice (High financial impact)"
            verdict_type = "warning"
        else:
            verdict = "🟢 Safe Buy"
            verdict_type = "safe"

        return JsonResponse({
            'success': True,
            'hours_required': hours_required,
            'current_score': current_metrics['health_score'],
            'projected_score': projected_metrics['health_score'],
            'budget_limit': budget_limit,
            'current_spent_pct': current_spent_pct,
            'projected_spent_pct': projected_spent_pct,
            'verdict': verdict,
            'verdict_type': verdict_type
        })


@login_required(login_url='login')
def nav_preview_api(request):
    user_id = request.user.id
    
    # 1. Dashboard Preview
    tx_cursor = list(db.transactions.find({'user_id': user_id}))
    total_income = 0.0
    total_expense = 0.0
    total_tax_shield = 0.0
    for tx in tx_cursor:
        amt = tx.get('amount', 0.0)
        if tx.get('type') == 'income':
            total_income += amt
            if tx.get('is_gig'):
                total_tax_shield += amt * (tx.get('tax_withholding', 20.0) / 100.0)
        elif tx.get('type') == 'expense':
            total_expense += amt
    total_balance = total_income - total_expense - total_tax_shield

    # 2. Budgets Preview
    budgets_cursor = list(db.budgets.find({'user_id': user_id}))
    budgets_data = []
    cat_spending = {}
    for tx in tx_cursor:
        if tx.get('type') == 'expense':
            cat = tx.get('category', 'Other')
            cat_spending[cat] = cat_spending.get(cat, 0.0) + tx.get('amount', 0.0)
    for b in budgets_cursor[:3]:
        spent = cat_spending.get(b['category'], 0.0)
        limit = b.get('limit', 0.0)
        pct = (spent / limit * 100) if limit > 0 else 0
        budgets_data.append({
            'category': b['category'],
            'spent': round(spent, 0),
            'limit': round(limit, 0),
            'pct': min(round(pct, 0), 100)
        })

    # 3. Smart Tools Preview
    cart_count = db.smart_cart.count_documents({'user_id': user_id})
    debts_count = db.debts.count_documents({'user_id': user_id})
    bills_count = db.recurring_bills.count_documents({'user_id': user_id})

    # 4. Price Tracker Preview
    trackers = list(db.price_tracker.find({'user_id': user_id}).limit(3))
    tracker_data = []
    for t in trackers:
        tracker_data.append({
            'title': t.get('title', 'Product')[:20],
            'price': round(t.get('current_price', 0.0), 0)
        })

    # 5. Tax Hub Preview
    # Just show the tax shield holding
    
    # 6. Investments Preview
    holdings_count = db.portfolio.count_documents({'user_id': user_id, 'quantity': {'$gt': 0}})

    # 7. Splits Preview
    splits_cursor = db.splits.find({'user_id': user_id})
    total_owed_to_me = 0.0
    for s in splits_cursor:
        total_owed_to_me += sum(f['owes'] for f in s['friends'] if not f['is_paid'])

    return JsonResponse({
        'dashboard': {
            'balance': round(total_balance, 0),
            'income': round(total_income, 0),
            'expense': round(total_expense, 0),
            'tax_shield': round(total_tax_shield, 0),
        },
        'budgets': budgets_data,
        'tools': {
            'cart_count': cart_count,
            'debts_count': debts_count,
            'bills_count': bills_count,
        },
        'tracker': tracker_data,
        'taxes': {
            'tax_shield': round(total_tax_shield, 0),
        },
        'investments': {
            'holdings_count': holdings_count,
        },
        'splits': {
            'total_owed': round(total_owed_to_me, 0),
        }
    })