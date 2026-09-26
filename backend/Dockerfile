FROM python:3.11-slim

WORKDIR /app

# Evita criação de arquivos .pyc e buffer de saída
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instala dependências de sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Instala dependências do Python
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia backend e frontend
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

WORKDIR /app/backend

# Expõe a porta padrão do servidor web
EXPOSE 8000

# Comando para iniciar o servidor em produção
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

