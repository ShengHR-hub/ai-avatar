# backend/app/repositories/

SQLAlchemy 数据访问层。

职责：SQLAlchemy 查询、数据持久化、行锁和数据库级查询优化。

禁止：HTTP 逻辑；Provider 调用；跨领域业务决策；隐式提交事务。

ENG-00 不在此目录创建任何 Repository。实现属于 `ENG-01` 及后续业务卡。
