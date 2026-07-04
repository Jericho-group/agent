FROM python:3.11-slim

WORKDIR /app

ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "crewai>=0.160.0"

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
