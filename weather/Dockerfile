FROM python:3.12-slim

RUN pip install --no-cache-dir requests

COPY weather_service.py find_station.py /app/
COPY static /app/static

EXPOSE 8080
CMD ["python3", "-u", "/app/weather_service.py"]
