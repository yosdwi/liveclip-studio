FROM node:22-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN mkdir -p /app/public
ARG NEXT_PUBLIC_CORE_URL=https://liveclip-core-production.up.railway.app
ENV NEXT_PUBLIC_CORE_URL=$NEXT_PUBLIC_CORE_URL
RUN npm run build
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 PORT=${PORT:-3000} node server.js"]
