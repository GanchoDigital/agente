# Usar a imagem oficial do Python
FROM python:3.10-slim

# Define variáveis de ambiente
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Define o diretório de trabalho
WORKDIR /app

# Copia os arquivos de requisitos
COPY requirements.txt .

# Instala as dependências
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código-fonte para o contêiner
COPY . .

# Cria um diretório para os logs
RUN mkdir -p /app/logs && touch /app/bot.log && chmod 777 /app/bot.log

# Expõe a porta que a aplicação utilizará
EXPOSE 3004

# Comando para iniciar a aplicação
CMD ["python", "src/main.py"] 