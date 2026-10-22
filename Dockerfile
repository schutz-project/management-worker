FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home worker

COPY management/ ./management/
COPY portal/ ./portal/

RUN mkdir -p /var/log/management \
    && chown -R worker:worker /app /var/log/management

USER worker

CMD ["python", "-m", "management.worker"]
