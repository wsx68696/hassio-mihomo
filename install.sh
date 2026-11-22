#!/bin/sh
# Release preparation pins this entry point to one version and bundle digest.
set -eu
umask 077

REPOSITORY_URL=https://github.com/wsx68696/hassio-mihomo
REPOSITORY_ID=055490f8
APP_SLUG=055490f8_mihomo
INSTALLER_VERSION=1.19.34-5
BUNDLE_SHA256=__BUNDLE_SHA256__
BUNDLE_NAME=mihomo-installer-amd64.tar.gz
DOWNLOAD_PROXY=''
REGISTER_ONLY=0
TEMP_DIR=''

fail() { printf '%s\n' "$*" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --proxy)
            [ "$#" -ge 2 ] || fail '--proxy 需要一个 HTTP 代理 URL。'
            DOWNLOAD_PROXY=$2
            case "$DOWNLOAD_PROXY" in http://*) ;; *) fail '请使用 http://IP:端口 形式的代理地址。' ;; esac
            shift 2 ;;
        --register-only) REGISTER_ONLY=1; shift ;;
        --help|-h)
            printf '%s\n' '用法：sh install.sh [--proxy http://代理主机IP:7897]' \
                '在 HAOS 本地控制台输入 login 后运行，或运行完整安装包中的脚本。'
            exit 0 ;;
        *) fail "未知参数：$1" ;;
    esac
done

[ "$(id -u)" = 0 ] || fail '请在 HAOS 主机 root shell 中运行；本地 HA 控制台输入 login 即可进入。'
case "$(uname -m)" in x86_64) ;; *) fail '此版本仅支持 x86-64 HAOS。' ;; esac
[ -r /etc/os-release ] || fail '无法确认 HAOS 系统。'
. /etc/os-release
case "${ID:-}" in haos|hassos) ;; *) fail '请在原版 HAOS 主机上运行，普通终端插件的容器 shell 不适用。' ;; esac
command -v ha >/dev/null 2>&1 || fail '找不到官方 ha 命令。'
command -v flock >/dev/null 2>&1 || fail '此 HAOS 缺少 flock，请使用受支持的官方 HAOS 版本。'
[ -d /mnt/data/supervisor ] || fail 'data 尚未就绪，请等待首次启动完成。'
ha supervisor info --no-progress >/dev/null || fail 'Supervisor 尚未就绪，请稍后重新运行。'

# Kernel locks are released on process death and reboot. Every code path,
# including download, registration and repair, shares this same lock.
exec 9>/mnt/data/.mihomo-install.flock
flock -n 9 || fail '已有安装任务正在运行，请等待它结束。'
cleanup() {
    if [ -n "$TEMP_DIR" ]; then
        case "$TEMP_DIR" in /mnt/data/mihomo-installer.*) rm -rf -- "$TEMP_DIR" ;; esac
    fi
    exec 9>&-
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

register_repository() {
    if ha store --help >/dev/null 2>&1; then
        STORE_INFO=$(ha store --raw-json --no-progress) || fail '读取插件商店失败，未尝试重复添加仓库。'
        if ! printf '%s\n' "$STORE_INFO" | grep -Eq '"slug"[[:space:]]*:[[:space:]]*"055490f8"'; then
            ha store add "$REPOSITORY_URL" --no-progress
        fi
    else
        STORE_INFO=$(ha addons repositories --raw-json --no-progress) || fail '读取更新仓库失败。'
        if ! printf '%s\n' "$STORE_INFO" | grep -Fq "$REPOSITORY_URL"; then
            ha addons repositories add "$REPOSITORY_URL" --no-progress
        fi
    fi
}

installed_app() {
    # "addons info" can also describe an uninstalled store app. Only the
    # installed-app listing is authoritative for this decision.
    APPS_INFO=$(ha addons --raw-json --no-progress) || fail '无法读取已安装插件列表，未更改任何插件数据。'
    printf '%s\n' "$APPS_INFO" | grep -Eq '"slug"[[:space:]]*:[[:space:]]*"055490f8_mihomo"'
}

