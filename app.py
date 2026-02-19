from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime
from googletrans import Translator
from twilio.rest import Client
from werkzeug.utils import secure_filename
import pandas as pd
import os
import random

# IMPORT MONGOENGINE
from mongoengine import connect, Document, StringField, FloatField, IntField, ReferenceField, DateTimeField, Q

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secretkey123'

# --- CONNECT TO MONGODB ---

connect('farm_db', host='localhost:27017')

# --- IMAGE UPLOAD CONFIG ---
UPLOAD_FOLDER = 'static/product_images'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- TWILIO CONFIG ---
app.config['TWILIO_ACCOUNT_SID'] = 'ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' 
app.config['TWILIO_AUTH_TOKEN'] = 'your_auth_token_here'
app.config['TWILIO_PHONE_NUMBER'] = '+15550000000'

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
translator = Translator()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- MONGODB MODELS ---
class User(Document, UserMixin):
    username = StringField(required=True, unique=True)
    phone = StringField(required=True, unique=True)
    password = StringField(required=True)
    role = StringField(required=True)

class Product(Document):
    name = StringField(required=True)
    price = FloatField(required=True)
    quantity = IntField(required=True)
    category = StringField()
    location = StringField(default='Not specified')
    image = StringField(default='default.jpg')
    farmer = ReferenceField(User)

class Order(Document):
    product = ReferenceField(Product)
    consumer = ReferenceField(User)
    farmer = ReferenceField(User)
    quantity = IntField(default=1)
    status = StringField(default='Pending')
    date = DateTimeField(default=datetime.utcnow)

