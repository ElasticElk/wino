FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libzbar0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY integrations/ integrations/
COPY hardware/ hardware/
COPY models/ models/
COPY assets/ assets/
COPY server.py .
COPY scanner_service.py .

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
