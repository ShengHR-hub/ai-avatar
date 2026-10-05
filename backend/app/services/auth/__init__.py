"""认证 Service 层（ENG-03）。

- `session_store`：Redis 会话存储（会话令牌摘要、版本化 key 命名空间、TTL）；
- 后续步骤将在此包内加入邀请码、邮箱绑定与用户资料 Service。
"""
