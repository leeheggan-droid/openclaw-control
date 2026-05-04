FROM python:3.11-slim

RUN pip install --no-cache-dir ansible

WORKDIR /work

ENTRYPOINT ["ansible-playbook"]
