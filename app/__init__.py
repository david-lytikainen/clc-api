from dotenv import load_dotenv
from flask import Flask
from flask_cors import CORS
import os
from datetime import timedelta
from app.extensions import db, jwt
from app.routes import main

load_dotenv()

def create_app():
    app = Flask(__name__)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "postgresql://localhost/SAS")
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "your-secret-key")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=1)
    app.config["JWT_TOKEN_LOCATION"] = ["headers"]
    app.config["JWT_HEADER_NAME"] = "Authorization"
    app.config["JWT_HEADER_TYPE"] = "Bearer"

    db.init_app(app)
    jwt.init_app(app)

    app.register_blueprint(main, url_prefix="/api")

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
