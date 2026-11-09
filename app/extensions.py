"""Shared extension instances (imported by models, blueprints and the factory)."""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()

#: No global default: limits are attached to the endpoints that need them
#: (credential submission and provider webhooks). A blanket per-IP cap would
#: punish legitimate Lagos buyers sharing a carrier CGNAT address.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    headers_enabled=True,
)

login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"
