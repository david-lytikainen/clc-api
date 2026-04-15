import json
import logging
import os
from datetime import datetime

import stripe
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models import Cart, Color, Order, Product, User
from app.routes.common import (
    generate_order_number,
    order_to_dict_with_product,
    product_has_color_image,
    stripe_metadata_dict,
)
from app.utils.email import send_receipt_email
from app.utils.shipping import (
    infer_shipping_tier_from_title,
    normalize_us_zip,
    shipping_cents_for_lines,
    zone_for_zip,
)

logger = logging.getLogger(__name__)

stripe_bp = Blueprint("stripe", __name__)


def _shipping_stripe_line(shipping_cents: int) -> dict:
    return {
        "price_data": {
            "currency": "usd",
            "product_data": {"name": "Shipping and handling"},
            "unit_amount": int(shipping_cents),
        },
        "quantity": 1,
    }

def _free_shipping_stripe_line() -> dict:
    return {
        "price_data": {
            "currency": "usd",
            "product_data": {"name": "Free Shipping"},
            "unit_amount": 0,
        },
        "quantity": 1,
    }


@stripe_bp.route("/validate-shipping-zip", methods=["POST"])
def validate_shipping_zip():
    data = request.get_json() or {}
    raw = (data.get("zip") or "").strip()
    normalized = normalize_us_zip(raw)
    if not normalized:
        return jsonify({"valid": False}), 200
    zone = zone_for_zip(normalized)
    if zone is None:
        return jsonify({"valid": False}), 200
    return jsonify({"valid": True, "zone": zone}), 200


@stripe_bp.route("/create-cart-checkout-session", methods=["POST"])
@jwt_required(optional=True)
def create_cart_checkout_session():
    data = request.get_json() or {}
    raw_items = data.get("items")
    logger.info("create-cart-checkout-session items=%s", raw_items)
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"error": "items must be a non-empty list"}), 400

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    line_items = []
    item_colors = []
    for it in raw_items:
        try:
            product_id = int(it.get("product_id"))
            quantity = int(it.get("quantity", 1))
            color_id = int(it.get("color_id"))
        except (TypeError, ValueError):
            logger.warning("create-cart-checkout-session invalid item: %s", it)
            return jsonify({"error": "Each item must have product_id, quantity, and color_id"}), 400
        if quantity < 1:
            continue
        product = Product.query.get(product_id)
        if not product or not product.stripe_price_id:
            logger.warning(
                "create-cart-checkout-session product %s missing or no stripe_price_id", product_id
            )
            return jsonify({"error": f"Product {product_id} not found or has no Stripe price"}), 400
        if not product_has_color_image(product_id, color_id):
            return jsonify({"error": f"Invalid color_id {color_id} for product {product_id}"}), 400
        line_items.append({"price": product.stripe_price_id, "quantity": quantity})
        item_colors.append({"color_id": color_id})

    if not line_items:
        return jsonify({"error": "No valid line items"}), 400

    # Shipping is free: show a $0 line item in Stripe.
    line_items.append(_free_shipping_stripe_line())

    try:
        user_id = None
        try:
            identity = get_jwt_identity()
            user_id = int(identity) if identity else None
        except Exception:
            pass
        customer_email = None
        if user_id:
            try:
                user = User.query.get(user_id)
                customer_email = user.email if user and user.email else None
            except Exception:
                customer_email = None

        success_base = os.getenv("STRIPE_SUCCESS_URL").rstrip("/")
        order_number = generate_order_number()
        success_url = f"{success_base}/orders/{order_number}"
        cancel_url = os.getenv("STRIPE_CANCEL_CART_URL").rstrip("/")
        allergic = data.get("allergic_to_cinnamon")
        meta = {
            "order_number": order_number,
            "user_id": str(user_id) if user_id else "",
            "item_colors": json.dumps(item_colors),
        }
        if allergic is not None:
            meta["allergic_to_cinnamon"] = "true" if allergic else "false"
        session_kwargs = dict(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=meta,
            shipping_address_collection={"allowed_countries": ["US"]},
            automatic_tax={"enabled": True},
        )
        if customer_email:
            session_kwargs["customer_email"] = customer_email
        if user_id:
            session_kwargs["client_reference_id"] = str(user_id)

        session = stripe.checkout.Session.create(**session_kwargs)
        return jsonify({"id": session.id, "url": session.url}), 200
    except Exception:
        logger.exception("Stripe cart checkout session creation failed")
        return jsonify({"error": "Failed to create checkout session"}), 500


