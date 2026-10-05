FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
COPY flight_monitor /app/flight_monitor

RUN mkdir -p /app/data && chown -R pwuser:pwuser /app
USER pwuser

# Stage 5B supplies DATABASE_URL, Telegram secrets and the external scheduler.
CMD ["python", "-m", "flight_monitor", "check"]
