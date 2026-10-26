FROM python:3.12-slim

# curl is only needed for the compose healthcheck; psycopg2-binary ships its own libpq
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Matches bitcoind's UID=1000/GID=1000 so bind-mounted host files stay writable
RUN useradd --uid 1000 --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "portfolio_web:app", "--app-dir", "/app/src", "--host", "0.0.0.0", "--port", "8000"]
