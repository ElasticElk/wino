FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY integrations/ integrations/
COPY hardware/ hardware/
COPY models/ models/
COPY server.py .
COPY scanner_service.py .

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
