from flask import current_app, render_template
from flask_mail import Message, Mail
from threading import Thread
import os
from app.models import User

mail = Mail()


def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            app.logger.error("Failed to send email: %s", e)


def _logo_url():
    return current_app.config.get("EMAIL_LOGO_URL")

def _client_url():
    return os.environ["CLIENT_URL"].rstrip("/")


def _order_link(order_number: str) -> str:
    return f"{_client_url()}/orders/{order_number}"


def _first_name_for_email(customer_email: str) -> str:
    try:
        u = User.query.filter_by(email=customer_email).first()
        return u.first_name if u and u.first_name else 'there'
    except Exception:
        return 'there'


def send_password_reset_code_email(user, code):
    app = current_app._get_current_object()
    if app.testing:
        app.logger.info("--- MOCK EMAIL --- To: %s | Code: %s --- END MOCK EMAIL ---", user.email, code)
        return

    body_html = render_template("email/password_reset.html", user=user, code=code, logo_url=_logo_url())
    msg = Message(
        "Your password reset code",
        sender=("Cinnamon Leather Co", app.config.get("MAIL_USERNAME")),
        recipients=[user.email],
        html=body_html,
    )
    Thread(target=send_async_email, args=(app, msg)).start()


def send_confirm_email(user, verify_url):
    app = current_app._get_current_object()
    if app.testing:
        app.logger.info("--- MOCK EMAIL --- To: %s | Verify: %s --- END MOCK EMAIL ---", user.email, verify_url)
        return

    body_html = render_template("email/confirm_email.html", user=user, verify_url=verify_url, logo_url=_logo_url())
    msg = Message(
        "Confirm your email address",
        sender=("Cinnamon Leather Co", app.config.get("MAIL_USERNAME")),
        recipients=[user.email],
        html=body_html,
    )
    Thread(target=send_async_email, args=(app, msg)).start()


def send_welcome_email(user):
    app = current_app._get_current_object()
    if app.testing:
        app.logger.info("--- MOCK EMAIL --- To: %s | Welcome --- END MOCK EMAIL ---", user.email)
        return

    first_name = user.first_name if getattr(user, 'first_name', None) else 'there'
    body_html = render_template(
        "email/welcome.html",
        user=user,
        first_name=first_name,
        logo_url=_logo_url(),
    )
    msg = Message(
        "Welcome to Cinnamon Leather Company!",
        sender=("Cinnamon Leather Co", app.config.get("MAIL_USERNAME")),
        recipients=[user.email],
        html=body_html,
    )
    Thread(target=send_async_email, args=(app, msg)).start()


def send_receipt_email(customer_email, order_date, order_number, product_names, total):
    app = current_app._get_current_object()
    if app.testing:
        app.logger.info("--- MOCK EMAIL --- To: %s | Receipt order %s --- END MOCK EMAIL ---", customer_email, order_number)
        return

    body_html = render_template(
        "email/receipt.html",
        order_date=order_date,
        order_number=order_number,
        product_names=product_names,
        total=total,
        link=_order_link(order_number),
        first_name=_first_name_for_email(customer_email),
        logo_url=_logo_url(),
    )
    msg = Message(
        "Thank you for your purchase!",
        sender=("Cinnamon Leather Co", app.config.get("MAIL_USERNAME")),
        recipients=[customer_email],
        html=body_html,
    )
    Thread(target=send_async_email, args=(app, msg)).start()


def send_shipped_email(customer_email, order_number, tracking_url=None):
    app = current_app._get_current_object()
    if app.testing:
        app.logger.info("--- MOCK EMAIL --- To: %s | Shipped order %s --- END MOCK EMAIL ---", customer_email, order_number)
        return

    body_html = render_template(
        "email/shipped.html",
        order_number=order_number,
        tracking_url=tracking_url,
        link=_order_link(order_number),
        first_name=_first_name_for_email(customer_email),
        logo_url=_logo_url(),
    )
    msg = Message(
        "Something special is on its way!",
        sender=("Cinnamon Leather Co", app.config.get("MAIL_USERNAME")),
        recipients=[customer_email],
        html=body_html,
    )
    Thread(target=send_async_email, args=(app, msg)).start()


def send_delivered_email(customer_email, order_number, delivery_date):
    app = current_app._get_current_object()
    if app.testing:
        app.logger.info("--- MOCK EMAIL --- To: %s | Delivered order %s --- END MOCK EMAIL ---", customer_email, order_number)
        return

    body_html = render_template(
        "email/delivered.html",
        order_number=order_number,
        delivery_date=delivery_date,
        link=_order_link(order_number),
        first_name=_first_name_for_email(customer_email),
        logo_url=_logo_url(),
    )
    msg = Message(
        "Your order has been delivered!",
        sender=("Cinnamon Leather Co", app.config.get("MAIL_USERNAME")),
        recipients=[customer_email],
        html=body_html,
    )
    Thread(target=send_async_email, args=(app, msg)).start()
