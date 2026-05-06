FROM python:3.12-slim

WORKDIR /app

COPY synapse_v2.py ui_v2.html ./

ENV SYNAPSE_PORT=3004 \
    SYNAPSE_DB_PATH=/data/synapse_v2.db \
    SYNAPSE_INSTANCE=synapse-v2

EXPOSE 3004

CMD ["python", "-u", "synapse_v2.py"]
