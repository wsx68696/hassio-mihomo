# Mihomo 配置

已安装原版 x86-64 HAOS 的设备，可从仓库 Release 下载离线插件安装包，在 HAOS 主机 root shell 运行其中的 `install.sh`。此安装方式使用 Supervisor 的标准插件部分恢复流程；安装后本插件仍由 Supervisor 管理，并保留官方 HAOS 更新。详细命令见仓库 README。

从 Home Assistant 侧栏打开 Mihomo，填写 Clash/Mihomo 订阅或导入节点/完整 YAML 配置。支持分组、`rules`、`rule-providers`、`proxy-providers`、GEOIP 和 GEOSITE；无效配置保留旧缓存。没有配置时 TUN 保持关闭，加载后按设置启用 TUN。

点击“打开 Zashboard”管理节点、模式和连接。控制器位于 HAOS 本机 9090 端口，需要配置页提供的密钥。模式、节点选择和 TUN 开关会保存到插件数据目录。

订阅与软件更新默认优先使用已有代理，并提供直连回退。只能通过代理访问订阅时，请先导入至少一个可用节点，再选择“仅通过现有代理”。失败的订阅不会覆盖旧节点。

排查订阅格式时，可以临时勾选“在日志中记录完整订阅响应”并保存。下次收到订阅正文时，完整内容会写入插件日志。正文可能包含节点凭据。

“更新 Mihomo 稳定版”和“更新 Zashboard 稳定版”下载并校验上游 Release；Mihomo 启动失败时回退。软件和配置保存在插件自己的持久化数据中，可随插件备份保存。

“GeoSite / GeoIP 规则资料”提供立即更新和定时更新。插件绕过 TUN，直连 gh-proxy.com 查询版本并下载资料。四份数据全部通过 SHA256 和 Mihomo 校验后一起切换。重载失败时恢复旧资料。

订阅的 `geo-auto-update` 和 `geo-update-interval` 会导入定时设置，默认间隔 24 小时。文件 provider 仅可读取 `/data/user-providers/`，建议优先使用 HTTP 或 inline provider。系统管理网络和局域网 DIRECT 规则优先，TUN/DNS/控制器由插件设置管理。

Home Assistant 2026.8 起新装默认 HTTP 端口是 80；本插件的 Zashboard 仍使用 9090 端口。

保留插件保护模式和 AppArmor，避免同时启用其他修改路由或 nftables 的代理插件。局域网、HA 内部 DNS 和设备发现流量直连。HAOS、Supervisor、Core 仍通过官方渠道更新。
