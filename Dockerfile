FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY . .

# usuario sin privilegios
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Por defecto sirve la API. El servicio `worker` del compose sobrescribe el comando: python -m app.worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
