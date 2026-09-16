FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/services/navigator/

# Create proper package structure so imports work
RUN mkdir -p /app/services && \
    touch /app/services/__init__.py

ENV PYTHONPATH=/app

EXPOSE 8100

CMD ["uvicorn", "services.navigator.main:app", "--host", "0.0.0.0", "--port", "8100"]
