FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opcua_adapter.py .

CMD ["python", "opcua_adapter.py"]
