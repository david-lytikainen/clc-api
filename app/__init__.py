from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS
import os
from datetime import timedelta
from app.extensions import db, jwt
from app.routes_all import main
from app.routes.user_routes import user_bp
from app.utils.email import mail

load_dotenv()

def create_app():
    app = Flask(__name__)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "postgresql://localhost/SAS")
    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", str(50 * 1024 * 1024)))
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "your-secret-key")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=1)
    app.config["JWT_TOKEN_LOCATION"] = ["headers"]
    app.config["JWT_HEADER_NAME"] = "Authorization"
    app.config["JWT_HEADER_TYPE"] = "Bearer"

    app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER")
    app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", 587))
    app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "true").lower() in ("true", "1", "t")
    app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")
    app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD")
    base_url = (os.getenv("CLIENT_URL")).rstrip("/")
    app.config["EMAIL_LOGO_URL"] = f"{base_url}/logo.svg"

    db.init_app(app)
    jwt.init_app(app)
    mail.init_app(app)

    @jwt.unauthorized_loader
    def unauthorized_callback(reason):
        return jsonify({"error": "Missing or invalid token", "msg": reason}), 401

    @jwt.invalid_token_loader
    def invalid_token_callback(reason):
        return jsonify({"error": "Invalid token", "msg": reason}), 401

    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_payload):
        return jsonify({"error": "Token expired", "msg": "Token has expired"}), 401

    app.register_blueprint(main, url_prefix="/api")
    app.register_blueprint(user_bp, url_prefix="/api")

    CORS(
        app,
        resources={
            r"/api/*": {"origins": "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5001,*".split(',')},
        },
        supports_credentials=True,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        expose_headers=["Content-Type"],
    )

    return app
