from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models import Cart

cart_bp = Blueprint("cart", __name__)


@cart_bp.route("/sync-cart", methods=["POST"])
@jwt_required()
def sync_cart():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({"error": "Invalid token identity"}), 401

    data = request.get_json() or {}
    items = data.get("items")
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400

    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        cart = Cart(user_id=user_id, items=items)
        db.session.add(cart)
    else:
        cart.items = items
        cart.updated_at = db.func.now()
    db.session.commit()
    return jsonify(cart.to_dict()), 200


@cart_bp.route("/cart", methods=["GET"])
@jwt_required()
def get_cart():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({"error": "Invalid token identity"}), 401
    cart = Cart.query.filter_by(user_id=user_id).first()
    return jsonify(cart.to_dict() if cart else {"items": []}), 200
