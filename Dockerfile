# Exemple simplifié
FROM python:3.11

WORKDIR /app
COPY . /app

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# ⚠️ ne lance rien ici (comme runserver), car ça sera fait dans docker-compose
