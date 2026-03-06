from flask import current_app
from flask_mail import Message, Mail
from threading import Thread

mail = Mail()


def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            app.logger.error("Failed to send email: %s", e)


def send_password_reset_code_email(user, code):
    app = current_app._get_current_object()
    if app.testing:
        app.logger.info("--- MOCK EMAIL --- To: %s | Code: %s --- END MOCK EMAIL ---", user.email, code)
        return

    body_html = f"""
    <p>Your password reset code is:</p>
    <h1>{code}</h1>
    <p>This code expires in 15 minutes.</p>
    """
    msg = Message(
        "Your password reset code",
        sender=("Cinnamon Leather Co", app.config.get("MAIL_USERNAME")),
        recipients=[user.email],
        html=body_html,
    )
    Thread(target=send_async_email, args=(app, msg)).start()
