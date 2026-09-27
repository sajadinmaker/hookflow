FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
# Migrations and alembic.ini ship with the image so `alembic upgrade head` can
# run as a deploy step (or a one-shot init container) against the same code that
# the app serves.
COPY alembic.ini ./
COPY migrations/ ./migrations/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Schema is applied separately, before this process starts. The app never
# creates tables at boot — see docs/deployment.md.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
