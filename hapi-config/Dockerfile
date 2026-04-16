FROM hapiproject/hapi:v7.6.0

# Tiny health-check script that uses Java (the only tool in this distroless image).
# Docker health check calls: java /app/healthcheck.java
COPY --chmod=644 hapi-config/healthcheck.java /app/healthcheck.java
