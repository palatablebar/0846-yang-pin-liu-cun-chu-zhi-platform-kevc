# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据（含已停用签发的原证据）。
- `GET /api/holds`：补录保全清单。
- `POST /api/holds`：补录迟到保全，提交 `profile`、`sample_id`、`effective_on`、`note`；在原判定日（含）前生效且改判“待处置→留存”时，自动停用受影响签发。
- `GET /api/deactivations`：签发停用审计登记。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。
