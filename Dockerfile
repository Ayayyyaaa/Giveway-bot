FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Le contenu de /app/data doit être monté en volume externe (voir docker-compose.yml)
# pour persister entre les mises à jour du bot.
VOLUME ["/app/data"]

CMD ["python", "main.py"]
