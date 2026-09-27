FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/tradedesk.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tradedesk ./tradedesk

EXPOSE 8000
# One worker only: debouncing and the follow-up scheduler run in-process.
CMD ["sh", "-c", "uvicorn tradedesk.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
