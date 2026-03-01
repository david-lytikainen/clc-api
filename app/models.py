from app.extensions import db
import json


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    reset_token = db.Column(db.String(255), unique=True, nullable=True)
    reset_token_expiration = db.Column(db.TIMESTAMP(timezone=True), nullable=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.TIMESTAMP(timezone=True), nullable=False, server_default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "role_id": self.role_id,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "phone": self.phone,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)


class ProductType(db.Model):
    __tablename__ = "product_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
        }


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    product_type_id = db.Column(db.Integer, db.ForeignKey("product_types.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    dimensions = db.Column(db.String(100), nullable=True)
    color = db.Column(db.String(50), nullable=True)
    stripe_price_id = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.text('true'))
    created_at = db.Column(db.TIMESTAMP(timezone=True), nullable=False, server_default=db.func.now())

    product_type = db.relationship("ProductType", backref=db.backref("products", lazy=True))

    def to_dict(self):
        return {
            "id": self.id,
            "product_type_id": self.product_type_id,
            "title": self.title,
            "description": self.description,
            "price": float(self.price) if self.price is not None else None,
            "stripe_price_id": self.stripe_price_id,
            "dimensions": self.dimensions,
            "color": self.color,
            "is_active": bool(self.is_active),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ProductImage(db.Model):
    __tablename__ = "product_images"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    s3_key = db.Column(db.String(512), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, server_default=db.text('0'))

    product = db.relationship("Product", backref=db.backref("images", lazy=True))

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "s3_key": self.s3_key,
            "sort_order": self.sort_order,
        }

class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    session_id = db.Column(db.String(255), nullable=False, index=True)
    order_number = db.Column(db.String(6), nullable=True, index=True)
    payment_intent_id = db.Column(db.String(255))
    stripe_price_id = db.Column(db.String(255))
    quantity = db.Column(db.Integer)
    amount_cents = db.Column(db.Integer)
    status = db.Column(db.String(20))
    customer_email = db.Column(db.String(255))
    created_at = db.Column(db.TIMESTAMP(timezone=True), server_default=db.func.now())
    paid_at = db.Column(db.TIMESTAMP(timezone=True), nullable=True)
    tracking_url = db.Column(db.String(512), nullable=True)
    comments = db.Column(db.Text, nullable=True)

    product = db.relationship('Product', backref=db.backref('orders', lazy='joined'), lazy='joined')

    def _comments_as_list(self):
        if not self.comments or not self.comments.strip():
            return []
        try:
            parsed = json.loads(self.comments)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
            return [str(self.comments)]
        except (ValueError, TypeError):
            return [self.comments] if self.comments else []

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "product_id": self.product_id,
            "session_id": self.session_id,
            "order_number": self.order_number,
            "payment_intent_id": self.payment_intent_id,
            "stripe_price_id": self.stripe_price_id,
            "quantity": self.quantity,
            "amount_cents": self.amount_cents,
            "status": self.status,
            "customer_email": self.customer_email,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "tracking_url": self.tracking_url,
            "comments": self._comments_as_list(),
        }
    

class Cart(db.Model):
    __tablename__ = 'carts'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    guest_token = db.Column(db.String(255), nullable=True)
    items = db.Column(db.JSON, nullable=False)
    updated_at = db.Column(db.TIMESTAMP(timezone=True), nullable=False, server_default=db.func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'guest_token': self.guest_token,
            'items': self.items,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }