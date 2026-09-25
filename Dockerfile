FROM python:3.12-alpine
WORKDIR /app
COPY seap_monitor.py /app/seap_monitor.py
COPY dashboard.py /app/dashboard.py
CMD ["python", "/app/dashboard.py"]
