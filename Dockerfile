FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y curl build-essential nodejs npm && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir -r requirements.txt
RUN npm install -g gmgn-cli@1.6.6
RUN chmod +x kida_bot/start.sh start_all.sh

ENV KIDA_MODE=SHADOW
ENV CHAIN=sol

# Expose the API port
EXPOSE 8000

CMD ["./start_all.sh"]
