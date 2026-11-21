# Mihomo + Zashboard for Home Assistant OS

在**原版 x86-64 HAOS** 上安装 Mihomo TUN 透明代理和原版 Zashboard。插件由原版 Supervisor 管理，覆盖 HAOS 本机、Core、Supervisor 和插件容器的联网。局域网、Docker 内部网络和 mDNS/SSDP 设备发现保持直连。

仓库只构建和发布**插件容器、安装包和 `install.sh`**。安装官方 HAOS 后运行脚本即可；无需刷定制 HAOS 镜像。

## 运行安装脚本

先安装官方 HAOS。实体 x86-64 设备选择 generic-x86-64，虚拟机选择官方 OVA。等待 Supervisor 和 Core 完成首次初始化；官方 HAOS 的首次初始化仍需要联网。

在设备本地的 HA 控制台输入 `login`，进入 **HAOS 主机 root shell**，执行：

```sh
wget -O /mnt/data/install-mihomo.sh https://github.com/wsx68696/hassio-mihomo/releases/latest/download/install.sh &&
sh /mnt/data/install-mihomo.sh
```

也可以在电脑上从 [Releases](https://github.com/wsx68696/hassio-mihomo/releases) 下载 `install.sh`，通过 USB、Samba 的 `share` 或 HAOS 调试 SSH 传入后运行。普通 SSH/终端插件中的 shell 位于容器内，需要使用 HAOS 主机控制台或调试 SSH。

脚本会下载并校验完整依赖包，注册本公开仓库，再通过原版 Supervisor 的**仅恢复本插件**接口导入预构建的插件镜像。Python、PyYAML、Mihomo、Zashboard 和网络工具已经包含在依赖包中，无需在 HAOS 上在线编译。只保存 data 中的插件、仓库缓存和插件自身数据，通过官方 API 保存安装状态，保留 Supervisor/Core 程序和官方更新机制。

安装成功后，在 HA 侧栏打开 **Mihomo**，导入 Clash/Mihomo 订阅或含 `proxies` 的节点文件。没有节点时保持直连；有效节点加载后开启 TUN。点击“打开 Zashboard”选择节点、模式，查看连接和日志。

API 密钥由每台设备首次运行时单独生成，只在 HA 登录后的配置页面提供。订阅、节点、面板选择和运行时软件保存在设备 data 中。重复运行脚本会保留已安装插件的数据。

## 首次下载需要代理

可以给脚本指定设备能够访问的 HTTP 代理：

```sh
sh /mnt/data/install-mihomo.sh --proxy http://代理主机的局域网IP:7897
```

如果脚本本身也需要通过代理下载，执行以下命令，将示例 IP 换成代理电脑的局域网 IP，并确保代理允许局域网连接：

```sh
http_proxy=http://192.168.1.10:7897 https_proxy=http://192.168.1.10:7897 \
  wget -O /mnt/data/install-mihomo.sh https://github.com/wsx68696/hassio-mihomo/releases/latest/download/install.sh &&
sh /mnt/data/install-mihomo.sh --proxy http://192.168.1.10:7897
```

也可在电脑上下载 `mihomo-installer-amd64.tar.gz` 和 `SHA256SUMS`，校验后传到设备，解压运行其中的 `install.sh`。插件导入阶段使用包内依赖；注册更新仓库遵守 Supervisor 的正常联网和支持检查。

## 通过插件商店更新

脚本自动添加的更新仓库为：

```text
https://github.com/wsx68696/hassio-mihomo
```

新的插件版本经 Actions 验证并发布到公开 GHCR 后，在 HA 的插件商店检查更新即可更新 Python 管理程序和完整插件容器。可以在插件设置中选择启用自动更新。订阅、节点和运行时更新缓存仍在插件自身 data 中。

Mihomo 与 Zashboard 也提供面板内的软件更新，下载方式可选择 `auto`、`proxy` 或 `direct`。`auto` 先使用已有代理，失败后尝试绕过 TUN 的直连。软件及订阅替换前会验证，失败保留原内容；Mihomo 新程序启动失败会回退。

Supervisor 更新完整插件时先下载新镜像，再停止旧插件，因此下载阶段可以使用当前 TUN。HAOS、Supervisor、Core 继续按各自官方流程更新；HAOS A/B 更新保留 data，不需要每次系统更新重新运行安装脚本。

## 权限与支持状态

保留插件保护模式和自定义 AppArmor，只使用主机网络、`NET_ADMIN`、`NET_RAW` 与 `/dev/net/tun`。不使用 Docker API、主机 PID/DBus 或完整主机访问，不修改 NetworkManager、systemd-resolved、Docker daemon 或 Supervisor/Core 代码。

Mihomo 管理自己的 TUN 和 nftables 表；保留系统/Docker 的其他防火墙规则和系统 IPv6 设置。实际设备的状态可用 `ha supervisor info` 和 `ha resolution info` 查看，检查结果由原版 Supervisor 提供。

## Actions 验证与发布

- 设置持久化、节点校验、坏订阅保留、程序更新回退和安装包内容测试。
- 隔离容器网络测试本机 TUN、转发容器、DNS、局域网直连、现有代理更新订阅和直连回退。
- 启动未修改的官方 HAOS OVA，确认 Core onboarding 可用，再运行实际安装脚本。
- 验证依赖导入阶段无需公共网络、Core/Supervisor 容器保持、公开仓库注册、保护模式/AppArmor、TUN、重复安装保留设置。
- 通过已有代理更新 Mihomo，执行官方签名的同版本 A/B OTA 重装，检查配置、插件和真实 `supported/healthy` 状态保留。
- GHCR 镜像可匿名拉取后，才发布安装脚本、校验文件和对应源码；测试用的 HAOS 系统磁盘不分发。

当前测试使用官方 HAOS 18.4 的 OVA 与同版本签名 OTA，不代表已经测试所有未来 HAOS 版本或所有实体硬件。官方镜像仅用于 Actions 临时验证。

维护插件管理程序时，递增 `mihomo/config.json` 的 `version` 后推送 `main`；Actions 从该版本生成匹配的容器标签和 Release。发布前需要确保 GitHub 的容器包 `hassio-mihomo-amd64` 已设为公开读取。

## 开源组件

安装脚本与管理程序采用 MIT。未修改的 [Mihomo v1.19.34](https://github.com/MetaCubeX/mihomo/tree/v1.19.34) 采用 GPL v3，匹配源码随 Release 提供；[Zashboard v3.30.0](https://github.com/Zephyruso/zashboard/tree/v3.30.0) 采用 MIT。对应许可证保存在 `mihomo/licenses/`。
