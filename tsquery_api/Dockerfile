# Alpine base (no perl/apt/util-linux), current OS patches, no pip/setuptools/wheel at runtime, non-root.
# Python 3.12: app.py uses telnetlib, which was removed in Python 3.13.
FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt ./
RUN apk upgrade --no-cache \
    && pip install --no-cache-dir --no-compile -r requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel \
    && rm -rf /root/.cache \
    && adduser -D -H -u 10001 tsapi
COPY app.py ./
USER tsapi
EXPOSE 8081
CMD ["python", "app.py"]
