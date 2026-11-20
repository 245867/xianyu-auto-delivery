# Changelog

本文件记录项目各版本的重要变更，按版本倒序排列。分类参考 [Keep a Changelog](https://keepachangelog.com/)。

## v1.1.1

### Changed

- 文件日志通过 `enqueue=True` 异步写盘，减少主事件循环等待磁盘 I/O 的时间。
- 每次写入比价历史前自动清理 30 天前的记录。

### Fixed

- 刷新 Token 时保持 WebSocket 心跳任务运行。
- 通知留痕写入失败或看门狗通知发送失败时记录警告，主流程继续运行。

## v1.1.0

### Added

- 供应商代采框架。
- 买家自助补发。
- 蓝海热词评分。
- AI 商品配图生成。

### Changed

- SQLite 升级为 WAL 模式。

## v1.0.0

### Added

- 基础自动发货。
- 关键词回复。
- 多账号托管。
- 黑名单风控。
