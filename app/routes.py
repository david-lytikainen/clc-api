from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from datetime import timedelta, datetime
import json
from app.extensions import db
from app.models import User, ProductType, Product, ProductImage, Order, Cart, Banner
from uuid import uuid4
import pathlib
from werkzeug.security import generate_password_hash, check_password_hash
import os
import logging
import random
import boto3
import stripe
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

main = Blueprint("main", __name__)

def _generate_order_number():
    for _ in range(10):
        num = ''.join(str(random.randint(0, 9)) for _ in range(6))
        if not Order.query.filter_by(order_number=num).first():
            return num
    return str(random.randint(100000, 999999))

def _is_admin():
    try:
        identity = get_jwt_identity()
        if not identity:
            return False
        user_id = int(identity)
        user = User.query.get(user_id)
        return user and user.role_id == 2
    except Exception:
        return False

@main.route("/create-account", methods=["POST"])
def create_account():
    data = request.get_json() or {}

    required = ["first_name", "last_name", "email", "password"]
    missing = [field for field in required if field not in data]
    if missing:
        return jsonify({"error": "Missing fields", "missing": missing}), 400
    
    if User.query.filter_by(email=data["email"]).first():
        return jsonify({"error": "Email already exists"}), 409

    try:
        hashed_password = generate_password_hash(data["password"])
        user = User(
            role_id=1,
            email=data["email"],
            password=hashed_password,
            first_name=data["first_name"],
            last_name=data["last_name"],
        )
        db.session.add(user)
        db.session.commit()

        token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=1))
        return jsonify({"token": token, "user": user.to_dict()}), 201
    
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to create user"}), 500

@main.route("/sign-in", methods=["POST"])
def sign_in():
    data = request.get_json() or {}

    required = ["email", "password"]
    missing = [field for field in required if field not in data]
    if missing:
        return jsonify({"error": "Missing fields", "missing": missing}), 400

    user = User.query.filter_by(email=data["email"]).first()
    if not user:
        return jsonify({"error": "Invalid email"}), 401
    elif not check_password_hash(user.password, data["password"]):
        return jsonify({"error": "Invalid password"}), 401

    token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=1))
    return jsonify({"token": token, "user": user.to_dict()}), 200

@main.route('/me', methods=['GET'])
@jwt_required()
def me():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify(user.to_dict()), 200

# PRODUCTS
@main.route("/product-types", methods=["GET"])
def get_product_types():
    pts = ProductType.query.order_by(ProductType.name).all()
    return jsonify([p.to_dict() for p in pts]), 200

def _make_s3_client():
    kwargs = {}
    kwargs["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID")
    kwargs["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY")
    kwargs["region_name"] = os.getenv("AWS_REGION")

    return boto3.client("s3", **kwargs)

def _presign_key(key: str, expires: int = 3600) -> str:
    try:
        bucket = os.getenv('S3_BUCKET')
        if not bucket or not key:
            return None
        s3 = _make_s3_client()
        return s3.generate_presigned_url(
            'get_object', Params={'Bucket': bucket, 'Key': key}, ExpiresIn=expires
        )
    except Exception:
        logger.exception('Failed to presign s3 key %s', key)
        return ''

