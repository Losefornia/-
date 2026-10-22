# 📺 AstrBot 墨水屏数据中台插件 (eink_api)

本项目是一个基于 AstrBot 框架开发的综合数据接口插件，专为 ESP32 墨水屏设备（如 4.2 寸考研/工作看板）打造。
它集成了宿舍电费、莱山天气、校园新闻、专注打卡，以及通过 QQ 官方机器人远程发送备忘录的功能。

## 📁 目录结构
```text
astrbot_plugin_eink_api/
├── main.py           # 核心代码
├── metadata.json     # 插件信息
├── study_data.db     # 自动生成：专注打卡数据库
└── eink_memos.db     # 自动生成：QQ备忘录数据库