class ActivityLog(Document):
    user = ReferenceField(User)
    action = StringField(required=True)
    timestamp = DateTimeField(default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.objects(id=user_id).first()

# --- FORECASTING ---
class DemandForecaster:
    def __init__(self, data_file='historical_data.csv'):
        if os.path.exists(data_file):
            self.df = pd.read_csv(data_file)
            try: self.df['date'] = pd.to_datetime(self.df['date'])
            except: pass 
        else:
            self.df = pd.DataFrame()

    def analyze(self, product_name):
        if self.df.empty: return "No Historical Data", 0, 0
        product_data = self.df[self.df['product_name'].str.lower() == product_name.lower()]
        if product_data.empty: return "New Product", 0, 0
        
        current_month = datetime.now().month
        if 'date' in product_data.columns:
            seasonal_data = product_data[product_data['date'].dt.month == current_month]
        else: seasonal_data = pd.DataFrame()
        
        if not seasonal_data.empty:
            avg_price = seasonal_data['price_per_kg'].mean()
            avg_qty = seasonal_data['quantity_sold'].mean()
            overall_avg_qty = product_data['quantity_sold'].mean()
            if avg_qty > overall_avg_qty * 1.1: trend = "High Demand (Seasonal Peak) 📈"
            elif avg_qty < overall_avg_qty * 0.9: trend = "Low Demand (Off-Season) 📉"
            else: trend = "Stable Demand ⚖️"
        else:
            avg_price = product_data['price_per_kg'].mean()
            avg_qty = product_data['quantity_sold'].mean()
            trend = "Stable (No seasonal data)"
        return trend, round(avg_price, 2), int(avg_qty)

forecaster = DemandForecaster()

# --- TRANSLATION ---
@app.context_processor
def inject_translator():
    def translate_text(text):
        try:
            dest_lang = session.get('lang', 'en')
            if dest_lang == 'en': return text
            return translator.translate(text, dest=dest_lang).text
        except: return text
    return dict(translate=translate_text)

@app.route('/set_language/<lang_code>')
def set_language(lang_code):
    session['lang'] = lang_code
    return redirect(request.referrer or url_for('index'))

# --- ROUTES ---
@app.route('/')
def index():
    showcase_products = list(Product.objects(quantity__gt=0).limit(6))
    return render_template('index.html', products=showcase_products)

@app.route('/about')
def about(): return render_template('about.html')

@app.route('/privacy')
def privacy(): return render_template('privacy.html')

@app.route('/customer_service')
def customer_service(): return render_template('customer_service.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        phone = request.form.get('phone')
        password = request.form.get('password')
        role = request.form.get('role')
        
        if User.objects(Q(username=username) | Q(phone=phone)).first():
            flash('Username or Phone number already exists.')
            return redirect(url_for('register'))
            
        new_user = User(username=username, phone=phone, password=password, role=role)
        new_user.save() 
        
        ActivityLog(user=new_user, action=f'Registered as {role}').save()
        
        login_user(new_user)
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.objects(username=username).first()
        if user and user.password == password:
            login_user(user)
            
            ActivityLog(user=user, action='Logged in via password').save()
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    return render_template('login.html')

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        phone = request.form.get('phone')
        user = User.objects(phone=phone).first()
        if user:
            otp = random.randint(1000, 9999)
            session['otp'] = otp
            session['reset_user_id'] = str(user.id)
            if 'ACxxxx' in app.config['TWILIO_ACCOUNT_SID']:
                flash(f"Test Mode (No Keys): Your OTP is {otp}")
                return redirect(url_for('verify_otp'))
            try:
                client = Client(app.config['TWILIO_ACCOUNT_SID'], app.config['TWILIO_AUTH_TOKEN'])
                message = client.messages.create(
                    body=f"Your Smart Farmer OTP is: {otp}",
                    from_=app.config['TWILIO_PHONE_NUMBER'],
                    to=phone
                )
                flash(f'OTP Sent via SMS to {phone}!')
                return redirect(url_for('verify_otp'))
            except Exception as e:
                flash(f"SMS Failed. Test Mode OTP: {otp}") 
                return redirect(url_for('verify_otp'))
        else:
            flash('Phone number not found.')
    return render_template('forgot_password.html')

@app.route('/verify_otp', methods=['GET', 'POST'])
def verify_otp():
    if request.method == 'POST':
        entered_otp = request.form.get('otp')
        saved_otp = session.get('otp')
        if saved_otp and int(entered_otp) == saved_otp:
            user_id = session.get('reset_user_id')
            user = User.objects(id=user_id).first()
            login_user(user)
            session.pop('otp', None)
            
            ActivityLog(user=user, action='Logged in via OTP').save()
            
            flash('OTP Verified! Logged in successfully.')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid OTP. Please try again.')
    return render_template('verify_otp.html')

@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
        
    elif current_user.role == 'farmer':
        my_products = list(Product.objects(farmer=current_user))
        incoming_orders = list(Order.objects(farmer=current_user))
        total_sales = Order.objects(farmer=current_user, status='Accepted').count()
        return render_template('farmer_dashboard.html', products=my_products, orders=incoming_orders, sales=total_sales)
    else:
        search_query = request.args.get('q', '')
        if search_query:
            products = list(Product.objects(
                Q(name__icontains=search_query) |
                Q(category__icontains=search_query) |
                Q(location__icontains=search_query)
            ))
        else:
            products = list(Product.objects())
            
        my_orders = list(Order.objects(consumer=current_user))
        return render_template('consumer_dashboard.html', products=products, orders=my_orders, search_query=search_query)

@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash('Unauthorized Access!')
        return redirect(url_for('dashboard'))
        
    users = list(User.objects())
    products = list(Product.objects())
    orders = list(Order.objects().order_by('-date'))
    logs = list(ActivityLog.objects().order_by('-timestamp').limit(100))
    
    total_sales_kg = sum([o.quantity for o in orders if o.status == 'Accepted'])
    total_revenue = sum([(o.quantity * o.product.price) for o in orders if o.status == 'Accepted'])
    total_inventory = sum([p.quantity for p in products])
    
    return render_template('admin_dashboard.html', 
                           users=users, 
                           products=products, 
                           orders=orders, 
                           logs=logs,
                           total_sales_kg=total_sales_kg,
                           total_revenue=total_revenue,
                           total_inventory=total_inventory)

@app.route('/check_forecast', methods=['POST'])
@login_required
def check_forecast():
    product_name = request.form.get('product_check')
    trend, avg_price, avg_qty = forecaster.analyze(product_name)
    
    my_products = list(Product.objects(farmer=current_user))
    incoming_orders = list(Order.objects(farmer=current_user))
    total_sales = Order.objects(farmer=current_user, status='Accepted').count()
    
    return render_template('farmer_dashboard.html', products=my_products, orders=incoming_orders, sales=total_sales, forecast_result={'name': product_name, 'trend': trend, 'price': avg_price, 'qty': avg_qty})

@app.route('/add_product', methods=['POST'])
@login_required
def add_product():
    if current_user.role != 'farmer': return redirect(url_for('index'))
    
    name = request.form.get('name')
    price = float(request.form.get('price'))
    qty = int(request.form.get('quantity'))
    category = request.form.get('category')
    location = request.form.get('location')
    
    image_file = request.files.get('image')
    filename = 'default.jpg'
    
    if image_file and allowed_file(image_file.filename):
        filename = secure_filename(image_file.filename)
        filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
        image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    
    new_prod = Product(name=name, price=price, quantity=qty, category=category, location=location, image=filename, farmer=current_user)
    new_prod.save()
    
    ActivityLog(user=current_user, action=f'Added new product: {name}').save()
    
    flash(f'Product Added!')
    return redirect(url_for('dashboard'))

@app.route('/buy/<string:product_id>', methods=['POST'])
@login_required
def buy_product(product_id):
    product = Product.objects(id=product_id).first()
    order_qty = int(request.form.get('order_quantity', 1))
    
    if product and product.quantity >= order_qty and order_qty > 0:
        new_order = Order(
            product=product, 
            consumer=current_user, 
            farmer=product.farmer,
            quantity=order_qty
        )
        new_order.save()
        
        ActivityLog(user=current_user, action=f'Placed order for {order_qty}kg of {product.name}').save()
        
        flash(f'Order placed for {order_qty} kg of {product.name}!')
    else:
        flash('Invalid quantity or out of stock!')
    return redirect(url_for('dashboard'))

@app.route('/manage_order/<string:order_id>/<action>')
@login_required
def manage_order(order_id, action):
    order = Order.objects(id=order_id).first()
    if not order or order.farmer.id != current_user.id: return "Unauthorized"
    
    if action == 'accept':
        if order.product.quantity >= order.quantity:
            order.status = 'Accepted'
            order.product.quantity -= order.quantity
            order.product.save()
            
            ActivityLog(user=current_user, action=f'Accepted order #{order.id}').save()
            flash('Order Accepted!')
        else:
            flash("Not enough stock left to accept this order!")
    elif action == 'reject': 
        order.status = 'Rejected'
        ActivityLog(user=current_user, action=f'Rejected order #{order.id}').save()
        flash('Order Rejected.')
    
    order.save()
    return redirect(url_for('dashboard'))

@app.route('/logout')
@login_required
def logout():
    ActivityLog(user=current_user, action='Logged out').save()
    logout_user()
    return redirect(url_for('index'))

if __name__ == '__main__':
    # --- HARDCODED ADMIN ACCOUNT CREATION ---
    try:
        admin_user = User.objects(username='Subhajit Rudra').first()
        if not admin_user:
            new_admin = User(
                username='Subhajit Rudra', 
                phone='Admin', 
                password='Subhajit2005', 
                role='admin'
            )
            new_admin.save()
            print("Master Admin Account 'Subhajit Rudra' successfully generated in MongoDB Atlas.")
    except Exception as e:
        print(f"Could not connect to MongoDB Atlas. Please check your password and Network Access IP settings! Error: {e}")
            
    app.run(debug=True)