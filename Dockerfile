FROM node:24-alpine AS frontend-check

WORKDIR /src
COPY panel/app.py panel/app.py
COPY panel/chat_page.py panel/chat_page.py
COPY scripts/check_frontend_syntax.mjs scripts/check_frontend_syntax.mjs
RUN node scripts/check_frontend_syntax.mjs && touch /frontend-syntax-ok

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/panel
COPY panel/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=frontend-check /frontend-syntax-ok /tmp/frontend-syntax-ok
RUN rm /tmp/frontend-syntax-ok
COPY panel/ /app/panel/

RUN mkdir -p /app/data /run/secrets

EXPOSE 3001
CMD ["python", "app.py"]
