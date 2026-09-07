# syntax=docker/dockerfile:1

# Vector's web image: the built Vite bundle, served by nginx, with the
# backend proxied onto the same origin. See nginx.conf for why the proxy is
# not optional.
#
# Build context is `frontend/`, not the repository root, so this build reads
# frontend/.dockerignore and cannot see .env, migrations/ or app/ at all.

# Pinned to the major that .github/workflows/ci.yml sets and that
# package.json's `engines.node: >=20.19` allows, so the bundle CI gates is
# built by the same major that builds the image. Pinned to a major rather
# than a patch on purpose: the patch stream is where Node's security fixes
# arrive, and a frozen patch is a frozen vulnerability.
ARG NODE_VERSION=22

# nginx-unprivileged rather than the nginx image: the stock one starts its
# master process as root to bind port 80, and there is nothing this container
# does that needs it. This variant listens on 8080 as uid 101 with no
# chown-ing, no setcap and no custom entrypoint to maintain.
ARG NGINX_VERSION=1.29-alpine


FROM node:${NODE_VERSION}-alpine AS builder

WORKDIR /build

# The lockfile and its manifest alone, before any source. Source changes
# every commit and the dependency tree changes rarely, so keeping them in
# separate layers means editing a component does not reinstall 400 packages.
COPY package.json package-lock.json ./

# `npm ci`, never `npm install`. ci installs the lockfile as written and
# fails when package.json and the lockfile disagree; install is allowed to
# quietly rewrite the lockfile to make them agree, which would mean the image
# ships a dependency tree no developer and no CI run ever had.
RUN npm ci

COPY . .

# `tsc -b && vite build`, both halves. Type-checking here rather than trusting
# CI to have done it means an image cannot be built from a tree that does not
# type-check -- which is the state a `docker build` is most often reached for.
RUN npm run build


FROM nginxinc/nginx-unprivileged:${NGINX_VERSION} AS runtime

# Replaces the base image's default server block rather than adding to it.
# Both listen on 8080, and two servers on one port with no distinguishing
# server_name means the first one loaded wins -- which would be a coin toss
# between this config and a directory listing of the stock welcome page.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Static output only. node_modules, the TypeScript sources, the eslint config
# and the 400-package dependency tree all stay in the builder: the runtime
# image is a web server and a directory of files.
COPY --from=builder /build/dist /usr/share/nginx/html

# Inherited from the base image and restated for the reader: nginx runs as
# uid 101, which is why the port is 8080 and not 80.
USER 101

EXPOSE 8080