@stripe_bp.route("/create-checkout-session/<price_id>", methods=["POST"])
@jwt_required(optional=True)
def create_checkout_session(price_id):
    data = request.get_json() or {}
    try:
        quantity = int(data.get("quantity", 1))
        color_id = int(data.get("color_id"))
    except Exception:
        return jsonify({"error": "Invalid quantity or missing color_id"}), 400

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

    product_row = Product.query.filter_by(stripe_price_id=price_id).first()
    if not product_row:
        return jsonify({"error": "Product not found for price"}), 400
    if not product_has_color_image(product_row.id, color_id):
        return jsonify({"error": "Invalid color_id for this product"}), 400

    line_items = [{"price": price_id, "quantity": quantity}, _free_shipping_stripe_line()]

    try:
        user_identity = get_jwt_identity()
        try:
            user_id = int(user_identity) if user_identity is not None else None
        except Exception:
            user_id = None
    except Exception:
        user_id = None
    customer_email = None
    if user_id:
        try:
            user = User.query.get(user_id)
            customer_email = user.email if user and user.email else None
        except Exception:
            customer_email = None

    try:
        success_base = os.getenv("STRIPE_SUCCESS_URL").rstrip("/")
        order_number = generate_order_number()
        success_url = f"{success_base}/orders/{order_number}"
        cancel_url = os.getenv("STRIPE_CANCEL_URL") + str(product_row.id)
        allergic = data.get("allergic_to_cinnamon")
        meta = {
            "order_number": order_number,
            "user_id": str(user_id) if user_id else "",
            "color_id": str(color_id),
        }
        if allergic is not None:
            meta["allergic_to_cinnamon"] = "true" if allergic else "false"
        session_kwargs = dict(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=meta,
            shipping_address_collection={"allowed_countries": ["US"]},
            automatic_tax={"enabled": True},
        )
        if customer_email:
            session_kwargs["customer_email"] = customer_email
        if user_id:
            session_kwargs["client_reference_id"] = str(user_id)

        session = stripe.checkout.Session.create(**session_kwargs)
        return jsonify({"id": session.id, "url": session.url, "price_id": price_id}), 200
    except Exception:
        logger.exception("Stripe checkout session creation failed")
        return jsonify({"error": "Failed to create checkout session"}), 500