@main.route('/products/create', methods=['POST'])
def create_product():
    form = request.form
    required = ['title', 'price', 'description', 'product_type_id', 'dimensions', 'color']
    missing = [f for f in required if not form.get(f)]
    if missing:
        return jsonify({'error': 'Missing fields', 'missing': missing}), 400

    try:
        product = Product(
            product_type_id=int(form.get('product_type_id')),
            title=form.get('title'),
            description=form.get('description'),
            price=form.get('price'),
            dimensions=form.get('dimensions'),
            color=form.get('color'),
        )
        db.session.add(product)
        db.session.flush()  # assigns product.id

        # create Stripe Product and Price and persist the price id
        stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
        try:
            unit_amount = int(float(product.price) * 100)
            stripe_prod = stripe.Product.create(name=product.title, description=product.description or '')
            stripe_price = stripe.Price.create(product=stripe_prod.id, unit_amount=unit_amount, currency='usd')
            product.stripe_price_id = stripe_price.id
            db.session.add(product)
        except Exception:
            logger.exception('Failed to create stripe product/price')
            raise

        s3 = _make_s3_client()
        bucket = os.getenv('S3_BUCKET')
        uploaded_keys = []

        files = request.files.getlist('images')
        for idx, f in enumerate(files):
            if not f:
                continue
            filename = secure_filename(f.filename or '')
            ext = pathlib.Path(filename).suffix or ''
            key = f"products/{product.id}/{uuid4().hex}{ext}"

            extra_args = {}
            if hasattr(f, 'mimetype') and f.mimetype:
                extra_args['ContentType'] = f.mimetype

            s3.upload_fileobj(f, bucket, key, ExtraArgs=extra_args or None)
            uploaded_keys.append(key)

            pi = ProductImage(product_id=product.id, s3_key=key, sort_order=idx)
            db.session.add(pi)
        db.session.commit()
        first_image = ProductImage.query.filter_by(product_id=product.id).order_by(ProductImage.sort_order).first()
        result = product.to_dict()
        if first_image:
            result['image_url'] = _presign_key(first_image.s3_key)

        return jsonify(result), 201

    except Exception as e:
        logger.exception('Failed to create product')
        db.session.rollback()
        try:
            s3 = _make_s3_client()
            bucket = os.getenv('S3_BUCKET')
            for key in uploaded_keys:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                except Exception:
                    logger.exception('Failed to delete orphaned S3 key %s', key)
        except Exception:
            logger.exception('Failed during S3 cleanup')
        return jsonify({'error': 'Failed to create product'}), 500

@main.route('/products/<product_type>', methods=['GET'])
def get_products(product_type):
    if product_type == 'all':
        products = Product.query.where(Product.is_active == True).order_by(Product.created_at.desc()).all()
    else:
        products = (Product.query.join(ProductType).where(ProductType.name == product_type, Product.is_active == True).order_by(Product.created_at.desc()).all())
    out = []
    for p in products:
        pd = p.to_dict()
        first_image = ProductImage.query.filter_by(product_id=p.id).order_by(ProductImage.sort_order).first()
        pd['image_url'] = _presign_key(first_image.s3_key) if first_image else None
        out.append(pd)
    return jsonify(out), 200

@main.route('/product/<int:product_id>', methods=['GET'])
def get_product(product_id):
    product = Product.query.get_or_404(product_id)
    pd = product.to_dict()
    all_images = ProductImage.query.filter_by(product_id=product.id).order_by(ProductImage.sort_order).all()
    pd['image_urls'] = [_presign_key(img.s3_key) for img in all_images]
    return jsonify(pd), 200

