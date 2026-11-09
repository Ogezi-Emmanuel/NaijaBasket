FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer is cached across app-code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Shell form so $PORT (injected by Render / Railway / Fly) expands at runtime.
CMD gunicorn -w 4 -k gthread --timeout 60 -b 0.0.0.0:${PORT:-8000} wsgi:app
