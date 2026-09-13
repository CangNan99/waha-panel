FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/panel
COPY panel/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY panel/ /app/panel/

RUN mkdir -p /app/data /run/secrets

EXPOSE 3001
CMD ["python", "app.py"]
