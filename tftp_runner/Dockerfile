FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tftpd-hpa && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Use tftp_runner-specific requirements and code from the repo root context
COPY tftp_runner/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY tftp_runner/main.py ./main.py

# Bake PXE/TFTP tree into the runner image.
# This should contain pxelinux/ipxe loaders and temp OS TFTP files.
RUN mkdir -p /shared/tftp
COPY tftp/ /shared/tftp/

ENV PYTHONUNBUFFERED=1
EXPOSE 9081

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9081"]
