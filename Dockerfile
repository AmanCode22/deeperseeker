FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data && \
    ln -sf /app/data/deeperseeker.db /app/deeperseeker.db

EXPOSE 4000

ENV DB_PATH=/app/data/deeperseeker.db
ENV HOST=0.0.0.0

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD curl -sf http://localhost:4000/health || exit 1

CMD ["python3", "app.py"]