@main.route("/test-email", methods=["POST"])
def test_email():
    
    ses = boto3.client(
        "ses",
        region_name=os.getenv("AWS_REGION"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
    )
    response = ses.send_email(
        Source="orders@cinnamonleatherco.com",
        Destination={"ToAddresses": ["david.lytikainen@gmail.com", "kate.lytikainen@gmail.com"]},
        Message={
            "Subject": {"Data": "Cinnamon Leather Co Test Email"},
            "Body": {"Text": {"Data": "This is a test email from Cinnamon Leather Co."}}
        }
    )
    return {"message_id": response["MessageId"]}

@main.route('/create-cart-checkout-session', methods=['POST'])
@jwt_required(optional=True)
def create_cart_checkout_session():
    data = request.get_json() or {}
    raw_items = data.get('items')
    logger.info('create-cart-checkout-session items=%s', raw_items)
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({'error': 'items must be a non-empty list'}), 400

    stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
    line_items = []
    for it in raw_items:
        try:
            product_id = int(it.get('product_id'))
            quantity = int(it.get('quantity', 1))
        except (TypeError, ValueError):
            logger.warning('create-cart-checkout-session invalid item: %s', it)
            return jsonify({'error': 'Each item must have product_id and quantity'}), 400
        if quantity < 1:
            continue
        product = Product.query.get(product_id)
        if not product or not product.stripe_price_id:
            logger.warning('create-cart-checkout-session product %s missing or no stripe_price_id', product_id)
            return jsonify({'error': f'Product {product_id} not found or has no Stripe price'}), 400
        line_items.append({'price': product.stripe_price_id, 'quantity': quantity})

    if not line_items:
        return jsonify({'error': 'No valid line items'}), 400

    try:
        user_id = None
        try:
            identity = get_jwt_identity()
            user_id = int(identity) if identity else None
        except Exception:
            pass

        success_base = os.getenv('STRIPE_SUCCESS_URL').rstrip('/')
        order_number = _generate_order_number()
        success_url = f"{success_base}/orders/{order_number}"
        cancel_url = os.getenv('STRIPE_CANCEL_CART_URL').rstrip('/')
        session_kwargs = dict(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={'order_number': order_number, 'user_id': str(user_id) if user_id else ''},
        )
        if user_id:
            session_kwargs['client_reference_id'] = str(user_id)

        session = stripe.checkout.Session.create(**session_kwargs)
        return jsonify({'id': session.id, 'url': session.url}), 200
    except Exception:
        logger.exception('Stripe cart checkout session creation failed')
        return jsonify({'error': 'Failed to create checkout session'}), 500

@main.route('/create-checkout-session/<price_id>', methods=['POST'])
@jwt_required(optional=True)
def create_checkout_session(price_id):
    data = request.get_json() or {}
    try:
        quantity = int(data.get('quantity', 1))
    except Exception:
        return jsonify({'error': 'Invalid quantity'}), 400

    stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

    line_items = [{'price': price_id, 'quantity': quantity}]

    try:
        user_identity = get_jwt_identity()
        try:
            user_id = int(user_identity) if user_identity is not None else None
        except Exception:
            user_id = None
    except Exception:
        user_id = None

    try:
        success_base = os.getenv('STRIPE_SUCCESS_URL').rstrip('/')
        order_number = _generate_order_number()
        success_url = f"{success_base}/orders/{order_number}"
        product_id = Product.query.filter_by(stripe_price_id=price_id).first().id
        cancel_url = os.getenv('STRIPE_CANCEL_URL') + str(product_id)

        session_kwargs = dict(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={'order_number': order_number, 'user_id': str(user_id) if user_id else ''},
        )
        if user_id:
            session_kwargs['client_reference_id'] = str(user_id)

        session = stripe.checkout.Session.create(**session_kwargs)
        return jsonify({'id': session.id, 'url': session.url, 'price_id': price_id}), 200
    except Exception:
        logger.exception('Stripe checkout session creation failed')
        return jsonify({'error': 'Failed to create checkout session'}), 500

@main.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

    if webhook_secret:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except ValueError:
            logger.exception('Invalid payload')
            return '', 400
        except stripe.error.SignatureVerificationError:
            logger.exception('Invalid signature')
            return '', 400

    event_type = event.get('type')
    logger.info('Received Stripe event: %s', event_type)

    if event_type == 'checkout.session.completed':
        session = event['data']['object']
        logger.info('Checkout session completed: %s', session.get('id'))
        try:
            stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
            session_obj = stripe.checkout.Session.retrieve(
                session.get('id'), expand=['line_items', 'payment_intent', 'customer_details']
            )
        except Exception:
            logger.exception('Failed to retrieve full session from Stripe')
            return '', 200

        try:
            existing = Order.query.filter_by(session_id=session_obj.id).first()
            if existing:
                logger.info('Order already exists for session %s (order id=%s)', session_obj.id, existing.id)
            else:
                line_items_obj = getattr(session_obj, 'line_items', None)
                line_items_data = getattr(line_items_obj, 'data', None) if line_items_obj else None
                if line_items_data is None and line_items_obj is not None and hasattr(line_items_obj, 'get'):
                    line_items_data = line_items_obj.get('data', [])
                line_items_list = list(line_items_data) if line_items_data else []
                if not line_items_list:
                    logger.warning('No line_items in session %s (expanded: %s)', session_obj.id, bool(line_items_obj))
                payment_intent = getattr(session_obj, 'payment_intent', None) or session_obj.get('payment_intent')
                payment_intent_id = getattr(payment_intent, 'id', None) if payment_intent else None
                customer_email = None
                if getattr(session_obj, 'customer_details', None):
                    try:
                        customer_email = session_obj.customer_details.email
                    except Exception:
                        customer_email = session_obj.get('customer_details', {}).get('email')
                try:
                    user_id_val = None
                    order_number = None
                    if getattr(session_obj, 'metadata', None):
                        try:
                            meta = session_obj.metadata
                            user_id_val = meta.get('user_id') if hasattr(meta, 'get') else getattr(meta, 'user_id', None)
                            order_number = meta.get('order_number') if hasattr(meta, 'get') else getattr(meta, 'order_number', None)
                        except Exception:
                            user_id_val = session_obj.get('metadata', {}).get('user_id')
                            order_number = session_obj.get('metadata', {}).get('order_number')
                    if not user_id_val and getattr(session_obj, 'client_reference_id', None):
                        user_id_val = session_obj.client_reference_id
                    if user_id_val is not None:
                        try:
                            user_id_val = int(user_id_val)
                        except Exception:
                            user_id_val = None
                    if not order_number:
                        order_number = _generate_order_number()
                except Exception:
                    user_id_val = None
                    order_number = _generate_order_number()

                for item in (line_items_list or [{}]):
                    price_obj = item.get('price') if isinstance(item, dict) else getattr(item, 'price', None)
                    if isinstance(price_obj, dict):
                        stripe_price_id = price_obj.get('id')
                    elif price_obj is not None:
                        stripe_price_id = getattr(price_obj, 'id', None) or (price_obj if isinstance(price_obj, str) else None)
                    else:
                        stripe_price_id = item.get('price') if isinstance(item, dict) else None
                    quantity = int(item.get('quantity', 1)) if isinstance(item, dict) else int(getattr(item, 'quantity', 1))
                    amount_cents = int(item.get('amount_total', 0) or 0) if isinstance(item, dict) else int(getattr(item, 'amount_total', 0) or 0)

                    product = Product.query.filter_by(stripe_price_id=stripe_price_id).first() if stripe_price_id else None
                    product_id = product.id if product else None

                    order = Order(
                        user_id=user_id_val,
                        product_id=product_id,
                        session_id=session_obj.id,
                        order_number=order_number,
                        payment_intent_id=payment_intent_id,
                        stripe_price_id=stripe_price_id,
                        quantity=quantity,
                        amount_cents=amount_cents,
                        status='Ordered',
                        customer_email=customer_email,
                        paid_at=(datetime.utcnow() if getattr(session_obj, 'payment_status', None) == 'paid' else None)
                    )
                    db.session.add(order)
                db.session.commit()
                logger.info('Created %s order(s) for session %s', len(line_items_list) or 1, session_obj.id)
                if user_id_val is not None:
                    cart = Cart.query.filter_by(user_id=user_id_val).first()
                    if cart:
                        cart.items = []
                        db.session.commit()
                        logger.info('Cleared cart for user %s after checkout', user_id_val)
        except Exception:
            logger.exception('Error creating order for session %s', session.get('id'))
            try:
                db.session.rollback()
            except Exception:
                logger.exception('Rollback failed')

    return '', 200

@main.route('/orders', methods=['GET'])
@jwt_required()
def get_orders():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401

    orders = Order.query.filter_by(user_id=user_id).order_by(Order.created_at.desc()).all()

    out = []
    for o in orders:
        od = o.to_dict()
        if o.product:
            od['product_title'] = o.product.title
            img = (
                ProductImage.query
                .filter_by(product_id=o.product.id)
                .order_by(ProductImage.sort_order)
                .first()
            )
            if img:
                od['image_url'] = _presign_key(img.s3_key)
            else:
                od['image_url'] = None
        else:
            od['product_title'] = None
            od['image_url'] = None
        out.append(od)

    return jsonify(out), 200

@main.route('/orders/<order_number>', methods=['GET'])
@jwt_required()
def get_order_by_number(order_number):
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401

    orders = Order.query.filter_by(order_number=order_number, user_id=user_id).order_by(Order.id).all()
    if not orders:
        return jsonify({'error': 'Order not found'}), 404

    out = []
    for o in orders:
        od = o.to_dict()
        if o.product:
            od['product_title'] = o.product.title
            img = (
                ProductImage.query
                .filter_by(product_id=o.product.id)
                .order_by(ProductImage.sort_order)
                .first()
            )
            if img:
                od['image_url'] = _presign_key(img.s3_key)
            else:
                od['image_url'] = None
        else:
            od['product_title'] = None
            od['image_url'] = None
        out.append(od)

    return jsonify({'order_number': order_number, 'orders': out}), 200

def _order_to_dict_with_product(o):
    od = o.to_dict()
    if o.product:
        od['product_title'] = o.product.title
        img = ProductImage.query.filter_by(product_id=o.product.id).order_by(ProductImage.sort_order).first()
        od['image_url'] = _presign_key(img.s3_key) if img else None
    else:
        od['product_title'] = None
        od['image_url'] = None
    return od

@main.route('/admin/orders', methods=['GET'])
@jwt_required()
def admin_list_orders():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    page = max(1, int(request.args.get('page', 1)))
    per_page = max(1, min(50, int(request.args.get('per_page', 10))))
    subq = db.session.query(Order.order_number, db.func.min(Order.created_at).label('created_at')).filter(
        Order.order_number.isnot(None)
    ).group_by(Order.order_number).subquery()
    total = db.session.query(db.func.count()).select_from(subq).scalar() or 0
    order_numbers = db.session.query(subq.c.order_number).order_by(subq.c.created_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()
    order_numbers = [r[0] for r in order_numbers]
    orders_by_number = {}
    for onum in order_numbers:
        rows = Order.query.filter_by(order_number=onum).order_by(Order.id).all()
        orders_by_number[onum] = [_order_to_dict_with_product(o) for o in rows]
    return jsonify({
        'orders_by_number': orders_by_number,
        'total': total,
        'page': page,
        'per_page': per_page,
    }), 200

@main.route('/admin/orders/<order_number>', methods=['GET'])
@jwt_required()
def admin_get_order(order_number):
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    orders = Order.query.filter_by(order_number=order_number).order_by(Order.id).all()
    if not orders:
        return jsonify({'error': 'Order not found'}), 404
    out = [_order_to_dict_with_product(o) for o in orders]
    return jsonify({'order_number': order_number, 'orders': out}), 200

@main.route('/admin/orders/<order_number>', methods=['PATCH'])
@jwt_required()
def admin_update_order(order_number):
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    orders = Order.query.filter_by(order_number=order_number).all()
    if not orders:
        return jsonify({'error': 'Order not found'}), 404
    if 'status' in data and data['status'] in ('Ordered', 'Shipped', 'Delivered'):
        for o in orders:
            o.status = data['status']
    if 'tracking_url' in data:
        val = data['tracking_url']
        val = str(val).strip() if val else None
        for o in orders:
            o.tracking_url = val
    if 'comments' in data:
        val = data['comments']
        if isinstance(val, list):
            raw = json.dumps([str(x).strip() for x in val if str(x).strip()])
        else:
            raw = json.dumps([str(val).strip()]) if val and str(val).strip() else None
        for o in orders:
            o.comments = raw
    db.session.commit()
    out = [_order_to_dict_with_product(o) for o in orders]
    return jsonify({'order_number': order_number, 'orders': out}), 200

BANNER_COLORS = ('primary', 'primary_dark', 'secondary', 'secondary_dark')

@main.route('/banner', methods=['GET'])
def get_banner():
    """Public: return the most recent banner only if it is active, else null."""
    banner = Banner.query.order_by(Banner.created_at.desc()).first()
    if not banner or not banner.is_active:
        return jsonify({'banner': None}), 200
    return jsonify({'banner': banner.to_dict()}), 200

@main.route('/admin/banner', methods=['POST'])
@jwt_required()
def admin_create_banner():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    is_active = bool(data.get('is_active', True))
    text = (data.get('text') or '').strip()[:500]
    background_color = (data.get('background_color') or 'primary').strip()
    if background_color not in BANNER_COLORS:
        background_color = 'primary'
    for b in Banner.query.all():
        b.is_active = False
    banner = Banner(is_active=is_active, text=text, background_color=background_color)
    db.session.add(banner)
    db.session.commit()
    return jsonify(banner.to_dict()), 201

@main.route('/sync-cart', methods=['POST'])
@jwt_required()
def sync_cart():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401

    data = request.get_json() or {}
    items = data.get('items')
    if not isinstance(items, list):
        return jsonify({'error': 'items must be a list'}), 400

    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        cart = Cart(user_id=user_id, items=items)
        db.session.add(cart)
    else:
        cart.items = items
        cart.updated_at = db.func.now()
    db.session.commit()
    return jsonify(cart.to_dict()), 200

@main.route('/cart', methods=['GET'])
@jwt_required()
def get_cart():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401
    cart = Cart.query.filter_by(user_id=user_id).first()
    return jsonify(cart.to_dict() if cart else {'items': []}), 200

