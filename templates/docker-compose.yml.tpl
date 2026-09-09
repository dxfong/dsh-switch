# dsh docker-compose 部署模板（dsh-switch 部署器生成）
# 占位符：__CONTAINER__ 容器名 / __PORT__ dsh 端口 / __DEPLOY_DIR__ 服务器部署目录
name: dsh-switch

services:
  __CONTAINER__:
    build:
      context: .
      args:
        DSH_VERSION: "__DSH_VERSION__"
    image: dsh-switch:__DSH_VERSION__
    container_name: __CONTAINER__
    restart: unless-stopped
    # host 网络：dsh 只能绑 127.0.0.1（官方安全硬限制），
    # host 模式下直接落在宿主机回环，SSH 隧道直达，无任何端口对局域网/公网暴露。
    network_mode: host
    environment:
      DSH_HOME: /data/dsh
      DSH_PORT: "__PORT__"
      DSH_TELEMETRY_MODE: DISABLED
      TZ: Asia/Shanghai
    volumes:
      - __DEPLOY_DIR__/data/home:/data/dsh
      - __DEPLOY_DIR__/data/workspace:/workspace
    working_dir: /workspace
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    read_only: true
    tmpfs:
      - /tmp:rw,nosuid,nodev,size=1g
    stop_grace_period: 10s
