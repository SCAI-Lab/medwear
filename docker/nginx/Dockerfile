FROM nginx:alpine
ARG SERVER_HOST=localhost
ARG SERVER_IP=127.0.0.1
RUN apk add --no-cache openssl && \
    mkdir -p /etc/nginx/certs && \
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout /etc/nginx/certs/localhost.key \
        -out /etc/nginx/certs/localhost.crt \
        -subj "/C=CH/ST=Zurich/L=Zurich/O=MedWear/CN=${SERVER_HOST}" \
        -addext "subjectAltName=DNS:${SERVER_HOST},DNS:localhost,IP:${SERVER_IP},IP:127.0.0.1"
# installed as a template: the entrypoint substitutes NGINX_LOCAL_RESOLVERS
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
COPY nginx.conf /etc/nginx/templates/default.conf.template
