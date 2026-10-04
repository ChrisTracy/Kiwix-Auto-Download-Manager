FROM python:3.12-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY app.py schedule.py index.html ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["python", "app.py"]
