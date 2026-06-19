# Shared image for every PortLess service. The service to run is selected at
# runtime via the SERVICE_MODULE env var (e.g. services.deals.main:app).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV SERVICE_MODULE=services.api_gateway.main:app
ENV PORT=8000

CMD ["sh", "-c", "uvicorn $SERVICE_MODULE --host 0.0.0.0 --port $PORT"]
