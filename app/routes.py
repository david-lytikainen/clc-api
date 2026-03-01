from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from datetime import timedelta, datetime
from app.extensions import db
from app.models import User, ProductType, Product, ProductImage, Order, Cart
from uuid import uuid4
import pathlib
from werkzeug.security import generate_password_hash, check_password_hash
import os
import logging
import boto3
import stripe
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

main = Blueprint("main", __name__)

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

        token = create_access_token(identity=user.id, expires_delta=timedelta(days=1))
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

    # attach authenticated user if available so webhook can assign Order.user_id
    try:
        user_identity = get_jwt_identity()
        try:
            user_id = int(user_identity) if user_identity is not None else None
        except Exception:
            user_id = None
    except Exception:
        user_id = None

    try:
        # attach price_id to success_url so frontend can know which price was used
        success_base = os.getenv('STRIPE_SUCCESS_URL')
        sep = '&' if '?' in success_base else '?'
        success_url = f"{success_base}{sep}price_id={price_id}"
        product_id = Product.query.filter_by(stripe_price_id=price_id).first().id
        cancel_url = os.getenv('STRIPE_CANCEL_URL') + str(product_id)

        session_kwargs = dict(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if user_id:
            # metadata and client_reference_id are server-set and safe
            session_kwargs['client_reference_id'] = str(user_id)
            session_kwargs['metadata'] = {'user_id': str(user_id)}

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
                line_items = getattr(session_obj, 'line_items', {}).get('data', []) if getattr(session_obj, 'line_items', None) else session_obj.get('line_items', {}).get('data', [])
                if line_items:
                    item = line_items[0]
                    price_obj = item.get('price') if isinstance(item, dict) else None
                    stripe_price_id = price_obj.get('id') if isinstance(price_obj, dict) else (item.get('price') if isinstance(item, dict) else None)
                    quantity = int(item.get('quantity', 1)) if isinstance(item, dict) else 1
                else:
                    stripe_price_id = None
                    quantity = 1

                product = Product.query.filter_by(stripe_price_id=stripe_price_id).first() if stripe_price_id else None
                product_id = product.id if product else None

                amount_cents = int(getattr(session_obj, 'amount_total', None) or session_obj.get('amount_total') or 0)
                payment_intent = getattr(session_obj, 'payment_intent', None) or session_obj.get('payment_intent')
                payment_intent_id = payment_intent.id
                customer_email = None
                if getattr(session_obj, 'customer_details', None):
                    try:
                        customer_email = session_obj.customer_details.email
                    except Exception:
                        customer_email = session_obj.get('customer_details', {}).get('email')

                # try to attach user from session metadata or client_reference_id
                try:
                    user_id_val = None
                    # metadata may be an object on expanded session or a dict
                    if getattr(session_obj, 'metadata', None):
                        try:
                            user_id_val = session_obj.metadata.get('user_id')
                        except Exception:
                            user_id_val = session_obj.get('metadata', {}).get('user_id')
                    if not user_id_val and getattr(session_obj, 'client_reference_id', None):
                        user_id_val = session_obj.client_reference_id
                    if user_id_val is not None:
                        try:
                            user_id_val = int(user_id_val)
                        except Exception:
                            # leave as None if it can't be parsed
                            user_id_val = None
                except Exception:
                    user_id_val = None

                order = Order(
                    user_id=user_id_val,
                    product_id=product_id,
                    session_id=session_obj.id,
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
                logger.info('Created order id=%s for session %s', order.id, session_obj.id)
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

