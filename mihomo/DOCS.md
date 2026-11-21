# Mihomo 配置

已安装原版 x86-64 HAOS 的设备，可从仓库 Release 下载离线插件安装包，在 HAOS 主机 root shell 运行其中的 `install.sh`。此安装方式使用 Supervisor 的标准插件部分恢复流程；安装后本插件仍由 Supervisor 管理，并保留官方 HAOS 更新。详细命令见仓库 README。

从 Home Assistant 侧栏打开 Mihomo，填写 Clash/Mihomo 订阅或导入含 `proxies` 列表的节点文件。没有可用节点时 TUN 保持关闭。订阅更新成功后自动启用 TUN。

点击“打开 Zashboard”管理节点、模式和连接。控制器位于 HAOS 本机 9090 端口，需要配置页提供的密钥。模式、节点选择和 TUN 开关会保存到插件数据目录。

订阅与软件更新默认优先使用已有代理，并提供直连回退。只能通过代理访问订阅时，请先导入至少一个可用节点，再选择“仅通过现有代理”。失败的订阅不会覆盖旧节点。

“更新 Mihomo 稳定版”和“更新 Zashboard 稳定版”下载并校验上游 Release；Mihomo 启动失败时回退。软件和配置保存在插件自己的持久化数据中，可随插件备份保存。

保留插件保护模式和 AppArmor，避免同时启用其他修改路由或 nftables 的代理插件。局域网、HA 内部 DNS 和设备发现流量直连。HAOS、Supervisor、Core 仍通过官方渠道更新。
