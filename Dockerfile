ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
# TZ 用 POSIX 写法（slim 镜像里不一定有 tzdata）。业务里的时间都显式按北京时间算，这里只影响日志时间
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=CST-8 \
    AIUS_DATA_DIR=/data
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir --retries 5 --timeout 120 -i "${PIP_INDEX_URL}" -r requirements.txt
COPY server/aius_server ./aius_server
# 宿主机 umask 077 时 git pull 出来的文件是 0600，非 root 用户会读不到
RUN chmod -R a+rX /app \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1
# 只起一个进程：同步任务、定时调度和通讯录同步都是进程里的后台线程，多进程会各跑一份
CMD ["uvicorn", "--factory", "aius_server.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
