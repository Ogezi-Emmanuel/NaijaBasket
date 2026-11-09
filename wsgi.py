"""WSGI entrypoint.

Local:      flask --app wsgi run --debug
Production: gunicorn -w 4 -k gthread -b 0.0.0.0:8000 wsgi:app
"""

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
