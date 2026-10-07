FROM python:3.12-alpine
RUN apk add --no-cache tzdata
# kiwix-manage writes library.xml; the kiwix-tools image ships it as a static binary
COPY --from=ghcr.io/kiwix/kiwix-tools:latest /usr/local/bin/kiwix-manage /usr/local/bin/kiwix-manage
WORKDIR /app
COPY app.py schedule.py index.html login.html ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["python", "app.py"]
