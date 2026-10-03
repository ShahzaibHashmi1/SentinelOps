# Load-test runner (compose profile "load"). Locust pinned; runs as root only because it writes
# CSV files into the bind-mounted loadtest/results folder.
FROM python:3.12-slim
RUN pip install --no-cache-dir locust==2.46.6
WORKDIR /loadtest
COPY loadtest/locustfile.py .
ENTRYPOINT ["locust"]