@stripe_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if webhook_secret:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except ValueError:
            logger.exception("Invalid payload")
            return "", 400
        except stripe.error.SignatureVerificationError:
            logger.exception("Invalid signature")
            return "", 400

    event_type = event.get("type")
    logger.info("Received Stripe event: %s", event_type)

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        logger.info("Checkout session completed: %s", session.get("id"))
        try:
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
            session_obj = stripe.checkout.Session.retrieve(
                session.get("id"), expand=["line_items", "payment_intent", "customer_details"]
            )
        except Exception:
            logger.exception("Failed to retrieve full session from Stripe")
            return "", 200

        try:
            existing = Order.query.filter_by(session_id=session_obj.id).first()
            if existing:
                logger.info("Order already exists for session %s (order id=%s)", session_obj.id, existing.id)
            else:
                line_items_obj = getattr(session_obj, "line_items", None)
                line_items_data = getattr(line_items_obj, "data", None) if line_items_obj else None
                if line_items_data is None and line_items_obj is not None and hasattr(line_items_obj, "get"):
                    line_items_data = line_items_obj.get("data", [])
                line_items_list = list(line_items_data) if line_items_data else []
                if not line_items_list:
                    logger.warning("No line_items in session %s (expanded: %s)", session_obj.id, bool(line_items_obj))
                payment_intent = getattr(session_obj, "payment_intent", None) or session_obj.get("payment_intent")
                payment_intent_id = getattr(payment_intent, "id", None) if payment_intent else None
                customer_email = None
                if getattr(session_obj, "customer_details", None):
                    try:
                        customer_email = session_obj.customer_details.email
                    except Exception:
                        customer_email = session_obj.get("customer_details", {}).get("email")
                shipping_address_raw = "{}"
                try:
                    collected_information = getattr(session_obj, "collected_information", None) or session_obj.get(
                        "collected_information", {}
                    )
                    if hasattr(collected_information, "to_dict"):
                        collected_information = collected_information.to_dict()
                    shipping_details = (
                        collected_information.get("shipping_details", {})
                        if isinstance(collected_information, dict)
                        else {}
                    )
                    if hasattr(shipping_details, "to_dict"):
                        shipping_details = shipping_details.to_dict()
                    shipping_address_payload = (
                        shipping_details.get("address", {}) if isinstance(shipping_details, dict) else {}
                    )
                    if hasattr(shipping_address_payload, "to_dict"):
                        shipping_address_payload = shipping_address_payload.to_dict()
                    if isinstance(shipping_address_payload, dict):
                        shipping_address_raw = json.dumps(shipping_address_payload)
                except Exception:
                    shipping_address_raw = "{}"
                try:
                    user_id_val = None
                    order_number = None
                    allergic_to_cinnamon_meta = None
                    if getattr(session_obj, "metadata", None):
                        try:
                            meta = session_obj.metadata
                            user_id_val = meta.get("user_id") if hasattr(meta, "get") else getattr(meta, "user_id", None)
                            order_number = (
                                meta.get("order_number") if hasattr(meta, "get") else getattr(meta, "order_number", None)
                            )
                            ac = (
                                meta.get("allergic_to_cinnamon")
                                if hasattr(meta, "get")
                                else getattr(meta, "allergic_to_cinnamon", None)
                            )
                            allergic_to_cinnamon_meta = ac
                        except Exception:
                            user_id_val = session_obj.get("metadata", {}).get("user_id")
                            order_number = session_obj.get("metadata", {}).get("order_number")
                            allergic_to_cinnamon_meta = session_obj.get("metadata", {}).get("allergic_to_cinnamon")
                    if not user_id_val and getattr(session_obj, "client_reference_id", None):
                        user_id_val = session_obj.client_reference_id
                    if user_id_val is not None:
                        try:
                            user_id_val = int(user_id_val)
                        except Exception:
                            user_id_val = None
                    if not order_number:
                        order_number = generate_order_number()
                    try:
                        allergic_to_cinnamon_order = allergic_to_cinnamon_meta == "true" if allergic_to_cinnamon_meta else None
                    except Exception:
                        allergic_to_cinnamon_order = None
                except Exception:
                    user_id_val = None
                    order_number = generate_order_number()
                    allergic_to_cinnamon_order = None

                meta_flat = stripe_metadata_dict(session_obj)
                item_colors_raw = meta_flat.get("item_colors")
                item_colors_list = []
                if item_colors_raw:
                    try:
                        item_colors_list = json.loads(item_colors_raw)
                    except (json.JSONDecodeError, TypeError):
                        item_colors_list = []
                single_color_meta = meta_flat.get("color_id")

                rows_to_process = line_items_list if line_items_list else []
                product_line_index = 0
                for idx, item in enumerate(rows_to_process):
                    price_obj = item.get("price") if isinstance(item, dict) else getattr(item, "price", None)
                    if isinstance(price_obj, dict):
                        stripe_price_id = price_obj.get("id")
                    elif price_obj is not None:
                        stripe_price_id = getattr(price_obj, "id", None) or (
                            price_obj if isinstance(price_obj, str) else None
                        )
                    else:
                        stripe_price_id = item.get("price") if isinstance(item, dict) else None
                    quantity = int(item.get("quantity", 1)) if isinstance(item, dict) else int(getattr(item, "quantity", 1))
                    amount_cents = (
                        int(item.get("amount_total", 0) or 0)
                        if isinstance(item, dict)
                        else int(getattr(item, "amount_total", 0) or 0)
                    )

                    product = Product.query.filter_by(stripe_price_id=stripe_price_id).first() if stripe_price_id else None
                    if not product:
                        continue

                    product_id = product.id

                    color_id_order = 1
                    if item_colors_list and product_line_index < len(item_colors_list):
                        try:
                            color_id_order = int(item_colors_list[product_line_index].get("color_id"))
                        except (TypeError, ValueError):
                            color_id_order = 1
                    elif single_color_meta is not None and product_line_index == 0:
                        try:
                            color_id_order = int(single_color_meta)
                        except (TypeError, ValueError):
                            color_id_order = 1
                    if product_id and not product_has_color_image(product_id, color_id_order):
                        logger.warning(
                            "Stripe webhook: color_id %s invalid for product %s, using 1",
                            color_id_order,
                            product_id,
                        )
                        color_id_order = 1

                    order = Order(
                        user_id=user_id_val,
                        product_id=product_id,
                        color_id=color_id_order,
                        session_id=session_obj.id,
                        order_number=order_number,
                        payment_intent_id=payment_intent_id,
                        stripe_price_id=stripe_price_id,
                        quantity=quantity,
                        amount_cents=amount_cents,
                        status="Ordered",
                        customer_email=customer_email,
                        shipping_address=shipping_address_raw,
                        paid_at=(datetime.utcnow() if getattr(session_obj, "payment_status", None) == "paid" else None),
                        allergic_to_cinnamon=allergic_to_cinnamon_order,
                    )
                    db.session.add(order)
                    product_line_index += 1
                db.session.commit()
                logger.info("Created %s order(s) for session %s", len(rows_to_process), session_obj.id)
                if customer_email:
                    orders_for_receipt = Order.query.filter_by(session_id=session_obj.id).order_by(Order.id).all()
                    if orders_for_receipt:
                        first = orders_for_receipt[0]
                        order_date = first.created_at.strftime("%B %d, %Y") if first.created_at else ""
                        receipt_lines = []
                        for o in orders_for_receipt:
                            title = o.product.title if o.product else "Item"
                            qty = o.quantity or 1
                            color_row = Color.query.get(o.color_id) if getattr(o, "color_id", None) else None
                            color_name = color_row.name if color_row else None
                            line_cents = int(o.amount_cents or 0)
                            line_amt = f"${line_cents / 100:.2f}"
                            if color_name:
                                receipt_lines.append(f"{title} × {qty} in {color_name} — {line_amt}")
                            else:
                                receipt_lines.append(f"{title} × {qty} — {line_amt}")
                        ship_meta = meta_flat.get("shipping_cents")
                        if ship_meta:
                            try:
                                sc = int(ship_meta)
                                if sc > 0:
                                    sz = meta_flat.get("shipping_zip") or ""
                                    receipt_lines.append(f"Shipping and handling (ZIP {sz}) — ${sc / 100:.2f}")
                            except (TypeError, ValueError):
                                pass
                        at_total = getattr(session_obj, "amount_total", None)
                        if at_total is None and isinstance(session_obj, dict):
                            at_total = session_obj.get("amount_total")
                        try:
                            total_cents = int(at_total or 0)
                        except (TypeError, ValueError):
                            total_cents = sum(o.amount_cents or 0 for o in orders_for_receipt)
                        total_formatted = f"${total_cents / 100:.2f}"
                        send_receipt_email(
                            customer_email,
                            order_date,
                            first.order_number,
                            receipt_lines,
                            total_formatted,
                        )
                if user_id_val is not None:
                    cart = Cart.query.filter_by(user_id=user_id_val).first()
                    if cart:
                        cart.items = []
                        db.session.commit()
                        logger.info("Cleared cart for user %s after checkout", user_id_val)
        except Exception:
            logger.exception("Error creating order for session %s", session.get("id"))
            try:
                db.session.rollback()
            except Exception:
                logger.exception("Rollback failed")

    return "", 200


@stripe_bp.route("/orders", methods=["GET"])
@jwt_required()
def get_orders():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({"error": "Invalid token identity"}), 401

    orders = Order.query.filter_by(user_id=user_id).order_by(Order.created_at.desc()).all()

    out = [order_to_dict_with_product(o) for o in orders]

    return jsonify(out), 200


@stripe_bp.route("/orders/<order_number>", methods=["GET"])
def get_order_by_number(order_number):
    orders = Order.query.filter_by(order_number=order_number).order_by(Order.id).all()
    if not orders:
        return jsonify({"error": "Order not found"}), 404

    out = [order_to_dict_with_product(o) for o in orders]

    return jsonify({"order_number": order_number, "orders": out}), 200
