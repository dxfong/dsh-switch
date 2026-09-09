# DeepSeek Harness 容器镜像模板
# 占位符（部署时由 dsh-switch 程序替换）：
#   __DSH_VERSION__  dsh 版本号（npm 包版本或 latest）
#   __NPM_REGISTRY__ npm 源（https://registry.npmmirror.com 或 https://registry.npmjs.org）
# 目标平台：linux/amd64 与 linux/arm64 通用（dsh 是纯 Node 应用）
# 说明：dsh 官方只发 npm 包（@deepseek-ai/dsh），无官方镜像，故用 node:22 基础镜像自装。
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

# agent 的"手"：shell / git / python；tini 作 PID 1（正确回收子进程）
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git openssh-client python3 python3-venv python3-pip \
        ca-certificates curl jq ripgrep procps tini \
    && rm -rf /var/lib/apt/lists/*

# 安装 dsh（__NPM_REGISTRY__ 由部署器按网络环境选择：国内镜像源失败自动回退官方源）
RUN npm config set registry "__NPM_REGISTRY__" \
    && npm install -g @deepseek-ai/dsh@__DSH_VERSION__ \
    && npm install -g pnpm@10 \
    && npm cache clean --force

RUN dsh --version || true

WORKDIR /workspace
RUN mkdir -p /data/dsh /workspace

ENV DSH_HOME=/data/dsh \
    DSH_TELEMETRY_MODE=DISABLED \
    HOME=/data/dsh \
    NPM_CONFIG_CACHE=/data/dsh/.cache/npm \
    PNPM_HOME=/data/dsh/.local/share/pnpm \
    PNPM_STORE_DIR=/data/dsh/.local/share/pnpm/store \
    XDG_CACHE_HOME=/data/dsh/.cache \
    NODE_ENV=production

USER node

VOLUME ["/data", "/workspace"]
EXPOSE 3080

# dsh 的 / 无 token 时返回 401，属正常；200/303/401 都算活着
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
    CMD curl -s -o /dev/null -m 5 -w '%{http_code}' \
        "http://127.0.0.1:${DSH_PORT:-3080}/" | grep -qE '^(200|303|401)$'

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# 执行位已由部署器在 SFTP 上传时设置（COPY 保留源权限）；
# 这里的 chmod 仅作兜底，`|| true` 防个别 overlayfs 环境的 EPERM 中断构建
RUN chmod +x /usr/local/bin/entrypoint.sh || true

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
