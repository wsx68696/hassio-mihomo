#!/bin/sh
# Install this app through the original Supervisor. Everything stays in data.
set -eu
umask 077

REPOSITORY_URL=https://github.com/wsx68696/hassio-mihomo
REPOSITORY_ID=055490f8
APP_SLUG=055490f8_mihomo
BUNDLE_NAME=mihomo-installer-amd64.tar.gz
DOWNLOAD_PROXY=''
REGISTER_ONLY=0

fail() { printf '%s\n' "$*" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --proxy)
            [ "$#" -ge 2 ] || fail '--proxy 需要一个 HTTP 代理 URL。'
            DOWNLOAD_PROXY=$2
            case "$DOWNLOAD_PROXY" in http://*) ;; *) fail '请使用 http://IP:端口 形式的代理地址。' ;; esac
            shift 2 ;;
        --help|-h)
            printf '%s\n' '用法：sh install.sh [--proxy http://代理主机IP:7897]' \
                '在 HAOS 本地控制台输入 login 后运行；也可解压完整安装包后运行其中的 install.sh。'
            exit 0 ;;
        --register-only) REGISTER_ONLY=1; shift ;;
        *) fail "未知参数：$1" ;;
    esac
done

[ "$(id -u)" = 0 ] || fail '请在 HAOS 主机 root shell 中运行；本地 HA 控制台输入 login 即可进入。'
case "$(uname -m)" in x86_64) ;; *) fail '此版本仅支持 x86-64 HAOS。' ;; esac
[ -r /etc/os-release ] || fail '无法确认 HAOS 系统。'
. /etc/os-release
case "${ID:-}" in haos|hassos) ;; *) fail '请在原版 HAOS 主机上运行，普通 SSH/终端插件的容器 shell 不适用。' ;; esac
command -v ha >/dev/null 2>&1 || fail '找不到官方 ha 命令。'
[ -d /mnt/data/supervisor ] || fail 'data 尚未就绪，请等待首次启动完成。'
ha supervisor info --no-progress >/dev/null || fail 'Supervisor 尚未就绪，请稍后重新运行。'

register_repository() {
    if ha store --help >/dev/null 2>&1; then
        if ! ha store --raw-json --no-progress 2>/dev/null | grep -Eq '"slug"[[:space:]]*:[[:space:]]*"055490f8"'; then
            ha store add "$REPOSITORY_URL" --no-progress
        fi
    else
        if ! ha addons repositories --raw-json --no-progress 2>/dev/null | grep -Fq "$REPOSITORY_URL"; then
            ha addons repositories add "$REPOSITORY_URL" --no-progress
        fi
    fi
}

# Re-running must never reset saved nodes, credentials or upgraded software.
if ha addons info "$APP_SLUG" --no-progress >/dev/null 2>&1; then
    register_repository
    printf '%s\n' 'Mihomo 已安装，保留现有配置。后续插件更新可直接在 HA 插件商店进行。'
    exit 0
fi

PACKAGE_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
if [ ! -f "$PACKAGE_DIR/mihomo-backup.tar" ]; then
    TEMP_DIR=$(mktemp -d /mnt/data/mihomo-installer.XXXXXX)
    cleanup_download() {
        case "$TEMP_DIR" in /mnt/data/mihomo-installer.*) rm -rf -- "$TEMP_DIR" ;; esac
    }
    trap cleanup_download EXIT
    trap 'exit 1' HUP INT TERM
    fetch_file() {
        if [ -n "$DOWNLOAD_PROXY" ]; then
            http_proxy="$DOWNLOAD_PROXY" https_proxy="$DOWNLOAD_PROXY" wget -T 60 -O "$2" "$1"
        else
            wget -T 60 -O "$2" "$1"
        fi
    }
    printf '%s\n' '下载已构建的 Mihomo 插件安装包……'
    RELEASE_URL="$REPOSITORY_URL/releases/latest/download"
    fetch_file "$RELEASE_URL/$BUNDLE_NAME" "$TEMP_DIR/$BUNDLE_NAME"
    fetch_file "$RELEASE_URL/SHA256SUMS" "$TEMP_DIR/SHA256SUMS"
    (cd "$TEMP_DIR" && grep "  $BUNDLE_NAME\$" SHA256SUMS | sha256sum -c -) || fail '安装包 SHA256 校验失败。'
    tar -xzf "$TEMP_DIR/$BUNDLE_NAME" -C "$TEMP_DIR"
    if [ "$REGISTER_ONLY" = 1 ]; then
        sh "$TEMP_DIR/haos-mihomo-installer/install.sh" --register-only
    else
        sh "$TEMP_DIR/haos-mihomo-installer/install.sh"
    fi
    exit 0
fi

printf '%s\n' '校验插件、离线仓库缓存和运行依赖……'
(cd "$PACKAGE_DIR" && sha256sum -c SHA256SUMS >/dev/null) || fail '安装包内部校验失败。'
BACKUP_SLUG=$(cat "$PACKAGE_DIR/backup-slug.txt")
case "$BACKUP_SLUG" in ''|*[!0-9a-f]*) fail '安装包的备份标识无效。' ;; esac
[ "${#BACKUP_SLUG}" = 8 ] || fail '安装包的备份标识长度无效。'

LOCK_DIR=/mnt/data/.mihomo-install.lock
mkdir "$LOCK_DIR" 2>/dev/null || fail '已有安装任务正在运行。'
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
trap 'exit 1' HUP INT TERM

if [ -d /mnt/data/supervisor/apps ]; then
    STORE_DIR=/mnt/data/supervisor/apps/git
else
    STORE_DIR=/mnt/data/supervisor/addons/git
fi
mkdir -p "$STORE_DIR" /mnt/data/supervisor/backup
if [ ! -e "$STORE_DIR/$REPOSITORY_ID" ]; then
    cp -R "$PACKAGE_DIR/repository" "$STORE_DIR/$REPOSITORY_ID"
fi

# Existing clean Git caches are loaded by Supervisor without network cloning.
# Repository membership is saved exclusively through the standard API.
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
ha backups restore "$BACKUP_SLUG" --addons "$APP_SLUG" --homeassistant=false --no-progress
ha addons info "$APP_SLUG" --no-progress >/dev/null || fail '未能确认插件安装状态，请查看 Supervisor 日志。'
printf '%s\n' \
    'Mihomo + Zashboard 已安装，并由 Supervisor 管理开机启动。' \
    '在 HA 的 Mihomo 面板导入订阅或节点；有效节点加载后启用 TUN。' \
    '以后的插件管理程序更新可在 HA 插件商店完成；Mihomo/Zashboard 也可在面板通过当前代理更新。'
