FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fpl_client.py analysis.py server.py ./

ENV MCP_TRANSPORT=streamable-http
ENV PYTHONUNBUFFERED=1
# Render (and most hosts) inject PORT automatically; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
