FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.29-alpine
COPY nginx-main.conf /etc/nginx/nginx.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY runtime-config.js.template /etc/enterprise-rag/runtime-config.js.template
COPY docker-entrypoint.d/40-runtime-config.sh /docker-entrypoint.d/40-runtime-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
RUN mkdir -p /tmp/nginx/client_temp /tmp/nginx/proxy_temp /tmp/nginx/fastcgi_temp \
    /tmp/nginx/uwsgi_temp /tmp/nginx/scgi_temp \
    /tmp/enterprise-rag-runtime \
    && chmod 0555 /docker-entrypoint.d/40-runtime-config.sh \
    && chown -R 101:101 /tmp/nginx /tmp/enterprise-rag-runtime
EXPOSE 8080
USER 101:101
