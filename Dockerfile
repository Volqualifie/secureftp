FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# 非 root 用户：即使服务被攻破，也只能写 /data
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin ftp \
 && mkdir -p /data/storage && chown -R ftp:ftp /data
WORKDIR /app
COPY requirements-server.txt .
RUN pip install -r requirements-server.txt
COPY protocol ./protocol
COPY server ./server
USER ftp
EXPOSE 9443
CMD ["python", "-m", "server.main", "--config", "/app/server/config.docker.yaml"]