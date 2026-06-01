FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
COPY plans.json .

ENV PYTHONUNBUFFERED=1
ENV PLANS_FILE=/app/plans.json

CMD ["uvicorn", "bot.main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "asyncio"]