healthy_app() {
    APP_INFO=$(ha addons info "$APP_SLUG" --raw-json --no-progress 2>/dev/null) || return 1
    printf '%s\n' "$APP_INFO" | grep -Eq '"state"[[:space:]]*:[[:space:]]*"started"' || return 1
    if command -v curl >/dev/null 2>&1; then
        DASHBOARD=$(curl --noproxy '*' -fsS --max-time 5 http://127.0.0.1:9090/ui/ 2>/dev/null) || return 1
    else
        DASHBOARD=$(http_proxy='' https_proxy='' wget -q -T 5 -O - http://127.0.0.1:9090/ui/ 2>/dev/null) || return 1
    fi
    printf '%s\n' "$DASHBOARD" | grep -qi '<html'
}

wait_healthy() {
    attempts=0
    while [ "$attempts" -lt 60 ]; do
        healthy_app && return 0
        attempts=$((attempts + 1))
        sleep 2
    done
    return 1
}

if installed_app; then
    register_repository
    [ "$REGISTER_ONLY" != 1 ] || exit 0
    if ! healthy_app; then
        printf '%s\n' '检测到已注册但未就绪的 Mihomo，尝试由 Supervisor 恢复启动，保留现有数据……'
        if ! ha addons restart "$APP_SLUG" --no-progress; then
            ha addons start "$APP_SLUG" --no-progress || true
        fi
        if ! wait_healthy; then
            # Standard rebuilding preserves the app's data and HA options.
            ha addons rebuild "$APP_SLUG" --no-progress || fail '插件未能恢复；已保留配置，请查看 Supervisor 日志。'
            ha addons start "$APP_SLUG" --no-progress || fail '插件仍无法启动；已保留配置。'
            wait_healthy || fail '插件健康检查未通过，未将本次安装标记为成功。'
        fi
    fi
    printf '%s\n' 'Mihomo 已安装且面板健康，现有配置已保留。后续更新可在 HA 插件商店进行。'
    exit 0
fi

PACKAGE_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
if [ ! -f "$PACKAGE_DIR/mihomo-backup.tar" ]; then
    case "$BUNDLE_SHA256" in ''|*[!0-9a-f]*) fail '请使用 Releases 中已固定版本和摘要的 install.sh，或解压完整安装包运行。' ;; esac
    [ "${#BUNDLE_SHA256}" = 64 ] || fail '安装入口缺少固定的安装包摘要。'
    TEMP_DIR=$(mktemp -d /mnt/data/mihomo-installer.XXXXXX)
    printf '%s\n' "下载已验证的 Mihomo $INSTALLER_VERSION 安装包……"
    RELEASE_URL="$REPOSITORY_URL/releases/download/v$INSTALLER_VERSION"
    if command -v curl >/dev/null 2>&1; then
        if [ -n "$DOWNLOAD_PROXY" ]; then
            curl --fail --location --retry 3 --connect-timeout 20 --max-time 1200 \
                --proxy "$DOWNLOAD_PROXY" --noproxy '' --output "$TEMP_DIR/$BUNDLE_NAME" "$RELEASE_URL/$BUNDLE_NAME"
        else
            curl --fail --location --retry 3 --connect-timeout 20 --max-time 1200 \
                --output "$TEMP_DIR/$BUNDLE_NAME" "$RELEASE_URL/$BUNDLE_NAME"
        fi
    else
        if [ -n "$DOWNLOAD_PROXY" ]; then
            no_proxy='' NO_PROXY='' http_proxy="$DOWNLOAD_PROXY" https_proxy="$DOWNLOAD_PROXY" \
                wget -T 60 -O "$TEMP_DIR/$BUNDLE_NAME" "$RELEASE_URL/$BUNDLE_NAME"
        else
            wget -T 60 -O "$TEMP_DIR/$BUNDLE_NAME" "$RELEASE_URL/$BUNDLE_NAME"
        fi
    fi
    (cd "$TEMP_DIR" && printf '%s  %s\n' "$BUNDLE_SHA256" "$BUNDLE_NAME" | sha256sum -c -) || fail '安装包与入口中固定的 SHA256 不符。'
    tar -xzf "$TEMP_DIR/$BUNDLE_NAME" -C "$TEMP_DIR"
    PACKAGE_DIR="$TEMP_DIR/haos-mihomo-installer"
fi

printf '%s\n' '校验插件、离线仓库缓存和运行依赖……'
(cd "$PACKAGE_DIR" && sha256sum -c SHA256SUMS >/dev/null) || fail '安装包内部校验失败。'
BACKUP_SLUG=$(cat "$PACKAGE_DIR/backup-slug.txt")
case "$BACKUP_SLUG" in ''|*[!0-9a-f]*) fail '安装包的备份标识无效。' ;; esac
[ "${#BACKUP_SLUG}" = 8 ] || fail '安装包的备份标识长度无效。'

if [ -d /mnt/data/supervisor/apps ]; then
    STORE_DIR=/mnt/data/supervisor/apps/git
else
    STORE_DIR=/mnt/data/supervisor/addons/git
fi
mkdir -p "$STORE_DIR" /mnt/data/supervisor/backup
if [ ! -e "$STORE_DIR/$REPOSITORY_ID" ]; then
    cp -R "$PACKAGE_DIR/repository" "$STORE_DIR/$REPOSITORY_ID"
fi
printf '%s\n' '注册公开 Mihomo 插件更新仓库……'
register_repository
[ "$REGISTER_ONLY" != 1 ] || exit 0

BACKUP_PATH="/mnt/data/supervisor/backup/$BACKUP_SLUG.tar"
if [ -e "$BACKUP_PATH" ]; then
    cmp -s "$PACKAGE_DIR/mihomo-backup.tar" "$BACKUP_PATH" || fail '同名备份已存在且内容不同，已保留原备份。'
else
    cp "$PACKAGE_DIR/mihomo-backup.tar" "$BACKUP_PATH"
fi
printf '%s\n' '通过原版 Supervisor 导入 Mihomo 及完整运行依赖……'
ha backups reload --no-progress >/dev/null
ha backups restore "$BACKUP_SLUG" --addons "$APP_SLUG" --homeassistant=false --no-progress || fail 'Supervisor 恢复未完成，请检查日志后重试。'
installed_app || fail 'Supervisor 尚未完成插件安装。'
wait_healthy || fail '插件面板健康检查未通过，未将本次安装标记为成功。'
printf '%s\n' \
    'Mihomo + Zashboard 已安装，并由 Supervisor 管理开机启动。' \
    '在 HA 的 Mihomo 面板导入订阅或 Clash 配置；验证后启用 TUN。' \
    '插件更新在 HA 商店进行；Mihomo、面板和规则资料可通过当前代理更新。'
