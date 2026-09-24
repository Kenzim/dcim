FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends isc-dhcp-server && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY dhcp_runner/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY dhcp_runner/main.py ./main.py
COPY runner_common ./runner_common
ENV PYTHONUNBUFFERED=1
EXPOSE 9080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9080"]
